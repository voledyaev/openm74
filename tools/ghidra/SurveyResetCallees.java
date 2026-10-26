/* Traverse the reviewed image's directly reachable reset/preparation code.
 * Static only: no firmware execution or device access. Indirect/absent flows are
 * explicit frontiers. This does not resolve data-dependent stores or interrupts.
 * @category openm74
 */
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.program.model.listing.Instruction;
import java.security.MessageDigest;
import java.util.*;

public class SurveyResetCallees extends GhidraScript {
 void require(boolean ok,String message){if(!ok)throw new IllegalStateException(message);}
 String quoted(String s){return "\""+s.replace("\\","\\\\").replace("\"","\\\"")+"\"";}
 String address(long a){return String.format("%06x",a);}
 // Exact-image static fallthrough annotations, not a decoder or p-code patch.
 // Infineon C166S V2 UM V1.7: sections 7 (qqq=2 => postincrement),
 // CoMULu/CoSHL/CoMACu/CoSUBR encodings. MRW repeat field is zero.
 // Reads and accumulator/flag effects are NOT executed or abstracted as no-ops.
 final Map<Long,String[]> reviewedDsp=Map.of(
  0xc5ea76L,new String[]{"83400002","comulu r4,[r0+]"},
  0xc5ea7aL,new String[]{"a3008210","coshl #0x10"},
  0xc5ea7eL,new String[]{"83401002","comacu r4,[r0+]"},
  0xc5ea84L,new String[]{"83201202","cosubr r2,[r0+]"});

 public void run() throws Exception {
  long size=currentProgram.getMemory().getBlock(toAddr(0xc00000)).getSize();
  require(size==851968,"unexpected image size");byte[] image=new byte[(int)size];
  require(currentProgram.getMemory().getBytes(toAddr(0xc00000),image)==size,"short image");
  String hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(image));
  require(hash.equals("166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"),"unreviewed image");
  println("IMAGE_SHA256="+hash);
  Deque<Long> pending=new ArrayDeque<>();pending.add(0xc0d572L);
  Set<Long> visited=new TreeSet<>(),callTargets=new TreeSet<>();
  List<String> calls=new ArrayList<>(),frontiers=new ArrayList<>(),watch=new ArrayList<>(),annotations=new ArrayList<>();
  int resets=0,returns=0;
  while(!pending.isEmpty()) {
   require(!monitor.isCancelled(),"cancelled");
   long at=pending.removeFirst();if(!visited.add(at))continue;
   require(visited.size()<=40000,"instruction budget exceeded");
   if(at<0xc00000||at>=0xc00000+size){frontiers.add("{\"address\":"+quoted(address(at))+",\"kind\":\"outside-image\"}");continue;}
   if(reviewedDsp.containsKey(at)) {
    String[] spec=reviewedDsp.get(at);byte[] bytes=new byte[4];
    require(currentProgram.getMemory().getBytes(toAddr(at),bytes)==4,"short DSP instruction");
    require(HexFormat.of().formatHex(bytes).equals(spec[0]),"reviewed DSP bytes changed");
    annotations.add("{\"address\":"+quoted(address(at))+",\"instruction\":"+quoted(spec[1])
     +",\"length\":4,\"flow\":\"fallthrough\"}");
    pending.add(at+4);continue;
   }
   Instruction i=getInstructionAt(toAddr(at));
   if(i==null){new DisassembleCommand(toAddr(at),null,false).applyTo(currentProgram,monitor);i=getInstructionAt(toAddr(at));}
   if(i==null) {
    frontiers.add("{\"address\":"+quoted(address(at))+",\"kind\":\"decode-failure\"}");
    continue;
   }
   String m=i.getMnemonicString().toLowerCase(),text=i.toString().toLowerCase();
   if(m.equals("srst")||text.contains("0xf0b8")||text.contains("0xf0ba")
     ||text.contains("0xf0b2")||text.contains("0xc002bc")||text.contains("0xc00d4a"))
    watch.add("{\"address\":"+quoted(address(at))+",\"instruction\":"+quoted(text)+"}");
   if(m.equals("srst")){resets++;continue;}
   if(m.startsWith("ret")){returns++;continue;}
   boolean transfer=i.getFlowType().isCall()||i.getFlowType().isJump();
   if(transfer) {
    if(i.getFlows().length==0)frontiers.add("{\"address\":"+quoted(address(at))+",\"kind\":\"indirect-transfer\",\"mnemonic\":"+quoted(m)+"}");
    for(var target:i.getFlows()) {
     long destination=target.getOffset();pending.add(destination);
     if(i.getFlowType().isCall()) {
      callTargets.add(destination);
      calls.add("{\"site\":"+quoted(address(at))+",\"target\":"+quoted(address(destination))+"}");
     }
    }
   }
   if(i.getFallThrough()!=null)pending.add(i.getFallThrough().getOffset());
   else if(!transfer)frontiers.add("{\"address\":"+quoted(address(at))+",\"kind\":\"terminal-or-unmodeled-flow\",\"mnemonic\":"+quoted(m)+"}");
  }
  StringJoiner targets=new StringJoiner(",");for(long target:callTargets)targets.add(quoted(address(target)));
  println("CALLEE_RESULT={\"root\":\"c0d572\",\"visited_addresses\":"+visited.size()
   +",\"direct_call_targets\":["+targets+"],\"call_edges\":["+String.join(",",calls)
   +"],\"return_sites\":"+returns+",\"srst_sites\":"+resets+",\"frontiers\":["+String.join(",",frontiers)
   +"],\"reset_literal_watch\":["+String.join(",",watch)+"],\"reviewed_dsp_fallthrough\":["+String.join(",",annotations)+"]}");
  println("PASS reset-callee survey; static direct flows only; indirect stores, interrupts and runtime effects unproved");
 }
}
