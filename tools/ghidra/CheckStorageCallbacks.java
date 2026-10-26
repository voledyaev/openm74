/* Exact-image callback registration and page comparison with synthetic flash/RAM.
 * Indirect far-return thunk is replaced with explicit callback dispatch; no IRQ,
 * flash physics, command completion, reset or device I/O is modeled.
 * @category openm74
 */
import java.security.MessageDigest;
import ghidra.program.model.listing.Instruction;
import java.util.*;
public class CheckStorageCallbacks extends CheckResetPreparation {
 void callbacks(Run r) throws Exception {
  r.pc(0xc0243e);long end=r.steps+300;
  while(r.pc()!=0xc02452&&r.steps<end)require(r.step(),"init returned");
  require(r.pc()==0xc02452,"init budget");
  require(r.dword(0xc610)==0xc44a26&&r.dword(0xc618)==0xc44a7c,"registration");
 }
 void execute(Run r,long entry,long expectedCallback) throws Exception {
  r.pc(entry);boolean returned=false;int callbacks=0;long end=r.steps+5000;
  while(r.steps<end) {
   long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"decode");
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect call");long target=ins.getFlows()[0].getOffset();
    if(target==0xc4ffec) {
     long cb=(r.reg("r5")<<16)|r.reg("r4");require(cb==expectedCallback,"callback selection");
     callbacks++;r.returns.push(at+ins.getLength());r.pc(cb);r.steps++;continue;
    }
    require(target==0xc453ac,"unexpected nested call "+Long.toHexString(target));
   }
   if(!r.step()){returned=true;break;}
  }
  require(returned&&callbacks==1,"callback completion");
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size==851968,"size");byte[] raw=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"image");
  println("IMAGE_SHA256="+hash);decode(0xc0243e,0xc02452);decode(0xc44d9e,0xc44e2c);
  decode(0xc44a26,0xc44aa0);decode(0xc4520c,0xc453d0);decode(0xc4554a,0xc45590);
  int cases=0;
  try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);callbacks(r);require((r.mem(0xc60c,1)[0]&255)==1,"ready");cases++;
  }
  for(int state:new int[]{1,2})for(int result:new int[]{1,2,3,4,6})try(Run r=fixture(0)) {
   r.word(0xc618,0x4a7c);r.word(0xc61a,0xc4);r.word(0xc5e4,state);r.word(0xc4e2,1);
   r.cpu.writeRegister("r12",result);execute(r,0xc453ac,0xc44a7c);
   require((r.dword(0xc616)&65535)==result&&(r.dword(0xc5e4)&65535)==0,"error result");
   require((r.dword(0xc4e2)&65535)==(state==1?2:3),"error mapping");cases++;
  }
  for(int mismatch:new int[]{-1,0,63,127})for(boolean protection:new boolean[]{false,true})try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);callbacks(r);
   byte[] page=new byte[128];for(int n=0;n<128;n++)page[n]=(byte)(n*37+11);
   r.put(0xe030a4,page);r.put(0xcc8000,page);
   if(mismatch>=0)r.put(0xcc8000+mismatch,new byte[]{(byte)(page[mismatch]^1)});
   r.word(0xffff0a,protection?0x800:0);r.word(0xc5e4,2);r.word(0xc4e2,1);
   r.word(0xc60a,4);r.word(0xc5f6,7);r.word(0xc5ee,6);
   r.word(0xc614,1);r.word(0xc616,5);r.put(0xc624,new byte[]{1});
   r.word(0xc620,128);r.word(0xc622,0);r.word(0xc62a,0x8000);r.word(0xc62c,0);
   r.word(0xc61c,0x30a4);r.word(0xc61e,0x380);
   boolean success=mismatch<0&&!protection;
   execute(r,0xc4520c,success?0xc44a26:0xc44a7c);
   require((r.dword(0xc4e2)&65535)==(success?0:3),"common status");
   require((r.dword(0xc616)&65535)==(mismatch>=0?4:protection?2:0),"compare/protection result");
   require((r.dword(0xc5ee)&65535)==(success?7:6),"record commit");cases++;
  }
  // Execute only the interrupt worker's admission prefix; watchdog instruction is skipped.
  for(int flag:new int[]{0,0x8000})for(int active:new int[]{0,1})for(int status:new int[]{0,5})for(int mode:new int[]{1,2})try(Run r=fixture(0)) {
   r.put(0xffff00,new byte[16]);r.word(0xffff04,flag);r.word(0xc614,active);r.word(0xc616,status);r.put(0xc624,new byte[]{(byte)mode});
   r.pc(0xc4554e);long end=r.steps+100;long target=0;boolean returned=false;
   while(r.steps<end) {
    Instruction ins=getInstructionAt(toAddr(r.pc()));require(ins!=null,"gate decode");
    if(ins.getFlowType().isCall()){target=ins.getFlows()[0].getOffset();break;}
    if(!r.step()){returned=true;break;}
   }
   boolean admitted=flag!=0&&active==1&&status==5;
   require(admitted?target==(mode==2?0xc45188:0xc4520c):returned&&target==0,"interrupt gate");cases++;
  }
  println("CALLBACK_RESULT={\"cases\":"+cases+",\"registration_cases\":1,\"error_callbacks\":10,\"page_completions\":8,\"interrupt_gate_prefixes\":16}");
  println("PASS storage callbacks; synthetic page comparison, explicit thunk shim");
 }
}
