/* Alternative-backend caller with injected nested read results and table contents.
 * Application default code executes. No serial transaction, complete nested
 * restore, full startup, reset, flash operation, timing or ECU I/O.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
import ghidra.program.model.listing.Instruction;
public class CheckAlternativeStartup extends CheckResetPreparation {
 void scenario(Run r,int first,int second,int pattern) throws Exception {
  for(int n=0;n<82;n++)r.word(0xe03000+2*n,65535);
  if(pattern!=0)r.word(0xe03000+2*(pattern==1?0:81),0);
  r.word(0xfd1e,16);r.word(0xfd66,0xffff);r.word(0xfd5a,0xffff);
  r.pc(0xc03218);long end=r.steps+30000;int reads=0,defaults=0;boolean boundary=false;
  while(r.steps<end) {
   long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"decode");
   if(at==0xc0321e)require(r.reg("r4")== (first==1&&second==1?1:6),"alternative caller result");
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect call");long target=ins.getFlows()[0].getOffset();
    if(target==0xc1055e) {
     require(at==0xc0322c&&r.reg("r12")==2&&r.reg("r13")==0x55d4,"next alternative arguments");boundary=true;break;
    }
    if(target==0xc12f10||target==0xc132cc) {
     require(r.reg("r12")==2,"nested restore mode");
     if(target==0xc12f10)require(reads==0&&r.reg("r13")==0x5864,"first descriptor");
     else require(reads==1,"second restore order");
     r.cpu.writeRegister("r4",reads++==0?first:second);r.pc(at+ins.getLength());r.steps++;continue;
    }
    if(target==0xc0272c){r.pc(at+ins.getLength());r.steps++;continue;}
    if(target==0xc08560)defaults++;
    require(Set.of(0xc13550L,0xc08560L,0xc081f0L,0xc5ea16L).contains(target),"unreviewed call");
   }
   require(r.step(),"unexpected caller return");
  }
  require(boundary&&reads==2&&defaults==(pattern==0?1:0),"sentinel/default sequence");
  require((r.dword(0xfd1e)&16)!=0,"selector changed");
  require(!r.cpu.getTrackedMemoryWriteSet().intersects(toAddr(0xfd1e),toAddr(0xfd1f)),"selector write in executed prefix");
  if(pattern==0) {
   require((r.dword(0xfd66)&0x4000)==0&&(r.dword(0xfd5a)&8)==0,"default caller flag clears");
   require((r.mem(0xe01183,1)[0]&255)==1,"application default marker");
   require((r.dword(0xe01412)&65535)==(getShort(toAddr(0xc8129c))&65535),"ROM default load");
  }
 }
 void nestedRead(Run r,int fill) throws Exception {
  r.word(0xfd1e,16);r.cpu.writeRegister("r12",2);r.pc(0xc132cc);
  int reads=0;long end=r.steps+200000;boolean returned=false;
  while(r.steps<end) {
   long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"nested read decode");
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect nested read");long target=ins.getFlows()[0].getOffset();
    if(target==0xc101f8) {
     require(r.reg("r12")==0x540+16*reads&&r.reg("r13")==16,"serial chunk arguments");
     byte[] chunk=new byte[16];Arrays.fill(chunk,(byte)fill);r.put(0xe023eb,chunk);
     reads++;r.pc(at+ins.getLength());r.steps++;continue;
    }
    require(target==0xc13eb8||target==0xc135fe,"unreviewed nested read call "+Long.toHexString(target));
   }
   if(!r.step()){returned=true;break;}
  }
  require(returned&&reads==34&&r.reg("r4")==0,"nested read completion");
  for(byte value:r.mem(0xb00c,544))require((value&255)==fill,"gathered serial bytes");
  int fields=0,fieldBytes=0;
  for(int n=1;n<100;n++) {
   long at=0xc81a44+8*n;int length=getByte(toAddr(at+4))&255;if(length==0)break;
   long destination=((long)(getShort(toAddr(at+2))&65535)<<14)|(getShort(toAddr(at))&0x3fff);
   require(destination>=0xe00000&&destination+length<=0xe04000,"serial field RAM");
   for(byte value:r.mem(destination,length))require((value&255)==fill,"restored serial field");
   fields++;fieldBytes+=length;
  }
  require(fields==42&&fieldBytes==45,"serial field descriptor geometry");
  require((r.dword(0xfd1e)&16)!=0&&!r.cpu.getTrackedMemoryWriteSet().intersects(toAddr(0xfd1e),toAddr(0xfd1f)),"nested read selector write");
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();require(size==851968,"size");
  byte[] raw=new byte[(int)size];require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");println("IMAGE_SHA256="+hash);
  decode(0xc03218,0xc03230);decode(0xc13550,0xc135fe);decode(0xc081f0,0xc08566);decode(0xc5ea16,0xc5ea3c);decode(0xc132cc,0xc13550);decode(0xc135fe,0xc13794);decode(0xc13eb8,0xc13ffc);
  int cases=0;
  for(int first:new int[]{0,1,3,65535})for(int second:new int[]{0,1,3,65535})for(int pattern=0;pattern<3;pattern++)try(Run r=fixture(0)) {
   // Exclude setup writes from the selector-write audit.
   r.word(0xfd1e,16);r.cpu.enableMemoryWriteTracking(false);r.cpu.enableMemoryWriteTracking(true);
   scenario(r,first,second,pattern);cases++;
  }
  for(int fill:new int[]{0,255})try(Run r=fixture(0)) {nestedRead(r,fill);cases++;}
  println("ALTERNATIVE_RESULT={\"cases\":"+cases+",\"return_pairs\":16,\"table_patterns\":3,\"sentinel_words\":82,\"nested_read_cases\":2,\"nested_read_bytes\":544,\"field_records\":42,\"field_bytes\":45}");
  println("PASS alternative startup; injected nested reads and actual application defaults");
 }
}
