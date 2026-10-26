/* Synthetic flash-status execution only. No flash physics, ECU I/O or timing.
 * Reuses the reviewed C166 address/call shims; calls into flash helpers are
 * explicitly entered below instead of using the parent harness's ready stubs.
 * @category openm74
 */
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.program.model.listing.Instruction;
import java.security.MessageDigest;
import java.util.HexFormat;

public class CheckFlashCompletion extends EmulateCanLoader {
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  byte[] raw=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"unreviewed image");
  println("IMAGE_SHA256="+hash);
  clearListing(toAddr(0xc009ca),toAddr(0xc00af3));
  for(long at=0xc009ca;at<0xc00af4;) {
   new DisassembleCommand(toAddr(at),null,false).applyTo(currentProgram,monitor);
   Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"decode gap");at+=ins.getLength();
  }
  int cases=0;
  // Independent combinations: PROG, ERASE, SQER, OPER, PROER. Other bits
  // remain zero; these fixtures do not claim exhaustive peripheral semantics.
  for(int mask=0;mask<32;mask++)try(Run r=new Run()) {
   int op=(mask&3)|((mask&4)<<2)|((mask&8)<<2),prot=(mask&16);
   r.cpu.writeRegister("r12",0x2000);r.cpu.writeRegister("r13",0xc0);
   r.word(0xffff06,0);r.word(0xffff08,op);r.word(0xffff0a,prot);
   r.pc(0xc009ca);int clear=0,reset=0;
   while(true) {
    if(r.pc()==0xc00a26)clear++;
    if(r.pc()==0xc00a4c)reset++;
    require(r.steps<200,"status budget");if(!r.step())break;
   }
   require(clear==((mask&3)!=0?1:0),"clear-status condition");
   require(reset==((mask&28)!=0?1:0),"reset-to-read condition");
   require(r.reg("r4")==((mask&28)!=0?1:0),"error return");cases++;
  }
  for(int bit=0;bit<3;bit++)try(Run r=new Run()) {
   r.word(0xffff06,1<<bit);r.pc(0xc009ca);int polls=0;
   while(r.steps<200){if(r.pc()==0xc009cc)polls++;require(r.step(),"busy returned");}
   require(polls>1&&r.tx==0,"busy polling");cases++;
  }
  // Enter the actual program helper. Peripheral writes are plain emulator RAM;
  // synthetic status is injected at each status-helper entry. No data is flashed.
  for(int mode=0;mode<3;mode++)try(Run r=new Run()) {
   r.cpu.writeRegister("r12",0x2000);r.cpu.writeRegister("r13",0xc0);
   r.cpu.writeRegister("r14",0xa500);r.cpu.writeRegister("r15",0);
   r.put(0xa500,new byte[128]);r.word(0xffff06,0);r.pc(0xc00a82);
   int loads=0,starts=0,polls=0,words=0;boolean returned=false;
   while(r.steps<4000) {
    long at=r.pc();
    if(at==0xc00a9a)loads++;
    if(at==0xc00ac2)words++;
    if(at==0xc00ad4)starts++;
    if(at==0xc009ca) {
     polls++;r.word(0xffff08,mode==0||mode==1&&polls>1?0:0x10);r.word(0xffff0a,0);
    }
    if(at==0xc00ae6){r.returns.push(0xc00aeaL);r.pc(0xc009ca);r.steps++;continue;}
    if(!r.step()){returned=true;break;}
   }
   require(loads==1&&starts==1&&words==64,"page command unexpectedly repeated");
   require(mode==2?!returned&&polls>2:returned&&polls==(mode==0?1:2),"completion behavior");cases++;
  }
  println("FLASH_RESULT={\"cases\":"+cases+",\"status_combinations\":32,\"busy_prefixes\":3,\"program_scenarios\":3}");
  println("PASS flash-completion synthetic p-code; no flash effects or timing");
 }
}
