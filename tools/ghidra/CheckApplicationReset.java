/* Check reviewed I444GI04 application-reset branches, without executing SRST.
 * Raw import at C00000, -readOnly -noanalysis. No device or peripheral model.
 * Reset-prefix and diagnostic-parser p-code uses modeled registers/RAM/addressing.
 * Reset preparation stops at entry; helpers are surveyed statically only.
 * @category openm74
 */
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.emulator.EmulatorHelper;
import ghidra.pcode.emulate.BreakCallBack;
import ghidra.pcode.memstate.MemoryState;
import ghidra.pcode.pcoderaw.PcodeOpRaw;
import java.util.regex.*;
import ghidra.program.model.listing.Instruction;
import java.security.MessageDigest;
import java.util.*;

public class CheckApplicationReset extends GhidraScript {
 void require(boolean ok,String message){if(!ok)throw new IllegalStateException(message);}
 Instruction decode(long address) throws Exception {
  new DisassembleCommand(toAddr(address),null,false).applyTo(currentProgram,monitor);
  Instruction i=getInstructionAt(toAddr(address));require(i!=null,"decode failed");return i;
 }
 String survey(long entry,long end) throws Exception {
  clearListing(toAddr(entry),toAddr(end-1));
  Deque<Long> pending=new ArrayDeque<>();Set<Long> visited=new TreeSet<>();
  Set<Long> targets=new TreeSet<>();int callSites=0,returns=0;pending.add(entry);
  while(!pending.isEmpty()) {
   long at=pending.removeFirst();if(!visited.add(at))continue;
   require(visited.size()<=10000&&at>=entry&&at<end,"flow escaped reviewed helper window");
   Instruction i=decode(at);String mnemonic=i.getMnemonicString().toLowerCase();
   if(i.getFlowType().isCall()) {
    require(i.getFlows().length==1,"indirect helper call");
    targets.add(i.getFlows()[0].getOffset());callSites++;
   } else if(i.getFlowType().isJump()) {
    require(i.getFlows().length>0,"unresolved helper jump");
    for(var target:i.getFlows())pending.add(target.getOffset());
   }
   if(mnemonic.startsWith("ret")){returns++;continue;}
   if(i.getFallThrough()!=null)pending.add(i.getFallThrough().getOffset());
  }
  StringJoiner calls=new StringJoiner(",");
  for(long target:targets)calls.add("\""+String.format("%06x",target)+"\"");
  return "{\"entry\":\""+String.format("%06x",entry)+"\",\"instructions\":"+visited.size()
   +",\"returns\":"+returns+",\"call_sites\":"+callSites+",\"direct_callees_not_followed\":["+calls+"]}";
 }
 void expect(long at,String text) throws Exception {
  clearListing(toAddr(at),toAddr(at+3));
  require(decode(at).toString().equalsIgnoreCase(text),"reviewed instruction changed at "+Long.toHexString(at));
 }
 // Register/RAM-only parsers. DPP0..3 are explicitly modeled as identity pages;
 // EXTP addressing uses the same reviewed shims as the loader harness. No device I/O.
 class ParserRun implements AutoCloseable {
  EmulatorHelper cpu=new EmulatorHelper(currentProgram);long page=-1;int left=0;
  ParserRun() throws Exception {
   cpu.writeMemory(toAddr(0),new byte[0x10000]);
   cpu.writeMemory(toAddr(0xe00000),new byte[0x4000]);
   for(int n=0;n<16;n++)cpu.writeRegister("r"+n,0);
   for(int n=0;n<4;n++)cpu.writeRegister("DPP"+n,n);
   cpu.writeRegister("PSW",0);cpu.writeRegister("CSP",0xc0);
   put(0xff1e,new byte[]{(byte)255,(byte)255});
   cpu.registerCallOtherCallback("segment",new Translate(false,false));
   cpu.registerCallOtherCallback("c166_reg_offset_addr",new Translate(true,false));
   cpu.registerCallOtherCallback("GetPagedOffset",new Translate(false,true));
   cpu.registerCallOtherCallback("c166_switch_load",new BreakCallBack(){
    public boolean pcodeCallback(PcodeOpRaw op) {
     MemoryState st=emulate.getMemoryState();long offset=st.getValue(op.getInput(1))&65535;
     long physical=left>0?(page<<14)|(offset&0x3fff):
      ((st.getValue(currentProgram.getRegister("DPP"+(offset>>14)))&0x3ff)<<14)|(offset&0x3fff);
     st.setValue(op.getOutput(),st.getValue(currentProgram.getAddressFactory().getDefaultAddressSpace(),physical,2));
     return true;
    }
   });
  }
  class Translate extends BreakCallBack {
   boolean sum,single;Translate(boolean sum,boolean single){this.sum=sum;this.single=single;}
   public boolean pcodeCallback(PcodeOpRaw op) {
    MemoryState st=emulate.getMemoryState();
    long offset=single?st.getValue(op.getInput(1)):sum?
     st.getValue(op.getInput(1))+st.getValue(op.getInput(2)):st.getValue(op.getInput(2));
    offset&=65535;
    long physical=left>0?(page<<14)|(offset&0x3fff):
     ((st.getValue(currentProgram.getRegister("DPP"+(offset>>14)))&0x3ff)<<14)|(offset&0x3fff);
    st.setValue(op.getOutput(),physical);return true;
   }
  }
  void put(long at,byte[] bytes){cpu.writeMemory(toAddr(at),bytes);}
  int byteAt(long at){return cpu.readMemory(toAddr(at),1)[0]&255;}
  long execute(long entry,long... stops) throws Exception {
   cpu.writeRegister(cpu.getPCRegister(),entry);
   for(int steps=0;steps<3000;steps++) {
    long at=cpu.getExecutionAddress().getOffset();
    for(long stop:stops)if(at==stop)return at;
    Instruction i=getInstructionAt(toAddr(at));
    require(i!=null,"undecoded parser instruction");String m=i.getMnemonicString().toLowerCase();
    if(m.equals("rets"))return at;
    require(!i.getFlowType().isCall()&&!m.equals("srst"),"unexpected parser call/reset");
    boolean extension=m.equals("extp");
    if(extension) {
     Matcher match=Pattern.compile("extp\\s+(\\S+),\\s*#?(0x[0-9a-f]+|\\d+)")
      .matcher(i.toString().toLowerCase());require(match.find(),"unparsed EXTP");
     String value=match.group(1).replace("#","");var register=currentProgram.getRegister(value);
     page=(register==null?Long.decode(value):cpu.readRegister(register).longValue())&0x3ff;
     left=Integer.decode(match.group(2));
    }
    require(cpu.step(monitor),"parser p-code failure: "+cpu.getLastError());
    if(!extension&&left>0)left--;
   }
   throw new IllegalStateException("parser instruction budget exceeded");
  }
  public void close(){cpu.dispose();}
 }
 String checkAdmission() throws Exception {
  for(long[] range:new long[][]{{0xc602b4,0xc602e0},{0xc48478,0xc4847e},
    {0xc679c4,0xc679ce}}) {
   clearListing(toAddr(range[0]),toAddr(range[1]-1));
   for(long at=range[0];at<range[1];){Instruction i=decode(at);at+=i.getLength();}
  }
  int entryCases=0,afterLengthCases=0;
  for(int state=0;state<256;state++) {
   try(ParserRun r=new ParserRun()) {
    r.put(0xc704,new byte[]{(byte)state});
    long stop=r.execute(0xc602b4,0xc602c4,0xc607a4);
    require(stop==(state==255?0xc607a4:0xc602c4),"entry admission mismatch");entryCases++;
   }
   if(state==255)continue;
   try(ParserRun r=new ParserRun()) {
    // The getter is not called here: inject its result to isolate the later gate.
    r.put(0xc704,new byte[]{(byte)state});r.cpu.writeRegister("r4",2);
    long stop=r.execute(0xc602c8,0xc602dc,0xc60792);
    byte[] length=r.cpu.readMemory(toAddr(0xe01656),2);
    require((length[0]&255)==2&&length[1]==0,"length result not stored");
    require(stop==(state==0?0xc602dc:0xc60792),"post-length admission mismatch");afterLengthCases++;
   }
  }
  return "{\"entry_cases\":"+entryCases+",\"post_length_cases\":"+afterLengthCases
   +",\"state_cell\":\"00c704\",\"length_cell\":\"e01656\","
   +"\"scope\":\"Synthetic state byte and injected length result; stopped before calls or alternate branches; no reception, callees or full dispatcher execution\"}";
 }
 byte[] firstFrame(boolean extended,int argument,int mutation) {
  byte[] frame=extended?new byte[]{(byte)0x80,0x10,(byte)0xf1,2,0x11,(byte)argument,0}:
   new byte[]{(byte)0x82,0x10,(byte)0xf1,0x11,(byte)argument,0};
  if(mutation==1)frame[1]=0x12;
  if(mutation==2)frame[2]=(byte)0xf2;
  if(mutation==3)frame[extended?4:3]=0x12;
  if(mutation==4) {
   frame=Arrays.copyOf(frame,frame.length+1);frame[frame.length-2]=0;
   frame[extended?3:0]=extended?3:(byte)0x83;
  }
  int sum=0;for(int n=0;n<frame.length-1;n++)sum+=frame[n]&255;
  frame[frame.length-1]=(byte)(sum+(mutation==5?1:0));return frame;
 }
 String checkParsers() throws Exception {
  for(long[] range:new long[][]{{0xc57f8e,0xc5808c},{0xc666dc,0xc6671a}}) {
   clearListing(toAddr(range[0]),toAddr(range[1]-1));
   for(long at=range[0];at<range[1];){Instruction i=decode(at);at+=i.getLength();}
  }
  int firstCases=0,firstAccepted=0,secondCases=0,secondAccepted=0,negative=0;
  for(boolean extended:new boolean[]{false,true})for(int argument=0;argument<256;argument++) {
   try(ParserRun r=new ParserRun()) {
    r.put(0xe01212,firstFrame(extended,argument,0));r.execute(0xc57f8e);
    boolean accepted=r.byteAt(0xde7b)==1;
    require(accepted==(argument==1||argument==0x90),"first parser acceptance");
    require(r.byteAt(0xde64)==argument,"first parser argument capture");
    if(accepted)firstAccepted++;firstCases++;
   }
  }
  for(int argument=0;argument<256;argument++) {
   try(ParserRun r=new ParserRun()) {
    r.put(0xfa54,new byte[]{0x11,(byte)argument});r.execute(0xc666dc);
    boolean accepted=r.byteAt(0xdef9)==1;
    require(accepted==(argument==1||argument==0x90||argument==0x91),"second parser acceptance");
    require(r.byteAt(0xdee9)==argument,"second parser argument capture");
    if(accepted)secondAccepted++;secondCases++;
   }
  }
  for(boolean extended:new boolean[]{false,true})for(int mutation=1;mutation<=5;mutation++) {
   try(ParserRun r=new ParserRun()) {
    r.put(0xe01212,firstFrame(extended,0x90,mutation));r.execute(0xc57f8e);
    int expectedStatus=mutation==5?0x77:mutation==4?0x12:0x11;
    require(r.byteAt(0xde7b)==expectedStatus,"invalid first parser status");negative++;
   }
  }
  try(ParserRun r=new ParserRun()) {
   r.put(0xfa54,new byte[]{0x12,(byte)0x90});r.execute(0xc666dc);
   require(r.byteAt(0xdef9)==0x11,"wrong second service accepted");negative++;
  }
  return "{\"first_entry\":\"c57f8e\",\"first_cases\":"+firstCases
   +",\"first_accepted\":"+firstAccepted+",\"second_entry\":\"c666dc\",\"second_cases\":"+secondCases
   +",\"second_accepted\":"+secondAccepted+",\"negative_cases\":"+negative
   +",\"scope\":\"Synthetic RAM; modeled DPP and EXTP; no transport, dispatcher, authorization or reset execution\"}";
 }
 int imageWord(byte[] image,int offset){return (image[offset]&255)|((image[offset+1]&255)<<8);}
 byte[] initializedRouteRam(byte[] image) {
  int at=0x4000,records=0;byte[] result=null;boolean terminated=false;
  while(records<1000) {
   int tag=imageWord(image,at),words=tag==1||tag==7?6:tag==5?3:tag==6?5:0;
   if(words==0){require(tag==0&&records==233,"unexpected initializer termination");terminated=true;break;}
   if(tag==6) {
    long destination=((long)imageWord(image,at+4)<<14)|(imageWord(image,at+2)&0x3fff);
    long source=((long)imageWord(image,at+8)<<14)|(imageWord(image,at+6)&0x3fff);
    int length=imageWord(image,at+10);
    if(destination==0xc702) {
     require(result==null&&source==0xc499f8&&length==26,"reviewed route initializer");
     result=Arrays.copyOfRange(image,(int)(source-0xc00000),(int)(source-0xc00000)+length);
    }
   }
   at+=2+2*words;records++;
  }
  require(terminated&&result!=null,"route initializer missing or budget exhausted");return result;
 }
 void runTransport(ParserRun r,long entry) throws Exception {
  Deque<Long> stack=new ArrayDeque<>();r.cpu.writeRegister(r.cpu.getPCRegister(),entry);
  Set<Long> allowed=Set.of(0xc4771cL,0xc4816aL,0xc481d2L,0xc48556L,0xc4861eL,0xc49a12L,
    0xc49b1eL,0xc4860cL);
  for(int steps=0;steps<20000;steps++) {
   long at=r.cpu.getExecutionAddress().getOffset();Instruction i=getInstructionAt(toAddr(at));
   if(i==null)i=decode(at);String m=i.getMnemonicString().toLowerCase();
   if(m.equals("calls")) {
    long target=i.getFlows()[0].getOffset();require(allowed.contains(target),"unreviewed transport call "+Long.toHexString(target));
    stack.push(at+i.getLength());r.cpu.writeRegister(r.cpu.getPCRegister(),target);continue;
   }
   if(m.equals("rets")) {
    if(stack.isEmpty())return;r.cpu.writeRegister(r.cpu.getPCRegister(),stack.pop());continue;
   }
   // Execute one real instruction with the parser's reviewed EXTP/address shims.
   boolean extension=m.equals("extp");
   if(extension) {
    Matcher match=Pattern.compile("extp\\s+(\\S+),\\s*#?(0x[0-9a-f]+|\\d+)")
     .matcher(i.toString().toLowerCase());require(match.find(),"unparsed transport EXTP");
    String value=match.group(1).replace("#","");var register=currentProgram.getRegister(value);
    r.page=(register==null?Long.decode(value):r.cpu.readRegister(register).longValue())&0x3ff;
    r.left=Integer.decode(match.group(2));
   }
   require(!m.equals("srst")&&!m.equals("exts"),"unexpected transport reset/address extension");
   require(r.cpu.step(monitor),"transport p-code failure at "+Long.toHexString(at)+": "+r.cpu.getLastError());
   if(!extension&&r.left>0)r.left--;
  }
  throw new IllegalStateException("transport instruction budget");
 }
 void prepareTransport(ParserRun r,byte[] initialized,int id,byte[] frame) {
  r.cpu.writeRegister("DPP0",0x321);r.cpu.writeRegister("DPP1",0x320);
  r.cpu.writeRegister("DPP2",0x322);r.cpu.writeRegister("DPP3",3);
  r.cpu.writeRegister("r0",0xd800);r.put(0xc702,initialized);
  r.put(0xc650,new byte[]{1});r.put(0xc642,new byte[]{2,0});r.put(0xd000,frame);
  r.cpu.writeRegister("r12",0);r.cpu.writeRegister("r13",id);
  r.cpu.writeRegister("r14",8);r.cpu.writeRegister("r15",0xd000);
 }
 String checkCanTransport(byte[] image) throws Exception {
  byte[] initialized=initializedRouteRam(image);
  for(long[] range:new long[][]{{0xc4771c,0xc47734},{0xc477c8,0xc4785a},
    {0xc4816a,0xc48178},{0xc481d2,0xc48478},{0xc48556,0xc48584},{0xc4861e,0xc487d6},
    {0xc49a12,0xc49ac8},{0xc49b1e,0xc49b4c},{0xc4860c,0xc4861e},
    {0xc666dc,0xc6671a}}) {
   clearListing(toAddr(range[0]),toAddr(range[1]-1));
   for(long at=range[0];at<range[1];){Instruction i=decode(at);at+=i.getLength();}
  }
  expect(0xc02394,"mov 0xfe02,#0x320");
  expect(0xc4725e,"mov r11,#0x80");expect(0xc47264,"mov r2,[r13+#0x101a]");
  expect(0xc472dc,"shr r2,#0x2");expect(0xc47302,"calls 0xc477c8");
  expect(0xc477d6,"mov r2,[r12+#0x6c2e]");expect(0xc47836,"calls 0xc481d2");
  expect(0xc49b30,"movb 0xc704,RL1");
  int cases=0;
  for(int id:new int[]{0x7df,0x7e0})for(int argument:new int[]{1,0x90,0x91}) {
   try(ParserRun r=new ParserRun()) {
    prepareTransport(r,initialized,id,new byte[]{2,0x11,(byte)argument,0,0,0,0,0});
    runTransport(r,0xc477c8);
    require(r.byteAt(0xc704)==0,"receive result not success");
    require(r.byteAt(0xfa54)==0x11&&r.byteAt(0xfa55)==argument,"payload did not reach diagnostic buffer");
    require(r.byteAt(0xc674)==2&&r.byteAt(0xc675)==0,"reassembled length");
    r.execute(0xc666dc);require(r.byteAt(0xdef9)==1&&r.byteAt(0xdee9)==argument,"transport/parser chain");
    cases++;
   }
  }
  int negative=0;
  for(int id:new int[]{0x7df,0x7e0})for(int first:new int[]{0x40,8}) {
   try(ParserRun r=new ParserRun()) {
    prepareTransport(r,initialized,id,new byte[]{(byte)first,0x11,(byte)0x90,0,0,0,0,0});
    runTransport(r,0xc477c8);
    require(r.byteAt(0xc704)==255&&r.byteAt(0xfa54)==0,
     "invalid transport frame reached the diagnostic buffer");negative++;
   }
  }
  return "{\"cases\":"+cases+",\"negative_cases\":"+negative+",\"rx_ids\":[2015,2016],\"buffer\":\"00fa54\","
   +"\"initializer_record\":\"c049a2\",\"scope\":\"Synthetic frame at software receive dispatch; real copy/parser p-code, CALLS/RETS shims, modeled RAM/DPP/controller state; no CAN peripheral, arbitration, authorization, post-processing or reset execution\"}";
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size==851968,"unexpected image size");byte[] image=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),image)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(image));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"unreviewed image");
  println("IMAGE_SHA256="+hash);
  clearListing(toAddr(0xc0d572),toAddr(0xc0d6bd));
  for(long at=0xc0d572;at<0xc0d6be;){Instruction i=decode(at);at+=i.getLength();}
  require(getInstructionAt(toAddr(0xc0d6b8)).getMnemonicString().equalsIgnoreCase("srst"),"reset endpoint");
  require(getInstructionAt(toAddr(0xc0d6b6)).getMnemonicString().equalsIgnoreCase("nop"),"reset-tail NOP");
  int cases=0,prepared=0,direct=0;
  for(int high:new int[]{0,1,128,255})for(int low=0;low<256;low++) {
   EmulatorHelper cpu=new EmulatorHelper(currentProgram);
   try {
    cpu.writeRegister("r12",(high<<8)|low);cpu.writeRegister("r1",0);
    cpu.writeRegister("PSW",0);cpu.writeRegister("CSP",0xc0);
    cpu.writeRegister(cpu.getPCRegister(),0xc0d572);
    long pc=0;
    for(int steps=0;steps<8;steps++) {
     pc=cpu.getExecutionAddress().getOffset();
     if(pc==0xc0d57c||pc==0xc0d6b6)break;
     require(cpu.step(monitor),"p-code failure: "+cpu.getLastError());
    }
    require(pc==(low==0x90?0xc0d57c:0xc0d6b6),"unexpected reset branch");
    if(low==0x90)prepared++;else direct++;
    cases++;
   } finally {cpu.dispose();}
  }
  // Raw aligned CALLS candidates are reported as candidates, not all possible xrefs.
  List<Long> calls=new ArrayList<>();
  for(int n=0;n+4<=image.length;n+=2)
   if((image[n]&255)==0xda&&(image[n+1]&255)==0xc0
     &&(image[n+2]&255)==0x72&&(image[n+3]&255)==0xd5)calls.add(0xc00000L+n);
  List<Long> expected=Arrays.asList(0xc507d2L,0xc5d208L,0xc5d28aL,0xc6050eL,0xc6aa1aL,0xc6aaacL,0xc6ab6cL);
  require(calls.equals(expected),"direct-call candidate set changed");
  for(long at:calls) {
   clearListing(toAddr(at),toAddr(at+3));Instruction i=decode(at);
   require(i.getMnemonicString().equalsIgnoreCase("calls")&&i.getFlows().length==1
    &&i.getFlows()[0].getOffset()==0xc0d572,"call candidate decode mismatch");
  }
  // Full-image opcode occurrences do not establish executable reachability.
  List<String> resets=new ArrayList<>();
  for(int n=0;n+4<=image.length;n+=2)
   if((image[n]&255)==0xb7&&(image[n+1]&255)==0x48
     &&(image[n+2]&255)==0xb7&&(image[n+3]&255)==0xb7)resets.add(String.format("%06x",0xc00000+n));
  StringJoiner sites=new StringJoiner(",");for(long at:calls)sites.add("\""+String.format("%06x",at)+"\"");
  StringJoiner opcodes=new StringJoiner(",");for(String at:resets)opcodes.add("\""+at+"\"");
  StringJoiner helpers=new StringJoiner(",");
  for(long[] range:new long[][]{{0xc1055e,0xc12492},{0xc12f10,0xc132cc},
    {0xc4577c,0xc459fa},{0xc45720,0xc4577c},{0xc10000,0xc10030}})
   helpers.add(survey(range[0],range[1]));
  expect(0xc0d662,"mov r13,#0x2222");expect(0xc0d66e,"extp 0x380, #0x1");
  expect(0xc0d672,"mov 0x2406,r13");
  expect(0xc033b0,"extp 0x380, #0x1");expect(0xc033b4,"mov r8,0x2406");
  expect(0xc033b8,"cmp r8,#0x55aa");expect(0xc033bc,"jmpr cc_NE,0xc03488");
  expect(0xc0610c,"mov r6,#0x55aa");expect(0xc06114,"mov 0x2406,r6");
  expect(0xc08232,"mov 0x2406,0xff1c");
  expect(0xc5028c,"movbz r12,0xde7e");expect(0xc50294,"extp 0x380, #0x1");
  expect(0xc50298,"movb RL2,[r12+#0x1212]");expect(0xc5029c,"movb 0xde64,RL2");
  expect(0xc58046,"movb RL3,[r12+#0x1212]");
  expect(0xc58050,"subb 0xdea8,0xff1e");expect(0xc58060,"movb 0xde64,RL3");
  expect(0xc666ea,"extp 0x3, #0x1");expect(0xc666ee,"movb RL2,0x3a55");
  expect(0xc666f2,"movb 0xdee9,RL2");
  expect(0xc505be,"calls 0xc5d48a");expect(0xc5d48a,"calls 0xc57f8e");
  expect(0xc603ea,"calls 0xc6ad5e");expect(0xc6ad5e,"calls 0xc666dc");
  expect(0xc55cd6,"cmpb RL3,#0x83");expect(0xc55d54,"calls 0xc5028a");
  expect(0xc48478,"mov r4,0xc674");expect(0xc679c8,"movb RL4,0x3a54");
  expect(0xc602dc,"calls 0xc679c4");
  expect(0xc50442,"bcmp 0xfdea.0x8,0xfdea.0x1");
  expect(0xc50446,"jmpa- cc_NN,0xc50c5c");
  String canTransport=checkCanTransport(image);
  String admission=checkAdmission();
  String parsers=checkParsers();
  println("RESET_RESULT={\"cases\":"+cases+",\"preparation_entry\":"+prepared
   +",\"direct_reset_tail\":"+direct+",\"aligned_direct_call_candidates\":["+sites
   +"],\"aligned_srst_opcode_occurrences\":["+opcodes+"],\"static_preparation_helpers\":["+helpers
   +"],\"can_transport\":"+canTransport+",\"admission_gates\":"+admission+",\"diagnostic_parsers\":"+parsers+",\"checked_marker_cell\":\"e02406\",\"checked_reset_parameter_producers\":[\"c58060\",\"c666f2\"]}");
  println("PASS application-reset cases="+cases+"; stopped at reset-tail NOP or preparation; no peripheral execution");
 }
}
