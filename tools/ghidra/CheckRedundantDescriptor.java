/* Read-mode redundant descriptors with an explicit synthetic serial-memory array.
 * Read/write calls and delays are replaced; arithmetic/default instructions run.
 * No serial I/O, device physics, timing, reset or complete startup.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
import ghidra.program.model.listing.Instruction;
public class CheckRedundantDescriptor extends CheckResetPreparation {
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();require(size==851968,"size");
  byte[] raw=new byte[(int)size];require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");println("IMAGE_SHA256="+hash);
  decode(0xc1055e,0xc12f10);decode(0xc5e756,0xc5ea3c);decode(0xc4ff1c,0xc4ff80);decode(0xc080dc,0xc081f0);
  List<String> outcomes=new ArrayList<>();int cases=0;
  for(int descriptor:new int[]{0x55d4,0x569c})for(int scenario=0;scenario<(descriptor==0x55d4?6:5);scenario++)try(Run r=fixture(0)) {
   int fill=scenario==1?255:0;int valid=scenario>=2?(scenario==5?0:scenario-2):-1;
   byte[] serial=new byte[4096];Arrays.fill(serial,(byte)fill);
   if(valid>=0) {
    int length=descriptor==0x55d4?96:320;
    int[] starts=descriptor==0x55d4?new int[]{0,96,192}:new int[]{336,656,976};
    int[] metadata=descriptor==0x55d4?new int[]{288,304,320}:new int[]{1296,1312,1328};
    for(int n=0;n<3;n++) {
     int value=n==valid?0:n+1;Arrays.fill(serial,starts[n],starts[n]+length,(byte)value);
     int sum=length*value&65535;if(n!=valid)sum^=1;
     serial[metadata[n]]=(byte)sum;serial[metadata[n]+1]=(byte)(sum>>8);
    }
   }r.word(0xfd1e,16);
   byte[] original=serial.clone();
   r.cpu.writeRegister("r12",2);r.cpu.writeRegister("r13",descriptor);r.pc(0xc1055e);
   Map<Integer,Integer> writeCounts=new TreeMap<>();int reads=0,writes=0,delays=0,defaults=0;boolean returned=false;long end=r.steps+500000;
   while(r.steps<end) {
    long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"decode "+Long.toHexString(at));
    if(ins.getFlowType().isCall()) {
     require(ins.getFlows().length==1,"indirect call");long target=ins.getFlows()[0].getOffset();
     if(target==0xc101f8||target==0xc10060) {
      int address=(int)r.reg("r12"),length=(int)r.reg("r13");
      require(length>0&&length<=320&&address+length<=serial.length,"serial interval");
      if(target==0xc101f8){r.put(0xe023eb,Arrays.copyOfRange(serial,address,address+length));reads++;}
      else {writeCounts.merge((address<<8)|length,1,Integer::sum);if(scenario!=5)System.arraycopy(r.mem(0xe023eb,length),0,serial,address,length);writes++;}
      r.pc(at+ins.getLength());r.steps++;continue;
     }
     if(target==0xc10000){delays++;r.pc(at+ins.getLength());r.steps++;continue;}
     if(target==0xc080dc||target==0xc12e00)defaults++;
     require(target>=0xc5e756&&target<0xc5ea3c||target==0xc4ff1c||target==0xc12e00||target==0xc080dc,"unknown call "+Long.toHexString(target));
    }
    if(!r.step()){returned=true;break;}
   }
   require(returned&&reads>0&&(scenario==5?r.reg("r4")==3:scenario<2||descriptor==0x569c?r.reg("r4")==0:r.reg("r4")==2),"completion descriptor="+descriptor+" scenario="+scenario+" returned="+returned+" result="+r.reg("r4")+" reads="+reads);
   require((scenario==5?writes>0:writes==(scenario<2||descriptor==0x569c?0:21))&&delays==writes,"repair command count");
   if(descriptor==0x55d4&&valid>=0) {
    Map<Integer,Integer> expectedWrites=new TreeMap<>();
    for(int start:new int[]{0,96,192})for(int offset=0;offset<96;offset+=16)expectedWrites.put(((start+offset)<<8)|16,scenario==5?3:1);
    for(int address:new int[]{288,304,320})expectedWrites.put((address<<8)|2,scenario==5?3:1);
    require(writeCounts.equals(expectedWrites),"repair intervals and passes");
   }
   if(scenario==5||descriptor==0x569c)require(Arrays.equals(serial,original),"serial preservation");
   if(valid>=0&&descriptor==0x55d4&&scenario!=5) {
    int length=96;int[] starts={0,96,192};int[] metadata={288,304,320};
    for(int n=0;n<3;n++) {
     for(int j=0;j<length;j++)require(serial[starts[n]+j]==0,"repaired payload");
     require(serial[metadata[n]]==0&&serial[metadata[n]+1]==0,"repaired checksum");
    }
   }
   require(defaults==(scenario==1?1:0),"default selection");
   if(valid>=0) {
    int bytes=0;long desc=descriptor==0x55d4?0xc815d4:0xc8169c;
    for(int n=1;n<100;n++) {
     long at=desc+8*n;int length=getByte(toAddr(at+4))&255;if(length==0)break;
     long destination=((long)(getShort(toAddr(at+2))&65535)<<14)|(getShort(toAddr(at))&0x3fff);
     for(byte value:r.mem(destination,length))require(value==0,"single valid copy scatter");bytes+=length;
    }
    require(bytes==(descriptor==0x55d4?88:315),"scatter size");
   }
   require((r.dword(0xfd1e)&16)!=0&&!r.cpu.getTrackedMemoryWriteSet().intersects(toAddr(0xfd1e),toAddr(0xfd1f)),"selector write");
   outcomes.add("{\"descriptor\":"+descriptor+",\"fill\":"+fill+",\"valid_copy\":"+valid+",\"write_effects_dropped\":"+(scenario==5)+",\"defaults\":"+defaults+",\"result\":"+r.reg("r4")+",\"reads\":"+reads+",\"writes\":"+writes+",\"delays\":"+delays+"}");cases++;
  }
  println("REDUNDANT_RESULT={\"cases\":"+cases+",\"outcomes\":["+String.join(",",outcomes)+"]}");
  println("PASS redundant descriptor; synthetic serial-memory effects");
 }
}
