/* Execute reviewed M74 CAN-loader instructions through Ghidra's C166 p-code.
 *
 * Run against a raw image at C00000 with -readOnly -noanalysis. The script changes
 * only the scratch listing and emulator state, never the input image or a device.
 *
 * Model boundaries:
 * - CALLS/RETS use a Java return stack; the hardware system stack is not modeled.
 * - C166 address CALLOTHER operations use explicit EXTP/EXTS/DPP translation.
 * - RX data is injected into message-object registers, with immediate NEWDAT clear.
 * - The real transmit-copy helper runs; no bus arbitration, ACK or timing is modeled.
 * - Flash erase/program calls are recorded, not executed; status polling returns ready.
 * - CRC table generation, byte updates, range checks and dispatch branches do run
 *   from the supplied firmware. Unknown p-code operations or budgets fail the run.
 *
 * The initial A5 RAM pattern intentionally exposes uninitialized response bytes.
 * Test data for page assembly is synthetic. No reconstructed firmware is exported.
 * @category openm74
 */
import ghidra.app.script.GhidraScript;
import ghidra.app.emulator.EmulatorHelper;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.pcode.emulate.BreakCallBack;
import ghidra.pcode.memstate.MemoryState;
import ghidra.pcode.pcoderaw.PcodeOpRaw;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.lang.Register;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Deque;
import java.util.HexFormat;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.CRC32;
import java.security.MessageDigest;

