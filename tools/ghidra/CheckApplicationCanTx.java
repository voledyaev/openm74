/* Synthetic application CAN transmit path, exact stock image only.
 * Real software buffer/context/driver p-code; CAN registers are initialized RAM.
 * No arbitration, ACK, interrupt timing, security or reset execution.
 * @category openm74
 */
import ghidra.program.model.listing.Instruction;
import java.security.MessageDigest;
import java.util.*;
import java.util.regex.*;
public class CheckApplicationCanTx extends CheckApplicationReset {
 void executeTx(ParserRun r,long entry) throws Exception {
  Deque<Long> stack=new ArrayDeque<>();r.cpu.writeRegister(r.cpu.getPCRegister(),entry);
  Set<Long> allowed=Set.of(0xc47c7eL,0xc49b4cL,0xc4816aL,0xc48178L,
    0xc47586L,0xc4771cL,0xc477b0L,0xc46f84L,0xc46a08L,0xc46a2cL,0xc481aeL,0xc49bc6L,0xc49c18L);
  for(int steps=0;steps<20000;steps++) {
   long at=r.cpu.getExecutionAddress().getOffset();Instruction i=getInstructionAt(toAddr(at));
   if(i==null)i=decode(at);String m=i.getMnemonicString().toLowerCase();
   if(m.equals("calls")) {
    long target=i.getFlows()[0].getOffset();require(allowed.contains(target),"unreviewed transport call "+Long.toHexString(target)+" at "+Long.toHexString(at)+" args="+r.cpu.readRegister("r12")+","+r.cpu.readRegister("r13")+","+r.cpu.readRegister("r14")+","+r.cpu.readRegister("r15"));
    stack.push(at+i.getLength());r.cpu.writeRegister(r.cpu.getPCRegister(),target);continue;
   }
   if(m.equals("rets")) {
    if(stack.isEmpty())return;r.cpu.writeRegister(r.cpu.getPCRegister(),stack.pop());continue;
   }
   // Execute one real instruction with the parser's reviewed EXTP/address shims.
   boolean extension=m.equals("extp");
   if(extension) {
    Matcher match=Pattern.compile("extp\\s+(\\S+),\\s*#?(0x[0-9a-f]+|\\d+)")
     .matcher(i.toString().toLowerCase());require(match.find(),"unparsed transport EXTP");
    String value=match.group(1).replace("#","");var register=currentProgram.getRegister(value);
    r.page=(register==null?Long.decode(value):r.cpu.readRegister(register).longValue())&0x3ff;
    r.left=Integer.decode(match.group(2));
   }
   require(!m.equals("srst")&&!m.equals("exts"),"unexpected transport reset/address extension");
   require(r.cpu.step(monitor),"transport p-code failure at "+Long.toHexString(at)+": "+r.cpu.getLastError());
   if(!extension&&r.left>0)r.left--;
  }
  throw new IllegalStateException("transport instruction budget");
 }

 void word(ParserRun r,long address,int value){r.put(address,new byte[]{(byte)value,(byte)(value>>8)});}
 int word(ParserRun r,long address){return r.byteAt(address)|(r.byteAt(address+1)<<8);}
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size==851968,"size");byte[] image=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),image)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(image));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"unreviewed image");
  println("IMAGE_SHA256="+hash);
  for(long[] range:new long[][]{{0xc49afc,0xc49b1e},{0xc49b4c,0xc49c4a},
    {0xc47c7e,0xc47d98},{0xc47e2a,0xc481d2},{0xc47586,0xc47646},
    {0xc4771c,0xc47734},{0xc477b0,0xc477c8},{0xc46f84,0xc470fa},
    {0xc46a08,0xc46a50}}) {
   clearListing(toAddr(range[0]),toAddr(range[1]-1));
   for(long at=range[0];at<range[1];){Instruction ins=decode(at);at+=ins.getLength();}
  }
  byte[] initialized=initializedRouteRam(image);int cases=0;
  for(int length:new int[]{1,2,7})for(int busy:new int[]{0,4,256})
   try(ParserRun r=new ParserRun()) {
   r.cpu.writeRegister("DPP0",0x321);r.cpu.writeRegister("DPP1",0x320);
   r.cpu.writeRegister("DPP2",0x322);r.cpu.writeRegister("DPP3",3);
   r.cpu.writeRegister("r0",0xd800);r.put(0xc702,initialized);
   r.put(0xc650,new byte[]{1});word(r,0xc644,3);word(r,0xc642,2);
   r.put(0xf9b1,new byte[]{1});r.put(0xf927,new byte[]{1});
   r.put(0x201b80,new byte[32]);word(r,0x201b9c,0x1400|busy);
   byte[] payload=new byte[length];for(int n=0;n<length;n++)payload[n]=(byte)(0x51+n);
   r.put(0xfb53,payload);r.put(0xe01704,new byte[]{(byte)length});
   executeTx(r,0xc49afc);require(r.byteAt(0xc663)==1,"TX context not armed");
   executeTx(r,0xc47e2a);
   if(busy!=0) {
    require(word(r,0x201b9a)==0&&r.byteAt(0x201b90)==0,"busy object overwritten");
    require(r.byteAt(0xc662)==8&&r.byteAt(0xc663)==1,"busy frame not retained");
    byte[] changed=new byte[length];Arrays.fill(changed,(byte)0xe0);r.put(0xfb53,changed);
    word(r,0x201b9c,0x1400);executeTx(r,0xc47e2a);
   }
   require((word(r,0x201b9a)&0x1fff)==(0x7e8<<2),"TX ID mismatch");
   require((word(r,0x201b82)>>8&15)==8,"TX DLC");
   require(r.byteAt(0x201b90)==length,"single-frame count");
   for(int n=0;n<length;n++)require(r.byteAt(0x201b91+n)==(payload[n]&255),"TX payload");
   for(int n=length+1;n<8;n++)require(r.byteAt(0x201b90+n)==0xaa,"TX padding");
   require(word(r,0x201b9e)==0x308,"TX request command");cases++;
  }
  println("APP_TX_RESULT={\"cases\":"+cases+",\"tx_id\":2024,\"message_object\":92,\"lengths\":[1,2,7],\"busy_bits\":[2,8]}");
  println("PASS application-can-tx synthetic p-code; no wire transmission");
 }
}
