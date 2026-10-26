/* Exact-image manager transitions; lower submission result is injected.
 * No scheduling, IRQ, flash programming or persistence model.
 * @category openm74
 */
import java.security.MessageDigest;
import java.util.*;
public class CheckStorageManager extends CheckResetPreparation {
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size==851968,"size");byte[] image=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),image)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(image));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");
  println("IMAGE_SHA256="+hash);decode(0xc44a26,0xc44a7c);decode(0xc453ac,0xc453d0);
  int cases=0;
  for(int result:new int[]{0,1,2,3,4,5,6,7,65535})try(Run r=fixture(0)) {
   r.word(0xc5e4,1);r.word(0xc4e2,1);r.word(0xc600,0x8000);r.word(0xc602,0);
   r.word(0xc604,512);r.word(0xc606,0x30a4);r.word(0xc608,0x380);r.pc(0xc44a26);
   boolean called=false,returned=false;
   while(r.steps<100) {
    if(r.pc()==0xc44a6e) {
     require(!called&&r.reg("r12")==0x8000&&r.reg("r13")==0&&r.reg("r14")==0x30a4&&r.reg("r15")==0x380,"worker arguments");
     require(r.dword(r.reg("r0"))==512,"stack length");called=true;
     r.cpu.writeRegister("r4",result);r.pc(0xc44a72);r.steps++;continue;
    }
    if(!r.step()){returned=true;break;}
   }
   require(called&&returned&&r.dword(0xc5e4)%65536==2&&r.dword(0xc4e2)%65536==1,"submission transition");cases++;
  }
  for(int id=0;id<8;id++)try(Run r=fixture(0)) {
   r.word(0xc5e4,2);r.word(0xc4e2,1);r.word(0xc60a,id);r.word(0xc5f6,13);
   r.put(0xc5f8+id,new byte[]{2});r.invoke(0xc44a26,100);
   require((r.dword(0xc5e4)&65535)==0&&(r.dword(0xc4e2)&65535)==0,"completion transition");
   require((r.dword(0xc5e6+2*id)&65535)==13&&(r.mem(0xc5f8+id,1)[0]&255)==0,"slot bookkeeping");cases++;
  }
  for(int result:new int[]{0,1,2,3,5})try(Run r=fixture(0)) {
   r.word(0xc614,1);r.put(0xc624,new byte[]{2});r.cpu.writeRegister("r12",result);
   r.invoke(0xc453ac,100);
   require((r.dword(0xc616)&65535)==result&&(r.dword(0xc614)&65535)==0&&(r.mem(0xc624,1)[0]&255)==0,"lower completion");cases++;
  }
  println("STORAGE_RESULT={\"cases\":"+cases+",\"submission_transitions\":9,\"completion_transitions\":8,\"lower_completions\":5}");
  println("PASS storage manager; synthetic transitions, no scheduler or flash model");
 }
}
