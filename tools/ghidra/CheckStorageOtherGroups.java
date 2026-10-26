/* Generated-record restores for independent descriptors, paired copies and one flag.
 * Real initialization/read/checksum/scatter paths, explicit far-callback dispatch,
 * held-ready RAM registers. No full startup, erase/program, reset, timing or ECU I/O.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
import ghidra.program.model.listing.Instruction;
public class CheckStorageOtherGroups extends CheckStorageRecovery {
 void fixSum(byte[] data) {long sum=0;for(int n=0;n<data.length;n+=4)if(n!=8)sum=(sum+u32(data,n))&0xffffffffL;put32(data,8,sum);}
 long base(int slot) throws Exception {return 0xcc0000+(getShort(toAddr(0xc82a4a+24*slot))&65535);}
 Run ready() throws Exception {
  Run r=fixture(0);r.put(0xffff00,new byte[16]);r.put(0xcc0000,new byte[65536]);r.put(0xc60c,new byte[]{1});return r;
 }
 Map<Long,Byte> expectedScatter(long descriptor,byte[] selected,int expectedLength) throws Exception {
  Map<Long,Byte> result=new TreeMap<>();int position=12;
  for(int n=1;n<1000;n++) {
   long at=descriptor+8*n;int length=getByte(toAddr(at+4))&255;
   if(length==0){require(position==12+expectedLength,"scatter count");return result;}
   long destination=((long)(getShort(toAddr(at+2))&65535)<<14)|(getShort(toAddr(at))&0x3fff);
   require(destination<65536||destination>=0xe00000&&destination+length<=0xe04000,"scatter RAM");
   for(int j=0;j<length;j++)result.put(destination+j,selected[position++]);
  }
  throw new IllegalStateException("descriptor budget");
 }
 void verifyScatter(Run r,long descriptor,byte[] selected,int expectedLength) throws Exception {
  for(Map.Entry<Long,Byte> e:expectedScatter(descriptor,selected,expectedLength).entrySet())require(r.mem(e.getKey(),1)[0]==e.getValue(),"scatter contents");
 }
 List<Long> startupCalls=new ArrayList<>();List<Integer> initializedSlots=new ArrayList<>();
 void execute(Run r,long entry) throws Exception {execute(r,entry,-1);}
 void execute(Run r,long entry,long stop) throws Exception {
  require(r.returns.isEmpty(),"return stack");r.pc(entry);long end=r.steps+700000;
  while(r.steps<end) {
   long at=r.pc();if(at==stop){require(r.returns.isEmpty(),"startup stop inside callee");return;}
   if(at==0xc03278)require(r.reg("r4")==6,"wrapper result reached next caller");
   Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"decode "+Long.toHexString(at));
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect call");long target=ins.getFlows()[0].getOffset();
    if(target==0xc4ffec) {
     long cb=(r.reg("r5")<<16)|r.reg("r4");require(cb>=0xc45d84&&cb<=0xc45da0&&(cb&3)==0,"callback");
     r.returns.push(at+ins.getLength());r.pc(cb);r.steps++;continue;
    }
    if(target==0xc447fe)initializedSlots.add((int)r.reg("r12"));
    if(at==0xc03268||at==0xc0326e||at==0xc03274||at==0xc0327a) {
     require(r.reg("r12")==2,"startup helper mode");startupCalls.add(target);
    }
    require(Set.of(0xc459faL,0xc4577cL,0xc45720L,0xc45c7eL,0xc08566L,0xc447feL,0xc5ea16L,0xc44aa0L,0xc4505aL,0xc450e8L,0xc45054L,
      0xc44bc0L,0xc44c50L,0xc44ca4L,0xc455faL,0xc086eeL).contains(target),"unreviewed call "+Long.toHexString(target));
   }
   if(!r.step())return;
  }
  throw new IllegalStateException("restore budget");
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();require(size==851968,"size");
  byte[] raw=new byte[(int)size];require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");println("IMAGE_SHA256="+hash);
  decode(0xc447fe,0xc44cec);decode(0xc45054,0xc45188);decode(0xc5ea16,0xc5ea3c);
  decode(0xc455fa,0xc45da4);decode(0xc08566,0xc086f8);decode(0xc031fc,0xc03282);decode(0xc0ae5a,0xc0b05a);int cases=0;
  for(int slot=0;slot<2;slot++)for(int scenario=0;scenario<4;scenario++)try(Run r=ready()) {
   byte[] good=record(slot,2048,17,9),bad=record(slot,2048,18,31);bad[12]^=1;
   if(scenario==1||scenario==2)r.put(base(slot)+2048,good);
   if(scenario>=2)r.put(base(slot)+4096,bad);
   r.cpu.writeRegister("r12",2);r.cpu.writeRegister("r13",slot==0?0x532c:0x550c);
   execute(r,0xc455fa);require(r.reg("r4")==0,"descriptor read result");
   verifyScatter(r,slot==0?0xc8132c:0xc8150c,scenario==0||scenario==3?new byte[2048]:good,slot==0?1955:1886);cases++;
  }
  // The outer wrapper checks for result1 although both modeled read helpers return0.
  for(int valid=0;valid<4;valid++)try(Run r=ready()) {
   Map<Long,Byte> expected=new TreeMap<>();
   for(int slot=0;slot<2;slot++) {
    byte[] data=record(slot,2048,17,9+slot);boolean exists=(valid&(1<<slot))!=0;
    if(exists)r.put(base(slot)+2048,data);
    expected.putAll(expectedScatter(slot==0?0xc8132c:0xc8150c,exists?data:new byte[2048],slot==0?1955:1886));
   }
   r.cpu.writeRegister("r12",2);execute(r,0xc45720);require(r.reg("r4")==6,"outer read result");
   for(Map.Entry<Long,Byte> e:expected.entrySet())require(r.mem(e.getKey(),1)[0]==e.getValue(),"outer scatter");cases++;
  }
  int markerOffset=getShort(toAddr(0xc81c7a))&65535;require(markerOffset==86,"pair marker offset");
  for(int valid=0;valid<4;valid++)for(int first=0;first<2;first++)for(int second=0;second<2;second++)try(Run r=ready()) {
   byte[] a=record(2,128,17,9),b=record(3,128,18,31);a[12+markerOffset]=(byte)first;b[12+markerOffset]=(byte)second;fixSum(a);fixSum(b);
   if((valid&1)!=0)r.put(base(2)+128,a);if((valid&2)!=0)r.put(base(3)+128,b);
   r.cpu.writeRegister("r12",2);execute(r,0xc459fa);
   byte[] selected=(valid&1)!=0?a:(valid&2)!=0?b:new byte[128];verifyScatter(r,0xc815d4,selected,88);
   boolean combined=(valid&1)!=0&&first==1||(valid&2)!=0&&second==1;
   require(((r.dword(0xfde2)&1)!=0)==combined,"combined pair flag");require(r.reg("r4")==0,"pair read result");cases++;
  }
  for(int scenario=0;scenario<6;scenario++)try(Run r=ready()) {
   byte[] good=record(7,128,17,9),bad=record(7,128,18,31);bad[12]^=1;
   if(scenario<3){good[12]=(byte)(scenario==2?0xa5:scenario);fixSum(good);r.put(base(7)+128,good);}
   if(scenario==4)r.put(base(7)+256,bad);
   if(scenario==5){r.put(base(7)+128,good);r.put(base(7)+256,bad);}
   r.cpu.writeRegister("r12",2);execute(r,0xc45c7e);
   int expected=scenario==3||scenario==4?255:good[12]&255;
   require((r.mem(0xe016b4,1)[0]&255)==expected,"single-field restore");require(r.reg("r4")==0,"single read result");cases++;
  }
  for(int scenario=0;scenario<3;scenario++)try(Run r=ready()) {
   startupCalls.clear();initializedSlots.clear();
   for(int slot=0;slot<8;slot++) {
    int length=getShort(toAddr(0xc82a4a+24*slot+4))&65535;
    boolean valid=scenario==1||scenario==2&&(slot==0||slot==3||slot==5||slot==6);
    if(valid) {
     byte[] data=record(slot,length,17,9);if(slot==7){data[12]=1;fixSum(data);}
     r.put(base(slot)+length,data);
    }
   }
   r.word(0xfd1e,0);execute(r,0xc03210,0xc0327e);
   require(startupCalls.equals(Arrays.asList(0xc459faL,0xc4577cL,0xc45720L,0xc45c7eL)),"startup group order");
   require(initializedSlots.equals(Arrays.asList(2,3,4,5,6,0,1,7)),"startup slot order");
   require((r.mem(0xe016b4,1)[0]&255)==(scenario==1?1:255),"startup single-field result");
   require(r.dword(0xf0b8)==0x3000,"startup reset register");cases++;
  }
  // Conditional slices: no serial helper or preceding application initializer executes.
  for(int flag:new int[]{0,16})try(Run r=ready()) {
   r.word(0xfd1e,flag);r.word(0xfd72,0xa5a5);r.word(0xfd2e,0xa5a5);r.pc(0xc03210);long end=r.steps+30;long target=0;
   while(r.steps<end) {
    Instruction ins=getInstructionAt(toAddr(r.pc()));if(ins.getFlowType().isCall()){target=ins.getFlows()[0].getOffset();break;}
    require(r.step(),"gate returned");
   }
   require(target==(flag==0?0xc459fa:0xc13550)&&r.reg("r12")==2,"initial startup gate");
   require((r.dword(0xfd72)&4)==0&&(r.dword(0xfd2e)&1)==0,"startup flag clears");cases++;
  }
  for(int flag:new int[]{0,16})for(int result:new int[]{0,3,65535})try(Run r=ready()) {
   r.word(0xfd1e,flag);r.cpu.writeRegister("r4",result);r.pc(0xc0321e);long end=r.steps+40;long target=0;
   while(r.steps<end) {
    long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));
    if(ins.getFlowType().isCall()) {
     target=ins.getFlows()[0].getOffset();
     if(target==0xc0272c){r.pc(at+ins.getLength());r.steps++;continue;}break;
    }
    require(r.step(),"recheck returned");
   }
   require(target==(flag==0?0xc459fa:0xc1055e)&&r.reg("r12")==2,"startup flag recheck");
   if(flag!=0)require(r.reg("r13")==0x55d4,"serial descriptor argument");cases++;
  }
  for(int config:new int[]{0,1})for(int result:new int[]{0,1,255,256})for(int flags:new int[]{0,16})for(int identityFlags:new int[]{0,0xfff})try(Run r=ready()) {
   r.put(0xc80a52,new byte[]{(byte)config});r.word(0xfd1e,flags);r.word(0xfd14,identityFlags);
   r.cpu.writeRegister("r4",result);r.pc(0xc031fc);long end=r.steps+2000;long target=0;
   while(r.steps<end) {
    Instruction ins=getInstructionAt(toAddr(r.pc()));require(ins!=null,"producer bridge decode");
    if(ins.getFlowType().isCall()) {
     target=ins.getFlows()[0].getOffset();if(target!=0xc0ae5a)break;
    }
    require(r.step(),"producer bridge returned");
   }
   boolean alternate=config==0&&(result&255)!=0;
   require(target==(alternate?0xc13550:0xc459fa)&&r.reg("r12")==2&&r.returns.isEmpty(),"producer to gate");
   require((r.mem(0xe01d0e,1)[0]&255)==(alternate?0x45:0x46),"intermediate diagnostic marker");cases++;
  }
  println("GROUPS_RESULT={\"cases\":"+cases+",\"descriptor_cases\":8,\"outer_cases\":4,\"pair_cases\":16,\"single_cases\":6,\"scatter_lengths\":[1955,1886,88,1],\"startup_sequences\":3,\"startup_gate_prefixes\":8,\"producer_bridge_cases\":32}");
  println("PASS storage other groups; generated records and held-ready read model");
 }
}
