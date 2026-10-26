/* Execute exact-image serial helpers with synthetic held-ready status/RX bytes.
 * Command ports are emulator RAM; no peripheral timing, external device or reset
 * preparation reachability is modeled. CALLS/RETS/address shims are explicit.
 * @category openm74
 */
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.program.model.listing.Instruction;
import java.security.MessageDigest;
import java.util.*;

public class CheckSerialPreparation extends EmulateCanLoader {
 final Set<Long> helpers=Set.of(0xc08b0eL,0xc08bd6L,0xc08c04L,0xc08c54L,0xc08c32L);
 void decode(long start,long end) throws Exception {
  clearListing(toAddr(start),toAddr(end-1));
  for(long at=start;at<end;) {
   new DisassembleCommand(toAddr(at),null,false).applyTo(currentProgram,monitor);
   Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"decode gap "+Long.toHexString(at));at+=ins.getLength();
  }
 }
 void expect(long at,String text) throws Exception {
  clearListing(toAddr(at),toAddr(at+3));
  new DisassembleCommand(toAddr(at),null,false).applyTo(currentProgram,monitor);
  Instruction ins=getInstructionAt(toAddr(at));
  require(ins!=null&&ins.toString().equalsIgnoreCase(text),"caller instruction changed");
 }
 boolean tick(Run r) throws Exception {
  Instruction ins=getInstructionAt(toAddr(r.pc()));require(ins!=null,"missing instruction");
  if(ins.getMnemonicString().equalsIgnoreCase("calls")) {
   long target=ins.getFlows()[0].getOffset();
   if(target==0xc0272c){r.pc(r.pc()+ins.getLength());r.steps++;return true;} // No watchdog/time model.
   require(helpers.contains(target),"unreviewed callee "+Long.toHexString(target));
  }
  return r.step();
 }
 Run fixture(int address,int count,boolean ready) throws Exception {
  Run r=new Run();r.cpu.writeRegister("r12",address);r.cpu.writeRegister("r13",count);
  r.put(0x204800,new byte[256]);r.word(0x204844,ready?0xc000:0);
  r.word(0xfd36,0);r.word(0xffb6,0);
  byte[] data=new byte[32];for(int n=0;n<data.length;n++)data[n]=(byte)(7+3*n);
  r.put(0xe023eb,data);return r;
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size==851968,"unexpected size");byte[] raw=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),raw)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"unreviewed image");
  println("IMAGE_SHA256="+hash);decode(0xc08b0e,0xc08c82);decode(0xc10060,0xc1034e);
  int[] constants={320,336,656,976,1296,1312,1328};
  for(int n=0;n<constants.length;n++)require(
   (currentProgram.getMemory().getShort(toAddr(0xc81bbe+n*2))&65535)==constants[n],"caller table changed");
  expect(0xc120cc,"mov r9,0x5bc0");expect(0xc12204,"mov r9,0x5bc2");
  expect(0xc12336,"mov r9,0x5bc4");expect(0xc121ca,"mov r12,0x5bc6");
  expect(0xc12302,"mov r12,0x5bc8");expect(0xc12434,"mov r12,0x5bca");
  expect(0xc1217a,"calls 0xc10060");expect(0xc12188,"calls 0xc101f8");
  expect(0xc121a4,"cmpb RL7,RL2");expect(0xc121a8,"mov r8,#0x3");
  expect(0xc122b2,"calls 0xc10060");expect(0xc122c0,"calls 0xc101f8");
  expect(0xc122dc,"cmpb RL7,RL2");expect(0xc122e0,"mov r8,#0x3");
  expect(0xc123e4,"calls 0xc10060");expect(0xc123f2,"calls 0xc101f8");
  expect(0xc1240e,"cmpb RL7,RL2");expect(0xc12412,"mov r8,#0x3");
  expect(0xc121c4,"cmp r6,0x5bbe");expect(0xc122fc,"cmp r6,0x5bbe");
  expect(0xc1242e,"cmp r6,0x5bbe");

  int cases=0;
  for(boolean write:new boolean[]{false,true})for(int address:new int[]{0x10,0x1f0,0x1234,0xfffe})
   for(int count:new int[]{0,1,2,16})try(Run r=fixture(address,count,true)) {
    r.pc(write?0xc10060:0xc101f8);List<Integer> tx=new ArrayList<>(),select=new ArrayList<>();
    int rx=0;boolean returned=false;
    while(r.steps<20000) {
     long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"undecoded PC");
     boolean send=ins.toString().toLowerCase().startsWith("mov 0x880,");
     if(at==0xc10328)r.word(0x20485c,0x80+rx++);
     int old=r.mem(0xffb6,1)[0]&4;boolean more=tick(r);int now=r.mem(0xffb6,1)[0]&4;
     if(now!=old)select.add(now==0?0:1);
     if(send)tx.add((r.mem(0x204880,1)[0]&255)|((r.mem(0x204881,1)[0]&255)<<8));
     if(!more){returned=true;break;}
    }
    require(returned,"serial helper budget");int actual=Math.max(1,count);
    List<Integer> expected=new ArrayList<>();if(write)expected.add(6);
    expected.add(write?2:3);expected.add(address>>8);expected.add(address&255);
    for(int n=0;n<actual;n++)expected.add(write?7+3*n:0);
    require(tx.equals(expected),"transmit sequence mismatch "+tx+" expected "+expected);
    require(select.equals(write?Arrays.asList(1,0,1,0,1):Arrays.asList(1,0,1)),"select transitions "+select);
    if(!write){require(rx==actual,"RX count");for(int n=0;n<actual;n++)require((r.mem(0xe023eb+n,1)[0]&255)==0x80+n,"RX destination");}
    cases++;
   }
  for(boolean write:new boolean[]{false,true})try(Run r=fixture(0x1234,1,false)) {
   r.pc(write?0xc10060:0xc101f8);int polls=0;
   while(r.steps<1000){if(r.pc()==(write?0xc100b0:0xc10248))polls++;require(tick(r),"not-ready returned");}
   require(polls>1,"not-ready prefix missing");cases++;
  }
  println("SERIAL_RESULT={\"cases\":"+cases+",\"ready_scenarios\":32,\"not_ready_prefixes\":2,\"zero_length_transfers_one_byte\":true,\"caller_static_payload_length\":320,\"caller_static_data_addresses\":[336,656,976],\"caller_static_metadata_addresses\":[1296,1312,1328]}");
  println("PASS serial-preparation synthetic p-code; no external device or timing");
 }
}
