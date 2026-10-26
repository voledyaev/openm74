/* Exact descriptor read with generated serial contents; numeric helpers execute.
 * Serial transfers are replaced at their call boundary; no device I/O or timing.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
import ghidra.program.model.listing.Instruction;
public class CheckAlternativeDescriptor extends CheckResetPreparation {
 void connected(Run r,int fill,boolean corrupt) throws Exception {
  r.word(0xfd1e,16);r.pc(0xc03218);int metadata=fill*528&65535;if(corrupt)metadata^=1;
  int reads=0,defaults=0;long end=r.steps+400000;boolean boundary=false;
  while(r.steps<end) {
   long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"connected decode");
   if(at==0xc1356a)require(r.reg("r4")== (corrupt?6:1),"first actual nested result");
   if(at==0xc13576)require(r.reg("r4")==0,"second actual nested result");
   if(at==0xc0321e)require(r.reg("r4")==6,"connected caller result");
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect connected call");long target=ins.getFlows()[0].getOffset();
    if(target==0xc1055e){require(r.reg("r12")==2&&r.reg("r13")==0x55d4,"next descriptor");boundary=true;break;}
    if(target==0xc0272c){r.pc(at+ins.getLength());r.steps++;continue;}
    if(target==0xc101f8) {
     int length=(int)r.reg("r13"),address=(int)r.reg("r12");byte[] bytes=new byte[length];
     if(address==0x530){require(length==2,"metadata length");bytes[0]=(byte)metadata;bytes[1]=(byte)(metadata>>8);}
     else {require(length==16&&(address>=0x320&&address<0x530||address>=0x540&&address<0x760),"connected serial request");Arrays.fill(bytes,(byte)fill);}
     r.put(0xe023eb,bytes);reads++;r.pc(at+ins.getLength());r.steps++;continue;
    }
    if(target==0xc08560)defaults++;
    require(target>=0xc5e756&&target<0xc5ea3c||Set.of(0xc13550L,0xc12f10L,0xc132ccL,
      0xc13eb8L,0xc135feL,0xc08560L,0xc081f0L,0xc4ff1cL).contains(target),"unreviewed connected call");
   }
   require(r.step(),"connected early return");
  }
  require(boundary&&reads==68&&defaults==(fill==255?1:0),"connected restore sequence");
  require((r.dword(0xfd1e)&16)!=0&&!r.cpu.getTrackedMemoryWriteSet().intersects(toAddr(0xfd1e),toAddr(0xfd1f)),"connected selector write");
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();require(size==851968,"size");
  byte[] raw=new byte[(int)size];require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");println("IMAGE_SHA256="+hash);
  decode(0xc12f10,0xc132cc);decode(0xc5e756,0xc5ea16);decode(0xc4ff1c,0xc4ff80);decode(0xc03218,0xc03230);decode(0xc13550,0xc135fe);
  decode(0xc132cc,0xc13550);decode(0xc135fe,0xc13794);decode(0xc13eb8,0xc13ffc);
  decode(0xc081f0,0xc08566);decode(0xc5ea16,0xc5ea3c);int cases=0;
  for(int fill:new int[]{0,255})for(int scenario=0;scenario<4;scenario++)try(Run r=fixture(0)) {
   r.word(0xfd1e,16);r.cpu.writeRegister("r12",2);r.cpu.writeRegister("r13",0x5864);r.pc(0xc12f10);
   byte[] payload=new byte[528];Arrays.fill(payload,(byte)fill);
   int metadata=fill*528&65535;if(scenario==1)metadata^=1;if(scenario==2)metadata=65535;
   if(scenario==3)payload[527]^=1;
   int sum=0;for(byte value:payload)sum=(sum+(value&255))&65535;
   int reads=0;boolean returned=false;long end=r.steps+200000;
   while(r.steps<end) {
    long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"descriptor decode "+Long.toHexString(at));
    if(ins.getFlowType().isCall()) {
     require(ins.getFlows().length==1,"indirect call");long target=ins.getFlows()[0].getOffset();
     if(target==0xc101f8) {
      int length=(int)r.reg("r13");require(r.reg("r12")== (reads==0?0x530:0x320+16*(reads-1)),"serial address index="+reads+" actual="+Long.toHexString(r.reg("r12")));
      require(length==(reads==0?2:16),"serial length");byte[] bytes=reads==0?new byte[]{(byte)metadata,(byte)(metadata>>8)}:Arrays.copyOfRange(payload,16*(reads-1),16*reads);r.put(0xe023eb,bytes);
      reads++;r.pc(at+ins.getLength());r.steps++;continue;
     }
     require(target>=0xc5e75c&&target<0xc5ea16||target==0xc4ff1c,"unexpected call "+Long.toHexString(target));
    }
    if(!r.step()){returned=true;break;}
   }
   require(returned&&reads==34,"descriptor completion");
   require(r.reg("r4")== (metadata==sum?1:6),"descriptor verdict "+r.reg("r4"));
   require(Arrays.equals(r.mem(0xb00c,528),payload),"gathered payload");
   int fieldBytes=0,fields=0;
   for(int n=1;n<100;n++) {
    long at=0xc81864+8*n;int length=getByte(toAddr(at+4))&255;if(length==0)break;
    long destination=((long)(getShort(toAddr(at+2))&65535)<<14)|(getShort(toAddr(at))&0x3fff);
    require(destination<65536||destination>=0xe00000&&destination+length<=0xe04000,"descriptor destination");
    for(byte value:r.mem(destination,length))require((value&255)==fill,"scattered fields after verdict");
    fieldBytes+=length;fields++;
   }
   require(fieldBytes==514&&fields==58,"descriptor shape");
   require(((r.dword(0xfd5a)&8)!=0)==(metadata!=sum),"checksum flag");
   require((r.dword(0xfd1e)&16)!=0&&!r.cpu.getTrackedMemoryWriteSet().intersects(toAddr(0xfd1e),toAddr(0xfd1f)),"selector write");cases++;
  }
  for(int fill:new int[]{0,255})for(boolean corrupt:new boolean[]{false,true})try(Run r=fixture(0)) {connected(r,fill,corrupt);cases++;}
  println("DESCRIPTOR_RESULT={\"cases\":"+cases+",\"data_bytes\":528,\"metadata_bytes\":2,\"field_bytes\":514,\"field_records\":58,\"connected_startup_cases\":4}");
  println("PASS alternative descriptor; generated serial reads and actual numeric helpers");
 }
}