public class EmulateCanLoader extends GhidraScript {
 int delta=0; byte[] image;
 void require(boolean yes,String message) { if(!yes)throw new IllegalStateException(message); }
 class Run implements AutoCloseable {
  EmulatorHelper cpu=new EmulatorHelper(currentProgram);
  Deque<Long> returns=new ArrayDeque<>();
  long page=-1,segment=-1; int left=0; long steps=0; int tx=0;
  List<byte[]> replies=new ArrayList<>();List<byte[]> pages=new ArrayList<>();
  List<Long> pageAddresses=new ArrayList<>();List<Long> sectors=new ArrayList<>();
  Run() throws Exception {
   for(int i=0;i<16;i++) cpu.writeRegister("r"+i,0);
   for(int i=0;i<4;i++)cpu.writeRegister("DPP"+i,i);
   cpu.writeRegister("PSW",0);cpu.writeRegister("CSP",0xc0);cpu.writeRegister("r0",0xa458);
   byte[] ram=new byte[0x1000];Arrays.fill(ram,(byte)0xa5);cpu.writeMemory(toAddr(0xa000),ram);
   cpu.writeMemory(toAddr(0x201000),new byte[0x40]);
   cpu.registerCallOtherCallback("segment",new Translate(false,false));
   cpu.registerCallOtherCallback("c166_reg_offset_addr",new Translate(true,false));
   cpu.registerCallOtherCallback("GetPagedOffset",new Translate(false,true));
   cpu.registerCallOtherCallback("c166_switch_load",new BreakCallBack(){
    public boolean pcodeCallback(PcodeOpRaw op){
     MemoryState st=emulate.getMemoryState();long off=st.getValue(op.getInput(1))&65535;
     long physical=left>0&&segment>=0?(segment<<16)|off:left>0&&page>=0?(page<<14)|(off&0x3fff):
      ((st.getValue(currentProgram.getRegister("DPP"+(off>>14)))&0x3ff)<<14)|(off&0x3fff);
     st.setValue(op.getOutput(),st.getValue(currentProgram.getAddressFactory().getDefaultAddressSpace(),physical,2));
     return true;
    }
   });
   word(0xff1c,0);word(0xff1e,65535);word(0xfd00,0);
  }
  class Translate extends BreakCallBack {
   boolean sum,single; Translate(boolean s,boolean one){sum=s;single=one;}
   public boolean pcodeCallback(PcodeOpRaw op) {
    MemoryState st=emulate.getMemoryState();
    long off=single?st.getValue(op.getInput(1)):sum?st.getValue(op.getInput(1))+st.getValue(op.getInput(2)):st.getValue(op.getInput(2));
    off&=65535;long physical;
    if(left>0&&segment>=0)physical=(segment<<16)|off;
    else if(left>0&&page>=0)physical=(page<<14)|(off&0x3fff);
    else physical=((st.getValue(currentProgram.getRegister("DPP"+(off>>14)))&0x3ff)<<14)|(off&0x3fff);
    st.setValue(op.getOutput(),physical);return true;
   }
  }
  long reg(String n){return cpu.readRegister(n).longValue()&65535;}
  void pc(long a){cpu.writeRegister(cpu.getPCRegister(),a);}
  long pc(){return cpu.getExecutionAddress().getOffset();}
  byte[] mem(long a,int n){return cpu.readMemory(toAddr(a),n);}
  void put(long a,byte[] b){cpu.writeMemory(toAddr(a),b);}
  void word(long a,int n){put(a,new byte[]{(byte)n,(byte)(n>>8)});}
  void dword(long a,long n){put(a,new byte[]{(byte)n,(byte)(n>>8),(byte)(n>>16),(byte)(n>>24)});}
  long dword(long a){byte[] b=mem(a,4);return ((long)(b[0]&255))|((long)(b[1]&255)<<8)|((long)(b[2]&255)<<16)|((long)(b[3]&255)<<24);}
  boolean step() throws Exception {
   long at=pc();Instruction ins=getInstructionAt(toAddr(at));
   require(ins!=null,"undecoded instruction "+Long.toHexString(at));
   String m=ins.getMnemonicString().toLowerCase();String text=ins.toString().toLowerCase();
   if(m.equals("calls")) {
    long target=ins.getFlows()[0].getOffset();
    if(target==0xc00a82L+delta) {
     pageAddresses.add(reg("r12")|(reg("r13")<<16));
     pages.add(mem(reg("r14")|(reg("r15")<<16),128));
     pc(at+ins.getLength());steps++;return true;
    }
    if(target==0xc00a58L+delta) {sectors.add(reg("r12")|(reg("r13")<<16));pc(at+ins.getLength());steps++;return true;}
    if(target==0xc009caL+delta) {cpu.writeRegister("r4",0);pc(at+ins.getLength());steps++;return true;}
    returns.push(at+ins.getLength());pc(target);steps++;return true;
   }
   if(m.equals("rets")) {
    if(at==0xc0058aL+delta){tx++;replies.add(reply());}
    if(returns.isEmpty())return false;
    pc(returns.pop());steps++;return true;
   }
   boolean extension=m.equals("extp")||m.equals("exts");
   if(extension) {
    Matcher mt=Pattern.compile("ext[ps]\\s+(\\S+),\\s*#?(0x[0-9a-f]+|\\d+)").matcher(text);
    require(mt.find(),"extension not parsed "+text);
    String value=mt.group(1).replace("#",""); Register r=currentProgram.getRegister(value);
    long v=r==null?Long.decode(value):reg(value);
    page=m.equals("extp")?v&0x3ff:-1;segment=m.equals("exts")?v&255:-1;
    left=Integer.decode(mt.group(2));
   }
   if(!cpu.step(monitor))throw new IllegalStateException("pcode failure at "+Long.toHexString(at)+" "+text+": "+cpu.getLastError());
   if(!extension&&left>0)left--;
   steps++;
   if(at==0xc0035aL+delta||at==0xc005b6L+delta)word(0x20101c,0); // modeled NEWDAT reset command
   return true;
  }
  void invoke(long entry,long limit) throws Exception {
   require(returns.isEmpty(),"unbalanced call shim");pc(entry);long end=steps+limit;
   while(steps<end&&!monitor.isCancelled())if(!step())return;
   throw new IllegalStateException("instruction budget at "+Long.toHexString(pc()));
  }
  void frame(byte[] data){require(data.length<=8,"oversized fixture");put(0x201010,data);word(0x201002,data.length<<8);word(0x20101c,8);}
  void idle() throws Exception {
   long end=steps+200000;
   while(steps<end&&!monitor.isCancelled()) {
    if(pc()==0xc005a0L+delta&&(mem(0x20101c,1)[0]&8)==0&&returns.isEmpty())return;
    require(step(),"unexpected root return in resident loop");
   }
   throw new IllegalStateException("idle instruction budget at "+Long.toHexString(pc()));
  }
  void resident() throws Exception {pc(0xc0059cL+delta);idle();}
  byte[] deliver(byte[] data) throws Exception {
   int before=tx;frame(data);idle();return tx==before?null:replies.get(replies.size()-1);
  }
  void session() throws Exception {resident();byte[] answer=deliver(new byte[]{1,0x12,2,0,0,0,0,0});
   require(Arrays.equals(answer,new byte[]{(byte)255,0,0x12,(byte)254,1,0,0,0}),"session reply");}
  void address(long value) throws Exception {deliver(new byte[]{2,0x22,0,0,(byte)value,(byte)(value>>8),(byte)(value>>16),(byte)(value>>24)});}
  byte[] crc(long address,long length) throws Exception {
   address(address);return deliver(new byte[]{14,0x33,(byte)length,(byte)(length>>8),(byte)(length>>16),(byte)(length>>24),0,0});
  }
  byte[] reply(){return mem(0x201030,8);}
  public void close(){cpu.dispose();}
 }
 // All trace payload bytes are generated here, never copied from the input image.
 String traceExchange(Run r, byte[] request, boolean startup) throws Exception {
  int before=r.tx;
  if(startup){r.frame(request);r.invoke(0xc002e4+delta,100000);r.resident();}
  else r.deliver(request);
  StringBuilder json=new StringBuilder("{\"request\":\"");
  json.append(HexFormat.of().formatHex(request)).append("\",\"replies\":[");
  for(int n=before;n<r.tx;n++){
   if(n>before)json.append(',');
   json.append('"').append(HexFormat.of().formatHex(r.replies.get(n))).append('"');
  }
  return json.append("]}").toString();
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size>=0x90000&&size<=0x200000,"unexpected raw image mapping");
  image=new byte[(int)size];int read=currentProgram.getMemory().getBytes(toAddr(0xc00000),image);
  require(read==size,"incomplete image memory read");
  require(read>=0x90000,"short program mapping");
  String h=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Arrays.copyOf(image,0x2000)));
  if(h.equals("a7162971cbbdaeebf8d9ae421220b981df64bcbd4bc876b503a7304ec2a0df99"))delta=4;
  else require(h.equals("7f1fd47c391a68460ebf3cfb78c0679c24e75470fd9787e368eb48032804310e")||h.equals("eb269ea46fe7432742f1d321ebe711cbf8a34b7aa1a03f78e6afaca4e2c84c7b"),"unreviewed boot hash");
  clearListing(toAddr(0xc002e4+delta),toAddr(0xc00bf0+delta));
  long a=0xc002e4+delta;
  while(a<0xc00bf0+delta){new DisassembleCommand(toAddr(a),null,false).applyTo(currentProgram,monitor);Instruction i=getInstructionAt(toAddr(a));require(i!=null,"decode gap "+Long.toHexString(a));a+=i.getLength();}
  long helper=delta==0?0xc00d9c:0xc00e10;
  clearListing(toAddr(helper),toAddr(helper+14));
  for(long at=helper;at<helper+14;){new DisassembleCommand(toAddr(at),null,false).applyTo(currentProgram,monitor);Instruction i=getInstructionAt(toAddr(at));require(i!=null,"helper decode gap");at+=i.getLength();}
  println("PROGRAM "+currentProgram.getName()+" boot="+h);
  println("IMAGE_SHA256="+HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(image)));
  println("ENGINE="+ghidra.framework.Application.getApplicationVersion()+" LANGUAGE="+currentProgram.getLanguageID());
  int cases=0;
  try(Run r=new Run()) {
   r.frame(new byte[]{1,0x33,(byte)0x96,0x69,0,0,0,0});
   r.invoke(0xc002e4+delta,100000);
   require((r.reg("PSW")&64)!=0,"startup flag");
   require(Arrays.equals(r.reply(),new byte[]{(byte)255,0,0x33,0x69,(byte)0x96,0x69,(byte)0xa5,(byte)0xa5}),"startup reply");cases++;
   r.resident();
   require(r.tx==2&&Arrays.equals(r.reply(),new byte[8]),"pending startup reprocessed with zero reply");cases++;
   require(r.deliver(new byte[]{14,0,4,0,0,0,0,0})==null,"CRC before session must be ignored");cases++;
   byte[] answer=r.deliver(new byte[]{1,0x12,2,0,0,0,0,0});
   require(Arrays.equals(answer,new byte[]{(byte)255,0,0x12,(byte)254,1,0,0,0}),"session negotiation");cases++;
   long[][] valid={{0xc02000,1},{0xc02000,4},{0xc02001,4},{0xc8fffc,4},{0xc02000,7},{0xc02000,128}};
   for(long[] test:valid) {
    byte[] reply=r.crc(test[0],test[1]);CRC32 expected=new CRC32();expected.update(image,(int)(test[0]-0xc00000),(int)test[1]);
    require(reply!=null&&(reply[1]&255)==0&&r.dword(0x201034)==expected.getValue(),"CRC execution mismatch");cases++;
   }
   long[][] invalid={{0xc02000,0},{0xc01fff,4},{0xc8fffd,4},{0xc90000,4},{0xfffffffcL,8}};
   for(long[] test:invalid){byte[] reply=r.crc(test[0],test[1]);require(reply!=null&&(reply[1]&255)==0x32,"CRC bounds not rejected");cases++;}
   byte[] stop=r.deliver(new byte[]{7,0x44,0,0,0,0,0,0});require(stop!=null,"session end response");
   require(r.deliver(new byte[]{14,0,4,0,0,0,0,0})==null,"ended session accepted CRC");cases++;
   println("CRC/startup/session cases passed="+cases+" instructions="+r.steps);
  }
  try(Run r=new Run()) {
   r.session();r.address(0xc02000);byte[] expected=new byte[128];
   for(int i=0;i<32;i++) {
    byte[] frame={0x18,(byte)i,4,(byte)(i*4),(byte)(i*4+1),(byte)(i*4+2),(byte)(i*4+3),0};
    System.arraycopy(frame,3,expected,i*4,4);r.deliver(frame);
    require(r.pages.size()==(i==31?1:0),"early or missing page commit");
   }
   require(Arrays.equals(r.pages.get(0),expected)&&r.pageAddresses.get(0)==0xc02000,"page content/address");cases++;
   require(r.dword(0xa40e)==0xc02080,"reported address progression");cases++;
   println("Full synthetic page passed; flash calls modeled="+r.pages.size());
  }
  try(Run r=new Run()) {
   r.session();r.address(0xc02000);byte[] frame={0x18,0x55,4,1,2,3,4,0};
   r.deliver(frame);r.deliver(frame);
   require((r.mem(0xa402,1)[0]&255)==8&&r.dword(0xa40e)==0xc02008,"duplicate fragment wasn't appended");cases++;
   require(r.pages.isEmpty(),"partial page committed");
   r.deliver(new byte[]{7,0,0,0,0,0,0,0});
   r.deliver(new byte[]{1,0,2,0,0,0,0,0});
   require((r.mem(0xa402,1)[0]&255)==8&&(r.mem(0xfd00,1)[0]&1)!=0,"session negotiation discarded partial page");cases++;
   r.address(0xc02080);r.deliver(frame);
   require((r.mem(0xa402,1)[0]&255)==12&&r.dword(0xa428)==0xc02000,"address setter replaced active page snapshot");cases++;
   r.resident();require((r.mem(0xa402,1)[0]&255)==0&&r.pages.isEmpty(),"partial state survived reinitialization");cases++;
   require(r.deliver(frame)==null,"reinitialized state accepted program without session");cases++;
  }
  try(Run r=new Run()) {
   r.session();r.address(0xc02001);
   byte[] reply=r.deliver(new byte[]{0x18,0,4,0,0,0,0,0});require((reply[1]&255)==0x33&&r.pages.isEmpty(),"unaligned page accepted");cases++;
   r.address(0xc02000);reply=r.deliver(new byte[]{0x18,0,6,0,0,0,0,0});require((reply[1]&255)==0x33&&r.pages.isEmpty(),"oversized initial fragment accepted");cases++;
   reply=r.deliver(new byte[]{0x55,0,0,0,0,0,0,0});require((reply[1]&255)==0x30,"unknown command response");cases++;
   reply=r.deliver(new byte[]{2,0,2,0,0,0,0,0});require((reply[1]&255)==0x32,"invalid address slot accepted");cases++;
  }
  try(Run r=new Run()) {
   r.session();r.address(0xc02000);
   r.deliver(new byte[]{0x18,0,5,1,2,3,4,5});
   require((r.mem(0xa402,1)[0]&255)==5&&r.dword(0xa40e)==0xc02004,"five-byte fragment/address asymmetry");cases++;
   for(int i=1;i<25;i++)r.deliver(new byte[]{0x18,0,5,1,2,3,4,5});
   byte[] reply=r.deliver(new byte[]{0x18,0,4,1,2,3,4,0});
   require((reply[1]&255)==0x32&&(r.mem(0xa402,1)[0]&255)==125&&r.pages.isEmpty(),"overflow altered page");cases++;
  }
  try(Run r=new Run()) {
   r.session();r.address(0xc02000);r.deliver(new byte[]{0x18,0,0,0,0,0,0,0});
   require((r.mem(0xa402,1)[0]&255)==0&&r.dword(0xa40e)==0xc02004,"zero-count address progression");cases++;
  }
  try(Run r=new Run()) {
   r.session();byte[] first=r.crc(0xc02000,4);byte[] repeated=r.deliver(new byte[]{14,0x33,4,0,0,0,0,0});
   require(Arrays.equals(first,repeated)&&r.dword(0xa40e)==0xc02000,"CRC retry changed state/result");cases++;
   r.deliver(new byte[]{7,0,0,0,0,0,0,0});
   require(r.deliver(new byte[]{1,0x11,2})!=null&&(r.mem(0xa404,1)[0]&32)!=0,"short negotiation with stale zero high byte");cases++;
   r.address(0xc02000);
   byte[] erase=r.deliver(new byte[]{0x10,0,0,0x20,0,0,0,0});
   require(erase!=null&&r.sectors.equals(Arrays.asList(0xc02000L,0xc03000L)),"modeled erase sector plan");cases++;
  }
  for(byte[] bad:new byte[][]{{2,0x33,(byte)0x96,0x69,0,0,0,0},{1,0x33,0,0x69,0,0,0,0}}) {
   try(Run r=new Run()) {
    r.frame(bad);r.invoke(0xc002e4+delta,1000000);
    require((r.reg("PSW")&64)==0&&r.tx==0,"invalid startup accepted");cases++;
   }
  }
  try(Run r=new Run()) {
   byte[] synthetic=new byte[32];for(int n=0;n<synthetic.length;n++)synthetic[n]=(byte)(n*3+7);
   r.put(0xc02000,synthetic); // Emulator memory only; input file remains untouched.
   List<String> trace=new ArrayList<>();
   trace.add(traceExchange(r,new byte[]{1,0,(byte)0x96,0x69,0,0,0,0},true));
   trace.add(traceExchange(r,new byte[]{1,1,2,0,0,0,0,0},false));
   trace.add(traceExchange(r,new byte[]{2,2,0,0,0,0x20,(byte)0xc0,0},false));
   trace.add(traceExchange(r,new byte[]{14,3,4,0,0,0,0,0},false));
   byte[] old=r.reply().clone();CRC32 expected=new CRC32();expected.update(synthetic,0,4);
   require(r.dword(0x201034)==expected.getValue(),"synthetic first CRC");cases++;
   trace.add(traceExchange(r,new byte[]{2,4,0,0,4,0x20,(byte)0xc0,0},false));
   byte[] addressReply=r.reply().clone();
   require(addressReply[3]==4&&Arrays.equals(Arrays.copyOfRange(old,4,8),Arrays.copyOfRange(addressReply,4,8)),"address reply did not retain previous CRC");cases++;
   trace.add(traceExchange(r,new byte[]{14,5,4,0,0,0,0,0},false));
   expected.reset();expected.update(synthetic,4,4);
   require(r.dword(0x201034)==expected.getValue(),"synthetic second CRC");
   trace.add(traceExchange(r,new byte[]{14,3,4,0,0,0,0,0},false));
   byte[] reused=r.reply();
   require(Arrays.equals(Arrays.copyOf(old,4),Arrays.copyOf(reused,4))&&!Arrays.equals(old,reused),"reused token collision not demonstrated");cases++;
   String payloadHash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(synthetic));
   println("SYNTHETIC_TRACE={\"kind\":\"synthetic-pcode-model\",\"payload_sha256\":\""+payloadHash+"\",\"rows\":["+String.join(",",trace)+"]}");
  }
  try(Run r=new Run()) {
   r.session();r.address(0xc02000);r.address(0xc02000);
   require(r.dword(0xa40e)==0xc02000,"identical address retry changed slot");cases++;
   r.address(0xc02004);r.address(0xc02000);
   require(r.dword(0xa40e)==0xc02000,"late old address was deduplicated");cases++;
  }
  try(Run r=new Run()) {
   r.session();
   byte[] first={2,0x41,0,0,0,0x20,(byte)0xc0,0};
   byte[] replacement={2,0x42,0,0,4,0x20,(byte)0xc0,0};
   r.frame(first);
   long end=r.steps+10000;
   // Pause after the first frame's token was copied into the RAM response.
   while(r.pc()!=0xc0063eL+delta&&r.steps<end)require(r.step(),"early root return");
   require(r.pc()==0xc0063eL+delta,"token-copy boundary not reached");
   r.frame(replacement);r.idle();
   require(r.reply()[2]==0x41&&r.reply()[1]==0&&r.dword(0xa40e)==0xc02004,
    "live RX replacement did not expose mixed token/address");cases++;
   println("RX_INTERLEAVING=synthetic-full-object-replacement-after-token-copy; "
    +"accepted-token=41 selected-address=c02004; no peripheral timing claim");
  }
  println("PASS cases="+cases+"; raw firmware p-code with address/call shims; CAN timing and flash effects modeled only");
 }
}
