/* Connected storage submission/erase/program flow in synthetic memory.
 * Command instructions execute; their flash effects and interrupt delivery are
 * supplied by the harness. Far-return callback dispatch uses an explicit shim.
 * No ECU I/O, flash physics, timing, power-loss or reset model.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
import ghidra.program.model.listing.Instruction;
public class CheckStorageSequence extends CheckResetPreparation {
 class Drive {
  Run r;List<Long> erased=new ArrayList<>(),programmed=new ArrayList<>();
  List<Long> callbacks=new ArrayList<>();List<byte[]> loaded=new ArrayList<>();
  int corruptProgram=-1;byte[] flash=new byte[65536];byte[] page;int words;long target;int eraseLength;long eraseTarget;
  Drive(Run run){r=run;}
  void invoke(long entry) throws Exception {
   require(r.returns.isEmpty(),"call stack");r.pc(entry);long end=r.steps+200000;
   while(r.steps<end) {
    long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"decode "+Long.toHexString(at));
    if(ins.getFlowType().isCall()) {
     require(ins.getFlows().length==1,"indirect call");long callee=ins.getFlows()[0].getOffset();
     if(callee==0xc4ffec) {
      long cb=(r.reg("r5")<<16)|r.reg("r4");require(cb==0xc44a26||cb==0xc44a7c,"unknown callback");
      callbacks.add(cb);r.returns.push(at+ins.getLength());r.pc(cb);r.steps++;continue;
     }
     require(Set.of(0xc44c50L,0xc44e2cL,0xc454d0L,0xc453d0L,0xc45426L,
       0xc44ee4L,0xc453acL,0xc45188L,0xc4520cL).contains(callee),"unreviewed call "+Long.toHexString(callee));
    }
    // Memory-mapped command writes must not become flash array data on later reads.
    if(at==0xc44f9c||at==0xc44fa0||at==0xc45262||at==0xc45266||at==0xc4531a||at==0xc4531e) {
     long address=at==0xc44f9c||at==0xc44fa0?(r.reg("r14")<<14)|(r.reg("r13")&0x3fff):
       (r.reg("r13")<<14)|(r.reg("r12")&0x3fff);
     require(address>=0xcc0000&&address+4<=0xcd0000,"flash read mapping");
     r.put(address,Arrays.copyOfRange(flash,(int)(address-0xcc0000),(int)(address-0xcc0000)+4));
    }
    if(at==0xc45426){page=new byte[128];words=0;}
    if(at==0xc45462)target=(r.reg("r3")<<14)|(r.reg("r2")&0x3fff);
    if(at==0xc454a2||at==0xc454ae) {
     require(words<64,"page overflow");int w=(int)r.reg("r14");page[2*words]=(byte)w;page[2*words+1]=(byte)(w>>8);words++;
    }
    if(at==0xc454ce) {
     require(words==64&&target>=0xcc0000&&target+128<=0xcd0000,"program target/load");
     // Explicit synthetic peripheral effect after real command instructions.
     System.arraycopy(page,0,flash,(int)(target-0xcc0000),128);if(programmed.size()==corruptProgram)flash[(int)(target-0xcc0000)]^=1;
     r.put(target,page);programmed.add(target);loaded.add(page.clone());
    }
    if(at==0xc454d0) {
     eraseTarget=0xcc0000+r.dword(0xc62a);
     eraseLength=r.dword(0xc620)>=4096&&(eraseTarget&4095)==0?4096:128;
    }
    if(at==0xc45524||at==0xc45548) {
     require(eraseTarget>=0xcc0000&&eraseTarget+eraseLength<=0xcd0000,"erase target");
     Arrays.fill(flash,(int)(eraseTarget-0xcc0000),(int)(eraseTarget-0xcc0000)+eraseLength,(byte)0);
     r.put(eraseTarget,new byte[eraseLength]);erased.add(eraseTarget);
    }
    if(!r.step())return;
   }
   throw new IllegalStateException("sequence budget "+Long.toHexString(r.pc()));
  }
 }
 void setup(Run r) throws Exception {
  r.put(0xffff00,new byte[16]);r.put(0xcc0000,new byte[65536]);
  r.pc(0xc0243e);long end=r.steps+300;
  while(r.pc()!=0xc02452&&r.steps<end)require(r.step(),"init return");
  require(r.pc()==0xc02452&&r.dword(0xc610)==0xc44a26&&r.dword(0xc618)==0xc44a7c,"init");
  r.put(0xc4e0,new byte[]{1});
 }
 int complete(Drive d) throws Exception {
  int ticks=0;
  while((d.r.dword(0xc4e2)&65535)==1&&ticks<100) {
   d.r.word(0xffff04,0x8000);d.invoke(0xc4554e);ticks++;
  }
  require(ticks<100,"synthetic completion budget");return ticks;
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();require(size==851968,"size");
  byte[] raw=new byte[(int)size];require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");println("IMAGE_SHA256="+hash);
  decode(0xc0243e,0xc02452);decode(0xc4491a,0xc44aa0);decode(0xc44c50,0xc44ca4);
  decode(0xc44d9e,0xc45590);int cases=0;
  for(int slot:new int[]{0,1,4,5,6})for(boolean wrap:new boolean[]{false,true})try(Run r=fixture(0)) {
   setup(r);Drive d=new Drive(r);long record=0xc82a4a+24*slot;
   int length=getShort(toAddr(record+4))&65535,count=getShort(toAddr(record+6))&65535;
   long source=((long)(getShort(toAddr(record+14))&65535)<<14)|(getShort(toAddr(record+12))&0x3fff);
   long destination=0xcc0000+(getShort(toAddr(record))&65535)+(wrap?0:length);
   byte[] data=new byte[length];for(int n=0;n<length;n++)data[n]=(byte)(n*29+slot+3);
   r.put(source,data);r.word(0xc5e6+2*slot,wrap?count-1:0);r.cpu.writeRegister("r12",slot);r.cpu.writeRegister("r13",1);
   d.invoke(0xc4491a);require(r.reg("r4")==0&&(r.dword(0xc4e2)&65535)==1,"submit");
   int ticks=complete(d);require((r.dword(0xc4e2)&65535)==0,"final common status slot="+slot+" common="+(r.dword(0xc4e2)&65535)+" lower="+(r.dword(0xc616)&65535)+" callbacks="+d.callbacks);
   require((r.dword(0xc5e6+2*slot)&65535)==(wrap?0:1),"record index");
   require(d.erased.size()==length/128&&d.programmed.size()==2*length/128,"command counts");
   require(d.callbacks.equals(Arrays.asList(0xc44a26L,0xc44a26L)),"success callbacks");
   require(ticks==3*length/128,"continuation count slot="+slot+" ticks="+ticks);
   require(Arrays.equals(Arrays.copyOfRange(d.flash,(int)(destination-0xcc0000),(int)(destination-0xcc0000)+length),r.mem(source,length)),"final synthetic contents");
   for(int n=0;n<length/128;n++) {
    require(d.erased.get(n)==destination+128*n,"erase sequence");
    require(d.programmed.get(n)==destination+128*n&&d.programmed.get(n+length/128)==destination+128*n,"program sequence");
    require(Arrays.equals(d.loaded.get(n),new byte[128]),"erase follow-up zero load");
   }
   cases++;
  }
  for(int fault=0;fault<7;fault++)try(Run r=fixture(0)) {
   setup(r);Drive d=new Drive(r);r.word(0xc5e4,1);r.word(0xc4e2,1);
   r.word(0xc600,0x8000);r.word(0xc602,0);r.word(0xc604,512);
   r.word(0xc606,0x30a4);r.word(0xc608,0x380);
   if(fault==0)r.put(0xc60c,new byte[]{0});
   if(fault==1)r.word(0xc600,0x8001);
   if(fault==2)r.word(0xc604,127);
   if(fault==3){r.word(0xc606,0);r.word(0xc608,0);}
   if(fault==4)r.word(0xc614,1);
   if(fault==5)d.flash[0x8000]=1;
   if(fault==6)r.word(0xffff0a,0x800);
   d.invoke(0xc44a26);
   require((r.dword(0xc5e4)&65535)==2,"post-rejection manager state");
   if(fault<5)require(r.reg("r4")==new int[]{1,6,7,5,4}[fault],"real API rejection result");
   require((r.dword(0xc4e2)&65535)==(fault<5?1:2),"post-rejection common status");
   require(d.callbacks.size()==(fault<5?0:1)&&d.erased.isEmpty()&&d.programmed.isEmpty(),"rejection effects");
   require((r.dword(0xc616)&65535)==(fault<5?0:fault==5?1:2),"rejection lower result");
   // A status poll and an idle interrupt-worker call do not invoke the success callback.
   r.word(0xffff04,0x8000);d.invoke(0xc4554e);
   require((r.dword(0xc4e2)&65535)==(fault<5?1:2),"rejection continued unexpectedly");cases++;
  }
  for(int slot:new int[]{0,4})try(Run r=fixture(0)) {
   setup(r);Drive d=new Drive(r);long record=0xc82a4a+24*slot;
   int length=getShort(toAddr(record+4))&65535,count=getShort(toAddr(record+6))&65535;
   long source=((long)(getShort(toAddr(record+14))&65535)<<14)|(getShort(toAddr(record+12))&0x3fff);
   byte[] data=new byte[length];Arrays.fill(data,(byte)0xa5);r.put(source,data);
   r.word(0xc5e6+2*slot,count-1);r.cpu.writeRegister("r12",slot);r.cpu.writeRegister("r13",1);
   // Fail first data page for slot4, last data page for slot0.
   d.corruptProgram=slot==4?length/128:2*length/128-1;
   d.invoke(0xc4491a);complete(d);
   require((r.dword(0xc4e2)&65535)==3&&(r.dword(0xc616)&65535)==4,"corruption result");
   require((r.dword(0xc5e6+2*slot)&65535)==count-1,"corruption committed index");
   require(d.programmed.size()==d.corruptProgram+1&&d.callbacks.equals(Arrays.asList(0xc44a26L,0xc44a7cL)),"corruption continuation");cases++;
  }
  println("SEQUENCE_RESULT={\"cases\":"+cases+",\"connected_slots\":[0,1,4,5,6],\"immediate_rejections\":7,\"connected_successes\":10,\"corruption_cases\":2}");
  println("PASS storage sequence; synthetic flash effects and interrupt delivery");
 }
}
