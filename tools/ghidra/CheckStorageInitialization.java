/* Initializer over generated records, with real geometry/read/checksum code.
 * Far-return callback dispatch is explicit; registers are held-ready RAM.
 * No full application startup, reset, flash write, timing or ECU I/O.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
import ghidra.program.model.listing.Instruction;
public class CheckStorageInitialization extends CheckStorageRecovery {
 void initialize(Run r,int slot,boolean missing) throws Exception {
  long table=0xc82a4a+24*slot;
  int length=getShort(toAddr(table+4))&65535;
  long buffer=((long)(getShort(toAddr(table+14))&65535)<<14)|(getShort(toAddr(table+12))&0x3fff);
  long cb=(getShort(toAddr(table+16))&65535L)|((getShort(toAddr(table+18))&65535L)<<16);
  r.cpu.writeRegister("r12",slot);r.pc(0xc447fe);long end=r.steps+400000;
  boolean returned=false;int defaults=0,callbacks=0;byte[] retained=null;
  while(r.steps<end) {
   long at=r.pc();if(at==0xc44824)require(r.reg("r4")==4&&r.reg("r5")==0,"division result");
   Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"init decode");
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect call");long target=ins.getFlows()[0].getOffset();
    if(target==0xc4ffec) {
     require(((r.reg("r5")<<16)|r.reg("r4"))==cb,"default callback pointer");callbacks++;
     r.returns.push(at+ins.getLength());r.pc(cb);r.steps++;continue;
    }
    require(Set.of(0xc5ea16L,0xc44aa0L,0xc4505aL,0xc450e8L,0xc45054L,0xc44bc0L,0xc44c50L,0xc44ca4L).contains(target),"unreviewed init call "+Long.toHexString(target));
    if(target==0xc5ea16)require(r.reg("r4")==60000&&r.reg("r5")==0&&r.reg("r10")==15000&&r.reg("r11")==0,"division arguments");
    if(target==0xc44ca4){defaults++;retained=r.mem(buffer+8,4);}
   }
   if(!r.step()){returned=true;break;}
  }
  require(returned,"init budget");require(defaults==(missing?1:0)&&callbacks==defaults,"default path selection");
  require(r.reg("r4")== (missing?10:0),"initializer result");
  require((r.mem(0xc4e0,1)[0]&255)==1&&(r.dword(0xc5e4)&65535)==0,"initializer readiness");
  require((r.mem(0xc5f8+slot,1)[0]&255)==(missing?2:0),"slot result");
  long flagAddress=slot==0?0xfd50:slot==1?0xfd12:slot==4?0xfd40:slot==5?0xfd00:0xfd08;
  int flagMask=slot==0||slot==4?0x800:slot==1?0x40:slot==5?0x20:0x400;
  require(((r.dword(flagAddress)&flagMask)!=0)==missing,"default flag");
  if(missing) {
   require((r.dword(0xc5e6+2*slot)&65535)==0,"default index");
   require(r.dword(buffer+4)==0,"default counter");
   require(Arrays.equals(r.mem(buffer+8,4),retained),"retained checksum field");
   for(int n=12;n<length;n++)require(r.mem(buffer+n,1)[0]==0,"default payload");
  }
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();require(size==851968,"size");
  byte[] raw=new byte[(int)size];require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");println("IMAGE_SHA256="+hash);
  decode(0xc447fe,0xc44cec);decode(0xc45054,0xc45188);decode(0xc5ea16,0xc5ea3c);decode(0xc45d84,0xc45da4);
  int cases=0;
  for(int slot:new int[]{0,1,4,5,6})for(int scenario=0;scenario<4;scenario++)try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);r.put(0xcc0000,new byte[65536]);r.put(0xc60c,new byte[]{1});
   long table=0xc82a4a+24*slot,base=0xcc0000+(getShort(toAddr(table))&65535);
   int length=getShort(toAddr(table+4))&65535;
   long buffer=((long)(getShort(toAddr(table+14))&65535)<<14)|(getShort(toAddr(table+12))&0x3fff);
   byte[] fill=new byte[length];Arrays.fill(fill,(byte)0xa5);r.put(buffer,fill);
   byte[] old=record(slot,length,17,9),newer=record(slot,length,18,31);
   if(scenario==1||scenario==2)r.put(base+length,old);
   if(scenario>=2){newer[12]^=1;r.put(base+2*length,newer);}
   initialize(r,slot,scenario==0||scenario==3);
   if(scenario==1||scenario==2)require(Arrays.equals(r.mem(buffer,length),old),"valid recovered content");
   cases++;
  }
  println("INIT_RESULT={\"cases\":"+cases+",\"slots\":[0,1,4,5,6],\"scenarios_per_slot\":4}");
  println("PASS storage initialization; generated records and explicit callback dispatch");
 }
}
