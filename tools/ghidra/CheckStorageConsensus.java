/* Three-copy restore selection and scatter over generated records.
 * Matrix cases replace per-slot initialization; connected cases execute it.
 * Indirect callback dispatch is explicit; all-invalid cases execute the application
 * default handler. No full startup, flash writes, timing or ECU I/O.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
import ghidra.program.model.listing.Instruction;
public class CheckStorageConsensus extends CheckStorageRecovery {
 long[] buffers={0xe030a4,0xe03da4,0xe03ba4};
 long[] flags={0xfd40,0xfd00,0xfd08};int[] flagMasks={0x800,0x20,0x400};
 int expected(int valid,int[] groups) {
  for(int a=0;a<3;a++)for(int b=a+1;b<3;b++)if((valid&(1<<a))!=0&&(valid&(1<<b))!=0&&groups[a]==groups[b])return a;
  for(int a=0;a<3;a++)if((valid&(1<<a))!=0)return a;return -1;
 }
 Map<Long,Byte> scatter(byte[] selected) throws Exception {
  Map<Long,Byte> output=new TreeMap<>();int position=12;
  for(int n=1;n<1000;n++) {
   long at=0xc8169c+8*n;int length=getByte(toAddr(at+4))&255;if(length==0){require(position==327,"descriptor size");return output;}
   long dest=((long)(getShort(toAddr(at+2))&65535)<<14)|(getShort(toAddr(at))&0x3fff);
   require(dest<65536||dest>=0xe00000&&dest+length<=0xe04000,"scatter RAM");
   for(int j=0;j<length;j++)output.put(dest+j,selected[position++]);
  }
  throw new IllegalStateException("descriptor budget");
 }
 void verifyDefaults(Run r) throws Exception {
  require((r.dword(0xfd72)&4)!=0,"default marker");
  int[] source={0x47be,0x47bc,0x47cc,0x47ca,0x4762,0x529c};
  int[] destination={0x1a16,0x14b6,0x17ac,0x20fc,0x2432,0x1412};
  for(int n=0;n<source.length;n++)require((r.dword(0xe00000+destination[n])&65535)==(getShort(toAddr(0xc80000+(source[n]&0x3fff)))&65535),"ROM default load");
  boolean configured=(r.mem(0xc80b96,1)[0]&255)!=0;
  for(int at=0x244c;at<=0x2453;at++)require((r.mem(0xe00000+at,1)[0]&255)==(configured?0:255),"default configuration branch");
  require((r.mem(0xe024a7,1)[0]&255)==255&&(r.dword(0xe01204)&65535)==0,"default constants");
 }
 void restore(Run r,int valid,int[] groups,boolean connected) throws Exception {
  restore(r,valid,groups,connected,false);
 }
 void restore(Run r,int valid,int[] groups,boolean connected,boolean tailOnly) throws Exception {
  byte[][] data=new byte[3][];int choice=expected(valid,groups);
  for(int n=0;n<3;n++) {
   data[n]=record(n+4,512,n+17,9+31*groups[n]);
   if(tailOnly) {
    byte[] common=record(n+4,512,n+17,9);
    System.arraycopy(common,12,data[n],12,315);long sum=0;
    for(int at=0;at<512;at+=4)if(at!=8)sum=(sum+u32(data[n],at))&0xffffffffL;
    put32(data[n],8,sum);
   }
   r.put(buffers[n],data[n]);
   if(connected&&(valid&(1<<n))!=0) {
    long table=0xc82a4a+24*(n+4),dest=0xcc0000+(getShort(toAddr(table))&65535)+512;
    r.put(dest,data[n]);
   }
  }
  r.cpu.writeRegister("r12",2);r.pc(0xc4577c);long end=r.steps+600000;
  List<Integer> init=new ArrayList<>();Set<Integer> copied=new HashSet<>();boolean returned=false,defaultBoundary=false;
  while(r.steps<end) {
   long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"restore decode");
   if(at==0xc458aa)copied.add(0);if(at==0xc458d8)copied.add(1);if(at==0xc45900)copied.add(2);
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect call");long target=ins.getFlows()[0].getOffset();
    if(target==0xc08566){defaultBoundary=true;}
    else if(target==0xc447fe) {
     int n=(int)r.reg("r12")-4;require(n>=0&&n<3,"init slot");init.add(n+4);
     if(!connected) {
      if((valid&(1<<n))==0)r.word(flags[n],(int)(r.dword(flags[n])&65535)|flagMasks[n]);
      r.cpu.writeRegister("r4",(valid&(1<<n))==0?10:0);r.pc(at+ins.getLength());r.steps++;continue;
     }
    } else if(target==0xc4ffec) {
     long cb=(r.reg("r5")<<16)|r.reg("r4");require(cb==0xc45d94||cb==0xc45d98||cb==0xc45d9c,"callback");
     r.returns.push(at+ins.getLength());r.pc(cb);r.steps++;continue;
    } else require(Set.of(0xc5ea16L,0xc44aa0L,0xc4505aL,0xc450e8L,0xc45054L,0xc44bc0L,0xc44c50L,0xc44ca4L,0xc08566L).contains(target),"unreviewed restore call");
   }
   if(!r.step()){returned=true;break;}
  }
  require(init.equals(Arrays.asList(4,5,6)),"init sequence");
  require(choice<0?defaultBoundary&&returned&&copied.isEmpty():returned&&!defaultBoundary&&copied.equals(Set.of(choice)),"restore choice");
  if(choice<0)verifyDefaults(r);
  if(choice>=0)for(Map.Entry<Long,Byte> e:scatter(data[choice]).entrySet())require(r.mem(e.getKey(),1)[0]==e.getValue(),"scattered data");
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();require(size==851968,"size");
  byte[] raw=new byte[(int)size];require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");println("IMAGE_SHA256="+hash);
  decode(0xc447fe,0xc44cec);decode(0xc45054,0xc45188);decode(0xc5ea16,0xc5ea3c);decode(0xc45d84,0xc45da4);decode(0xc4577c,0xc459fa);decode(0xc08566,0xc086ee);decode(0xc0326c,0xc03278);
  require(getInstructionAt(toAddr(0xc0326c)).toString().equals("mov r12,#0x2"),"restore caller argument");
  require(getInstructionAt(toAddr(0xc0326e)).getFlows()[0].getOffset()==0xc4577c,"restore caller");
  require(getInstructionAt(toAddr(0xc03272)).toString().equals("mov r12,#0x2"),"next caller argument");
  require(getInstructionAt(toAddr(0xc03274)).getFlows()[0].getOffset()==0xc45720,"next caller");
  int[][] patterns={{0,0,0},{0,0,1},{0,1,0},{0,1,1},{0,1,2}};int cases=0;
  for(int valid=0;valid<8;valid++)for(int[] groups:patterns)try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);r.put(0xcc0000,new byte[65536]);r.put(0xc60c,new byte[]{1});
   restore(r,valid,groups,false);cases++;
  }
  int[] validMasks={7,4,0,7,7};int[] patternIndices={3,4,0,4,1};
  for(int n=0;n<validMasks.length;n++)try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);r.put(0xcc0000,new byte[65536]);r.put(0xc60c,new byte[]{1});
   restore(r,validMasks[n],patterns[patternIndices[n]],true);cases++;
  }
  for(int config:new int[]{0,1})try(Run r=fixture(0)) {
   r.put(0xc80b96,new byte[]{(byte)config});r.put(0xe024ff,new byte[]{(byte)255});
   r.invoke(0xc08566,200);verifyDefaults(r);
   require((r.mem(0xe024ff,1)[0]&255)==248,"default bit mask");cases++;
  }
  try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);r.put(0xcc0000,new byte[65536]);r.put(0xc60c,new byte[]{1});
   restore(r,7,patterns[3],true,true);cases++;
  }
  println("CONSENSUS_RESULT={\"cases\":"+cases+",\"selection_matrix_cases\":40,\"connected_cases\":5,\"scatter_bytes\":315,\"default_configuration_cases\":2,\"tail_selection_cases\":1,\"static_restore_call_sites\":2}");
  println("PASS storage consensus; generated copies, initializer shims or held-ready reads");
 }
}
