/* Exact-image reset preparation boundaries with synthetic RAM and skipped callees.
 * Helpers are NOT treated as executed: root-call arguments are recorded and r4
 * results injected. Two helper prefixes execute only until their first nested call.
 * Stops before reset-tail NOP/SRST. No peripheral transaction or reset occurs.
 * @category openm74
 */
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.program.model.listing.Instruction;
import java.security.MessageDigest;
import java.util.*;

public class CheckResetPreparation extends EmulateCanLoader {
 final Set<Long> roots=Set.of(0xc1055eL,0xc12f10L,0xc4577cL,0xc45720L,0xc10000L);
 void decode(long start,long end) throws Exception {
  clearListing(toAddr(start),toAddr(end-1));
  for(long at=start;at<end;) {
   new DisassembleCommand(toAddr(at),null,false).applyTo(currentProgram,monitor);
   Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"decode gap");at+=ins.getLength();
  }
 }
 Run fixture(int gate) throws Exception {
  Run r=new Run();r.put(0,new byte[65536]);r.put(0xe00000,new byte[0x4000]);
  r.put(0x204000,new byte[0x200]);r.word(0xff1e,65535);r.word(0xff1c,0);
  r.cpu.writeRegister("DPP0",0x321);r.cpu.writeRegister("DPP1",0x320);
  r.cpu.writeRegister("DPP2",0x322);r.cpu.writeRegister("DPP3",3);
  r.cpu.writeRegister("r0",0xd800);r.cpu.writeRegister("r12",0x90);
  r.word(0xfd1e,gate);r.word(0xf0b8,0x3000);r.pc(0xc0d572);
  r.cpu.enableMemoryWriteTracking(true);return r;
 }
 List<String> root(Run r,int result,long stop) throws Exception {
  List<String> calls=new ArrayList<>();
  while(r.steps<10000) {
   long at=r.pc();if(at==stop)return calls;
   Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"undecoded root");
   require(!ins.getMnemonicString().equalsIgnoreCase("srst"),"reset reached");
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect root call");long target=ins.getFlows()[0].getOffset();
    require(roots.contains(target),"unknown root callee");
    String args=Long.toHexString(target)+":"+Long.toHexString(r.reg("r12"));
    if(target==0xc1055e||target==0xc12f10)args+=":"+Long.toHexString(r.reg("r13"));
    calls.add(args);r.cpu.writeRegister("r4",result);r.pc(at+ins.getLength());r.steps++;
   } else require(r.step(),"root returned");
  }
  throw new IllegalStateException("root budget");
 }
 int packedCount=-1;
 long prefix(Run r,long entry) throws Exception {
  r.pc(entry);long end=r.steps+20000;
  while(r.steps<end) {
   if(r.pc()==0xc120a8)packedCount=(int)r.reg("r6");
   if(r.pc()==0xc13154)packedCount=(int)r.reg("r9");
   Instruction ins=getInstructionAt(toAddr(r.pc()));require(ins!=null,"undecoded prefix");
   if(ins.getFlowType().isCall())return r.pc();
   require(!ins.getMnemonicString().equalsIgnoreCase("srst"),"prefix reset");
   require(r.step(),"prefix returned before expected boundary");
  }
  throw new IllegalStateException("helper prefix budget");
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size==851968,"image size");byte[] raw=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"unreviewed image");
  for(int d=0;d<2;d++) {
   int base=d==0?0x8169c:0x81864,total=0,count=0;
   for(int n=1;n<1000;n++) {
    int length=raw[base+n*8+4]&255;if(length==0)break;total+=length;count++;
    require(n<999,"descriptor budget");
   }
   require(total==(d==0?315:514)&&count==(d==0?52:58),"descriptor geometry");
  }
  println("IMAGE_SHA256="+hash);decode(0xc0d572,0xc0d6be);
  decode(0xc1055e,0xc12492);decode(0xc12f10,0xc132cc);decode(0xc031fc,0xc0320c);int cases=0;
  require((raw[0x80a52]&255)==1,"stock selector byte");
  for(int gate:new int[]{0,16})for(int result:new int[]{0,3,65535})try(Run r=fixture(gate)) {
   List<String> calls=root(r,result,0xc0d6b6);
   List<String> expected=gate==0?Arrays.asList("c4577c:1","c10000:64","c45720:1","c10000:a"):
    Arrays.asList("c1055e:1:569c","c12f10:1:5864","c10000:a");
   require(calls.equals(expected),"root call arguments "+calls);
   require(r.dword(0xe02406)%65536==0x2222,"marker");
   require(r.cpu.getTrackedMemoryWriteSet().contains(toAddr(0xe02406)),"write tracker missed marker");
   require(!r.cpu.getTrackedMemoryWriteSet().intersects(toAddr(0xf0b8),toAddr(0xf0bb)),"root reset-register write");
   require((r.mem(0xf0b8,2)[0]&255)==0&&(r.mem(0xf0b8,2)[1]&255)==0x30,"root reset config");cases++;
  }
  int[][] checksums=new int[2][2];
  for(int helper=0;helper<2;helper++)for(int pattern=0;pattern<2;pattern++)try(Run r=fixture(16)) {
   root(r,0,helper==0?0xc0d68c:0xc0d696);
   long buffer=helper==0?0xb22c:0xb00c;int bound=helper==0?320:528,copied=helper==0?315:514;
   byte[] scratch=new byte[bound];Arrays.fill(scratch,pattern==0?(byte)0:(byte)0xa5);
   r.put(buffer,scratch);packedCount=-1;
   long stop=prefix(r,helper==0?0xc1055e:0xc12f10);
   require(stop==(helper==0?0xc120dc:0xc1317e),"helper boundary "+Long.toHexString(stop));
   require(packedCount==copied,"packed count="+packedCount);
   for(int n=copied;n<bound;n++)require((r.mem(buffer+n,1)[0]&255)==(pattern==0?0:165),"tail cleared");
   if(helper==0) {
    require(r.reg("r9")==336,"first bank");
    for(int n=0;n<8;n++)require((r.mem(buffer+n,1)[0]&255)==(n%2==0?0:128),"root words not packed");
    checksums[helper][pattern]=(r.mem(r.reg("r0")+2,1)[0]&255)|((r.mem(r.reg("r0")+3,1)[0]&255)<<8);
   } else checksums[helper][pattern]=(int)r.reg("r9");
   int sum=0;for(byte v:r.mem(buffer,bound))sum+=v&255;
   require(checksums[helper][pattern]==(sum&65535),"checksum does not cover full bound");cases++;
  }
  require(((checksums[0][1]-checksums[0][0])&65535)==5*165,"first tail contribution");
  require(((checksums[1][1]-checksums[1][0])&65535)==14*165,"second tail contribution");
  try(Run r=fixture(16)) {
   r.cpu.writeRegister("r12",1);require(root(r,0,0xc0d6b6).isEmpty(),"non-preparation call");
   require(r.dword(0xe02406)==0,"non-preparation marker changed");cases++;
  }
  for(int gate:new int[]{0,16})for(int config:new int[]{0,1})
   for(int result:new int[]{0,1,255,256})try(Run r=fixture(gate)) {
    // Contrafactual config changes only emulator memory, never the input image.
    r.put(0xc80a52,new byte[]{(byte)config});r.cpu.writeRegister("r4",result);r.pc(0xc031fc);
    long end=r.steps+50;
    while(r.pc()!=0xc0320c&&r.steps<end)require(r.step(),"producer returned");
    require(r.pc()==0xc0320c,"producer budget");
    require(((r.mem(0xfd1e,1)[0]&16)!=0)==((result&255)!=0&&config==0),"producer condition");cases++;
   }
  println("PREPARATION_RESULT={\"cases\":"+cases+",\"root_status_cases\":6,\"helper_prefixes\":4,\"non_preparation_cases\":1,\"packed_lengths\":[315,514],\"processed_lengths\":[320,528],\"tail_lengths\":[5,14],\"descriptor_records\":[52,58],\"flag_producer_cases\":16,\"stock_config_value\":1}");
  println("PASS reset-preparation boundaries; skipped callees, no SRST or device effects");
 }
}
