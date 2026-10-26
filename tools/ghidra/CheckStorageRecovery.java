/* Record selection over generated flash records; actual read/checksum code runs.
 * Direct recovery entry, held-ready plain-RAM peripheral registers, no reset,
 * initialization geometry checks, flash operation, timing or ECU I/O.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
import ghidra.program.model.listing.Instruction;
public class CheckStorageRecovery extends CheckResetPreparation {
 long u32(byte[] b,int at){return (b[at]&255L)|((b[at+1]&255L)<<8)|((b[at+2]&255L)<<16)|((b[at+3]&255L)<<24);}
 void put32(byte[] b,int at,long n){for(int i=0;i<4;i++)b[at+i]=(byte)(n>>(8*i));}
 byte[] record(int slot,int length,long generation,int seed) throws Exception {
  byte[] data=new byte[length];for(int n=12;n<length;n++)data[n]=(byte)(n*37+seed);
  long table=0xc82a4a+24*slot;
  put32(data,0,(getShort(toAddr(table+8))&65535L)|((getShort(toAddr(table+10))&65535L)<<16));
  put32(data,4,generation);long sum=0;
  for(int n=0;n<length;n+=4)if(n!=8)sum=(sum+u32(data,n))&0xffffffffL;
  put32(data,8,sum);return data;
 }
 void recover(Run r,int slot) throws Exception {
  r.cpu.writeRegister("r12",0x6a4a+24*slot);r.cpu.writeRegister("r13",0xc5e6+2*slot);r.pc(0xc44aa0);
  long end=r.steps+300000;
  while(r.steps<end) {
   Instruction ins=getInstructionAt(toAddr(r.pc()));require(ins!=null,"recovery decode");
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect recovery call");long target=ins.getFlows()[0].getOffset();
    require(Set.of(0xc4505aL,0xc450e8L,0xc45054L,0xc44bc0L,0xc44c50L).contains(target),"unreviewed recovery call");
   }
   if(!r.step())return;
  }
  throw new IllegalStateException("recovery budget");
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();require(size==851968,"size");
  byte[] raw=new byte[(int)size];require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");println("IMAGE_SHA256="+hash);
  decode(0xc44aa0,0xc44ca4);decode(0xc45054,0xc45188);int cases=0;
  for(int slot:new int[]{0,1,4,5,6})for(int scenario=0;scenario<9;scenario++)try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);r.put(0xcc0000,new byte[65536]);r.put(0xc60c,new byte[]{1});
   long table=0xc82a4a+24*slot,base=0xcc0000+(getShort(toAddr(table))&65535);
   int length=getShort(toAddr(table+4))&65535;
   long buffer=((long)(getShort(toAddr(table+14))&65535)<<14)|(getShort(toAddr(table+12))&0x3fff);
   byte[] old=record(slot,length,scenario==6?0xffffffffL:17,9);
   byte[] newer=record(slot,length,scenario==6?0:scenario==5?17:18,31);
   if(scenario!=0)r.put(base+length,old);
   if(scenario>=2) {
    if(scenario==2)newer[0]^=1;
    if(scenario==3||scenario==7)newer[12]^=1;
    if(scenario==4)Arrays.fill(newer,128,newer.length,(byte)0);
    r.put(base+2*length,newer);
   }
   if(scenario==7)r.put(base+length,new byte[length]);
   recover(r,slot);long result=r.reg("r4"),index=r.dword(0xc5e6+2*slot)&65535;
   boolean valid=scenario!=0&&scenario!=7;int expectedIndex=scenario==5||scenario==8?2:1;
   require(valid?result==0||result==1:result==2,"selection result scenario="+scenario+" result="+result);
   if(valid) {
    require(index==expectedIndex,"selected index scenario="+scenario+" index="+index);
    require(Arrays.equals(r.mem(buffer,length),scenario==5||scenario==8?newer:old),"selected content");
   }
   cases++;
  }
  // Independent slot recovery can select different save generations after interruption.
  try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);r.put(0xcc0000,new byte[65536]);r.put(0xc60c,new byte[]{1});
   for(int slot:new int[]{0,1,4,5,6}) {
    long table=0xc82a4a+24*slot,base=0xcc0000+(getShort(toAddr(table))&65535);
    int length=getShort(toAddr(table+4))&65535;
    r.put(base+length,record(slot,length,17,9));
    byte[] newer=record(slot,length,18,31);
    if(slot>=4)Arrays.fill(newer,128,newer.length,(byte)0);
    r.put(base+2*length,newer);
   }
   for(int slot:new int[]{0,1,4,5,6}) {
    recover(r,slot);require(r.reg("r4")==0||r.reg("r4")==1,"mixed recovery status");
    require((r.dword(0xc5e6+2*slot)&65535)==(slot<2?2:1),"mixed recovery generation");
    long table=0xc82a4a+24*slot;
    long buffer=((long)(getShort(toAddr(table+14))&65535)<<14)|(getShort(toAddr(table+12))&0x3fff);
    require(r.dword(buffer+4)==(slot<2?18:17),"mixed selected generation");
   }
   cases++;
  }
  println("RECOVERY_RESULT={\"cases\":"+cases+",\"slots\":[0,1,4,5,6],\"scenarios_per_slot\":9,\"mixed_slot_cases\":1}");
  println("PASS storage recovery; generated records and held-ready read model");
 }
}
