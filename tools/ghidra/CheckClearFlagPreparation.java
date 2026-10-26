/* Clear-flag reset helpers with real packing/control p-code and explicit manager shims.
 * Storage initialization/submission and asynchronous status are modeled; watchdog
 * calls are skipped. Separate low-level API prefixes stop before their worker call.
 * No full initialization, IRQ, flash physics, reset or ECU I/O.
 * @category openm74
 */
import ghidra.program.model.listing.Instruction;
import java.security.MessageDigest;
import java.util.*;
import java.io.ByteArrayOutputStream;

public class CheckClearFlagPreparation extends CheckResetPreparation {
 byte[] payload(Run r,long descriptor) throws Exception {
  ByteArrayOutputStream bytes=new ByteArrayOutputStream();
  for(int n=1;n<1000;n++) {
   long at=descriptor+8*n;
   int length=getByte(toAddr(at+4))&255;if(length==0)return bytes.toByteArray();
   int offset=getShort(toAddr(at))&65535,page=getShort(toAddr(at+2))&65535;
   bytes.write(r.mem(((long)page<<14)|(offset&0x3fff),length));
  }
  throw new IllegalStateException("descriptor budget");
 }
 void seedSources(Run r) throws Exception {
  Set<Long> addresses=new TreeSet<>();
  for(long descriptor:new long[]{0xc8169c,0xc8132c,0xc8150c})for(int n=1;n<1000;n++) {
   long at=descriptor+8*n;int length=getByte(toAddr(at+4))&255;if(length==0)break;
   long source=((long)(getShort(toAddr(at+2))&65535)<<14)|(getShort(toAddr(at))&0x3fff);
   for(int j=0;j<length;j++) {
    long address=source+j;
    require(address<65536||address>=0xe00000&&address<0xe04000,"source outside modeled RAM");
    addresses.add(address);
   }
  }
  for(long address:addresses)r.put(address,new byte[]{(byte)(address*17+9)});
  r.put(0xc4e0,new byte[]{0});r.word(0xc4e2,0);
 }
 long buffer(int id){return id<2?0xe032a4:id==4?0xe030a4:id==5?0xe03da4:0xe03ba4;}
 void verifyPacked(Run r,int id,byte[] expected) throws Exception {
  int size=id<2?2048:512;long base=buffer(id);
  require(Arrays.equals(r.mem(base+12,expected.length),expected),"packed payload "+id);
  for(int n=12+expected.length;n<size;n++)require((r.mem(base+n,1)[0]&255)==165,"retained tail "+id);
 }
 void helper(Run r,int which,int terminal,int submission,boolean reject,boolean forever) throws Exception {
  seedSources(r);
  List<Integer> initialized=new ArrayList<>(),submitted=new ArrayList<>();
  Map<Integer,byte[]> expected=new HashMap<>();int polls=0,watchdogs=0;boolean returned=false;
  r.cpu.writeRegister("r12",1);r.pc(which==0?0xc4577c:0xc45720);
  long end=r.steps+200000;
  while(r.steps<end) {
   long at=r.pc();Instruction ins=getInstructionAt(toAddr(at));require(ins!=null,"helper decode");
   if(ins.getFlowType().isCall()) {
    require(ins.getFlows().length==1,"indirect call");long target=ins.getFlows()[0].getOffset();
    if(target==0xc447fe) {
     int id=(int)r.reg("r12");initialized.add(id);
     require(which==0?id>=4&&id<=6:id==0||id==1,"initialization id");
     byte[] fill=new byte[id<2?2048:512];Arrays.fill(fill,(byte)0xa5);r.put(buffer(id),fill);
     expected.put(id,payload(r,id<2?(id==0?0xc8132c:0xc8150c):0xc8169c));
     r.cpu.writeRegister("r4",0);r.pc(at+ins.getLength());r.steps++;continue;
    }
    if(target==0xc4491a) {
     int id=(int)r.reg("r12");submitted.add(id);polls=0;
     require(r.reg("r13")==1,"submission mode");verifyPacked(r,id,expected.get(id));
     if(!reject){r.cpu.writeRegister("r4",submission);r.pc(at+ins.getLength());r.steps++;continue;}
     // C4E0 stays zero: enter the real API's not-ready return, no nested worker.
    } else if(target==0xc44a20) {
     polls++;if(!reject)r.word(0xc4e2,forever||polls==1?1:terminal);
    } else if(target==0xc0272c) {
     watchdogs++;r.pc(at+ins.getLength());r.steps++;continue;
    } else require(target==0xc455fa,"unreviewed call "+Long.toHexString(target));
   }
   require(!ins.getMnemonicString().equalsIgnoreCase("srst"),"reset");
   if(!r.step()){returned=true;break;}
  }
  List<Integer> ids=which==0?Arrays.asList(4,5,6):Arrays.asList(0,1);
  if(forever)require(!returned&&submitted.equals(Arrays.asList(ids.get(0)))&&polls>2&&watchdogs>2,"held-busy prefix");
  else {
   require(returned&&initialized.equals(ids)&&submitted.equals(ids),"call sequence");
   require(r.reg("r4")==2,"outer result changed");
   if(!reject)require(watchdogs==ids.size(),"busy status did not service watchdog");
  }
  require(!r.cpu.getTrackedMemoryWriteSet().intersects(toAddr(0xf0b8),toAddr(0xf0bb)),"reset register write");
 }
 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size==851968,"size");byte[] image=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),image)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(image));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"unreviewed image");
  println("IMAGE_SHA256="+hash);
  decode(0xc0d572,0xc0d6be);decode(0xc455fa,0xc459fa);decode(0xc4491a,0xc44a26);
  decode(0xc4505a,0xc450e8);int cases=0;
  for(int id=4;id<=6;id++) {
   long record=0xc82a4a+id*24;
   require((getShort(toAddr(record))&65535)==0x8000+(id-4)*0x2000,"slot offset");
   require((getShort(toAddr(record+4))&65535)==512&&(getShort(toAddr(record+6))&65535)==16,"slot geometry");
  }
  try(Run r=fixture(0)) {
   require(payload(r,0xc8169c).length==315&&payload(r,0xc8132c).length==1955&&payload(r,0xc8150c).length==1886,"descriptor lengths");
  }
  for(int which=0;which<2;which++)for(int terminal:new int[]{0,3})for(int submission:new int[]{0,1})try(Run r=fixture(0)) {
   root(r,0,which==0?0xc0d69e:0xc0d6ac);helper(r,which,terminal,submission,false,false);cases++;
  }
  for(int which=0;which<2;which++)try(Run r=fixture(0)) {
   root(r,0,which==0?0xc0d69e:0xc0d6ac);helper(r,which,0,1,true,false);cases++;
  }
  for(int which=0;which<2;which++)try(Run r=fixture(0)) {
   root(r,0,which==0?0xc0d69e:0xc0d6ac);helper(r,which,1,0,false,true);cases++;
  }
  for(int offset:new int[]{0x8000,0xa000,0xc000})try(Run r=fixture(0)) {
   r.cpu.writeRegister("r12",offset);r.cpu.writeRegister("r13",0);
   r.cpu.writeRegister("r14",0x30a4);r.cpu.writeRegister("r15",0xe0);
   r.put(0xc60c,new byte[]{1});r.word(0xd800,512);r.word(0xd802,0);r.pc(0xc4505a);
   long end=r.steps+200;
   while(r.pc()!=0xc450e0&&r.steps<end)require(r.step(),"API returned before worker boundary");
   require(r.pc()==0xc450e0&&r.dword(0xc62a)==0xcc0000L+offset,"API target mapping");cases++;
  }
  for(boolean ready:new boolean[]{false,true})try(Run r=fixture(0)) {
   r.cpu.writeRegister("r12",0xffff);r.cpu.writeRegister("r13",0);
   r.cpu.writeRegister("r14",0x30a4);r.cpu.writeRegister("r15",0xe0);
   r.put(0xc60c,new byte[]{(byte)(ready?1:0)});r.word(0xd800,512);r.word(0xd802,0);
   r.invoke(0xc4505a,200);require(r.reg("r4")== (ready?7:1),"API rejection");cases++;
  }
  println("CLEAR_RESULT={\"cases\":"+cases+",\"packing_status_cases\":8,\"real_rejection_cases\":2,\"held_busy_prefixes\":2,\"api_target_prefixes\":3,\"api_rejections\":2,\"payload_lengths\":[315,1955,1886],\"relative_offsets\":[32768,40960,49152]}");
  println("PASS clear-flag preparation; manager shims and API prefixes, no flash effects");
 }
}
