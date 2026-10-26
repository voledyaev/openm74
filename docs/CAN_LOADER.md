# Flash-resident CAN loader: offline analysis

Status: read-only analysis on macOS with Ghidra 12.1.4. No CAN traffic was sent and no
ECU operation was performed. Detailed disassembly covers representative boot variants, with a byte survey of further
images. This is not a verified programming procedure or a new supported transport.

## Sample and scope

Image: I444GI04, 851968 bytes, mapped at `0xC00000`, SHA256
`166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b`.
The loaded Ghidra memory was hashed independently and matched the file.
This is not the historical bench reference identified in AGENTS.md; its identity must
not be substituted for that reference. Images and raw listings are kept outside this
repository because a dump can contain donor identifiers.

The examined boot sector contains CAN initialization, a startup exchange, a command
loop and flash erase/program routines. This supports the existence of a flash-resident
CAN loader, separate from the mask-ROM BSL selected by boot pins. It does not establish
that this loader can upload a full image or that other firmware versions behave alike.

## Reset path

| Location | Observed instruction or destination |
|---|---|
| `0xC00000` | Jump to `0xC002A2` |
| `0xC002A2` | Read `0xF0B2`, mask with `0x3000`, compare with `0x3000` |
| Equal branch | Jump to application entry `0xC02000` |
| Unequal branch | Jump to boot runtime initialization `0xC00D4A` |
| `0xC00D92` | Call loader entry `0xC002BC` |
| `0xC0021A` | Write `0x3000` to `0xF0B8` during loader initialization |
| `0xC002CE` | Call bounded startup exchange `0xC002E4` |
| Unsuccessful exchange | Execute `SRST` at `0xC002DA` |
| Successful exchange | Enter command loop `0xC0059C` |

The XC2000 system manual identifies `0xF0B2` as `RSTSTAT0`; the tested bits are
`CPU[13:12]`. A value of `11` reports an application reset caused by the CPU reset
trigger. `0xF0B8` is `RSTCON0`; writing `0x3000` selects that reset type for `SRST`.
The resulting static interpretation is a startup opportunity followed by a reset into
the application when the exchange fails. This is not a check of an external programming
pin. It assumes execution reached the flash reset vector; selecting a ROM BSL is a
separate earlier decision.

The application initialization loads `RSTCON0`'s address at `0xC0247C`, ORs in
`0x3000` and stores it at `0xC02486`. Its reset routine and selected callers are
examined below; a software route into the resident loader is still unestablished.

### Application reset path: one exact image

This analysis covers the I444GI04 image identified above, not all boot-prefix matches.
Application bytes can differ despite an identical boot sector. The reset routine starts
at `0xC0D572`: it copies `r12` to `r1`, compares its low byte with `0x90`, and branches
to the tail at `0xC0D6B6` when unequal. That tail is `NOP`, then `SRST` at `0xC0D6B8`.
Equality instead enters preparation at `0xC0D57C`, with RAM/peripheral writes and helper
calls before the same reset tail. The value is a function argument, not an established
wire command. The helpers have a bounded static survey below; their transitive effects
and all earlier state transitions are not fully audited here.

The short unequal path contains no change of reset configuration. If the application's
observed configuration remains in force, its CPU application reset would select the
application branch at the flash reset discriminator, not reopen the CAN startup exchange.
This conditional result explains why finding a reset routine alone does not establish
software loader entry. It does not rule out another path changing reset configuration,
a different reset source, a direct jump or additional logic inside unexamined helpers.

An aligned raw scan finds seven direct `CALLS` candidates to this routine, each decoded
as that call. Selected surrounding branches give the following local conditions; these
are not a complete reachability, authorization or transport audit:

| Call sites | Local buffer conditions and argument |
|---|---|
| `0xC507D2`, `0xC5D208`, `0xC5D28A` | Byte at `0xDE74` equals `0x11`, byte at `0xDE7B` equals `0x01`; argument comes from `0xDE64` |
| `0xC6050E`, `0xC6AA1A`, `0xC6AAAC`, `0xC6AB6C` | Byte at `0xDEF8` equals `0x11`, byte at `0xDEF9` equals `0x01`; argument comes from `0xDEE9`, with `0x91` excluded on these branches |

The reset-specific producers and second path's software CAN route are identified below;
the first physical transport and full diagnostic-session/security prerequisites remain unresolved. Do not translate this table into a
CAN request or assume that a standard service with similar values implements the path.
The raw call scan does not find indirect calls, near branches or all possible entries.
The full-image aligned `SRST` opcode scan finds two occurrences at `0xC002DA` and
`0xC0D6B8`; opcode occurrence is not a proof of executable reachability or absence of
other reset mechanisms.

The reset-prefix part of `tools/ghidra/CheckApplicationReset.java` executes only the short
register/branch prefix in fresh Ghidra emulator instances. All 256 low-byte values are checked with high bytes
`0x00`, `0x01`, `0x80`, `0xFF`: 1024 cases, of which 4 reach preparation and 1020 reach
the reset-tail `NOP`. Execution stops before that instruction or at preparation entry;
no `SRST`, preparation helper, CPU reset or peripheral effect is emulated. The decoder's
`NOP` userop is outside this check. A separate wrapper validates the exact image hash,
loaded-memory hash and success marker; Ghidra exit status alone is insufficient.
[The report](data/application-reset.json) contains metadata and addresses, not firmware bytes.

```
python tools/check_application_reset.py IMAGE.bin --report reset-evidence.json
```

The tool refuses other images rather than applying this application's addresses to a
whole family. The parser and static helper survey below add local evidence; neither
the identified call sites nor the bounded branch check establishes a working software
entry to the CAN loader.

### Preparation state marker and parameter source

The preparation branch writes `0x2222` at `0xC0D672` into offset `0x2406` using
`EXTP 0x380`, i.e. physical address `0xE02406`. A consumer at `0xC033B4` reads that
same cell, compares it with `0x55AA` and branches to `0xC03488` on mismatch. Other
examined application paths write `0x55AA` at `0xC06114` and clear the cell at `0xC08232`.
Thus the preparation value fails this application's state-validity comparison if it
survives until the read. Neither RAM retention nor intervening writes across reset have
been tested. The flash reset discriminator described above reads reset status, not this
cell; treating the value as a proven loader-entry flag is unsupported.

The reset-specific producers are identified in the parser section below. Several
services write the same argument cells; a matching destination alone cannot connect a
producer to a particular reset call. The first physical transport remains unresolved;
the second path's software CAN receive route is established below under model assumptions.

The same reproducible check now statically follows direct branch/fallthrough edges in
five preparation helpers. It does not enter their callees or execute any helper. All
conditional branches are included regardless of the actual preparation arguments; these
are bounded CFG surveys, not paths observed in a running ECU:

| Entry | Decoded instruction starts | Direct call sites | Distinct direct callees |
|---|---|---|---|
| `0xC1055E` | 2811 | 408 | 10 |
| `0xC12F10` | 359 | 33 | 8 |
| `0xC4577C` | 241 | 16 | 5 |
| `0xC45720` | 38 | 4 | 1 |
| `0xC10000` | 22 | 1 | 1 |

The helper at `0xC10000` has nested counted loops, `NOP` instructions and a call to
`0xC0272C`, where the examined sequence services the watchdog with `SRVWDT` and returns.
That supports a delay/watchdog role, without establishing milliseconds. The other helpers
have substantial RAM processing and further calls; their transitive peripheral effects
are not established by this survey. Unresolved indirect control flow, decoding failure or
escape from a reviewed helper window fails the check rather than silently truncating it.
The report lists the direct callees excluded from each helper's traversal. It does not
prove the absence of a reset-register write in that larger call graph.

The bounded preparation check below resolves the immediate call arguments and selected
helper prefixes. Complete upstream admission and transitive effects still need analysis;
none of these checks establishes software loader entry.

### Preparation with actual call arguments

`tools/check_reset_preparation.py` executes the top-level `0x90` branch with synthetic
RAM and the reviewed application DPP mapping. It records and skips helper calls, injects
their `r4` results and stops before the reset-tail `NOP` at `0xC0D6B6`. Six cases cover
the branch flag clear/set and results 0, 3 and `0xFFFF`; another case checks argument
`0x01` reaches the tail without preparation calls. No helper completion or reset is
implied by those skipped calls.

The branch at `0xC0D682` reads bit 4 of RAM cell `0xFD1E`. A producer is checked
below; the physical meaning and later mutation of this flag remain unresolved. The recorded call sequences are:

| Flag | Ordered calls and explicit arguments |
|---|---|
| Clear | `0xC4577C` with `r12=1`; `0xC10000` with `r12=0x64`; `0xC45720` with `r12=1`; `0xC10000` with `r12=0x0A` |
| Set | `0xC1055E` with `r12=1`, `r13=0x569C`; `0xC12F10` with `r12=1`, `r13=0x5864`; `0xC10000` with `r12=0x0A` |

The top-level instructions do not inspect `r4` between these calls and the reset tail;
injecting a local failure result does not stop that modeled path. A callee can still
hang, reset, change memory or otherwise fail to return: that behavior is not simulated.
The root writes the marker and other state, including zero to `0x204010`. Memory-write
tracking observes the marker write but no write to `0xF0B8` through `0xF0BB` in these
root-only cases. This is not an absence proof for skipped callees or interrupts.

Four more cases enter the two serial-side helpers with those actual arguments and stop
before their first nested calls: `0xC120DC` and `0xC1317E`. The second helper's early
read call at `0xC12F70` belongs to mode 2 and is not reached by the tested mode 1 prefix.
Independent descriptor parsing and executed copies give:

| Descriptor | Data records | Copied bytes | Bytes included in checksum loop | Retained tail |
|---|---|---|---|---|
| `0xC8169C` | 52 | 315 | 320 | 5 |
| `0xC81864` | 58 | 514 | 528 | 14 |

With scratch buffers initialized to zero or `0xA5`, the tails of buffers at `0xB22C` and `0xB00C`
remain unchanged after packing, and their bytes contribute to the additive checksum.
The first packed bytes also reflect words initialized by the real top-level prefix.
Thus the examined functions do not by themselves make all processed bytes independent
of prior scratch contents. The tails may be reserved or initialized elsewhere; this is
model sensitivity, not an observed ECU fault or proof of undefined startup RAM.

Another 16 cases execute the producer at `0xC031FC` through `0xC0320C`, stopping
before its next call. It sets the flag only when the low byte of the result from
`0xC1034E` is nonzero and logical configuration byte `0x4A52` is zero. Under the
reviewed DPP mapping that byte is at `0xC80A52`, with value 1 in this exact image:
this producer therefore clears the flag, selecting the other preparation chain if it
remains unchanged until reset preparation. Tests cover both initial flag values,
representative result values including `0x0100`, and the stock byte versus a synthetic
zero byte in emulator memory. They do not execute `0xC1034E`, all initialization or
later flag writers. Presence of the serial path alone does not establish its normal
selection on this board/image.

All 27 cases pass. [The report](data/reset-preparation.json) fingerprints the exact
image, scripts and located decoder. RAM is synthetic, boot initialization is not executed,
other root callees are skipped, and numerical/serial callees beyond the prefix boundaries
are not entered. No full preservation transaction, peripheral behavior or software
loader-entry proof is provided.

### Clear-flag preparation: packing and storage-manager boundaries

`tools/check_clear_flag_preparation.py` checks `0xC4577C` and `0xC45720` with
`r12=1`, the arguments used by the clear-flag reset branch. It seeds descriptor-selected
RAM with generated bytes, executes packing and control instructions, and explicitly
models storage initialization/submission/status. It does not execute those manager
operations or a complete root-to-flash transaction. All evidence is for the same exact
stock image.

`0xC4577C` calls initialization for slots 4, 5 and 6, packs the descriptor at
`0xC8169C` into three buffers, then submits those slots with mode 1. `0xC45720`
executes `0xC455FA` for descriptors `0xC8132C` and `0xC8150C`, using slots 0 and 1.
The shared buffer is reused sequentially. In each modeled initialization the destination
is filled with `0xA5`; this replaces the real initialization/read behavior rather than
claiming an erased-memory value.

| Slots | RAM buffer starts | Copied payload | Buffer bytes | Leading bytes before payload | Retained bytes after payload |
|---|---|---|---|---|---|
| 4, 5, 6 | `0xE030A4`, `0xE03DA4`, `0xE03BA4` | 315 | 512 | 12 | 185 |
| 0 | `0xE032A4` | 1955 | 2048 | 12 | 81 |
| 1 | `0xE032A4` | 1886 | 2048 | 12 | 150 |

The test compares every copied byte with the descriptor-selected generated source and
checks that the trailing bytes retain their initial pattern. This establishes packing
boundaries, not the meaning of stored fields or the complete persistent header format.
The copied data and retained tail must not be conflated with a complete backup.

Eight cases vary the injected submission result and final status; two more enter the
real `0xC4491A` not-ready path with control byte `0xC4E0` zero and unchanged status
`0xC4E2` zero. Both higher-level helpers continue through their slots and return local
value 2 in these cases. The submission result is not checked there, and a final status
3 is treated like 0. Value 2 is consequently not independent evidence that contents
were persisted. These are caller counterexamples with initialization skipped, not an
observed failure of the real manager or a full status-code specification.

`0xC44A20` merely reads `0xC4E2`. Two held-status-1 prefixes remain in the wait loops
with watchdog calls and no helper-local attempt limit. The getter does not advance
storage work; the scheduling and completion of other execution paths remain outside
this model. No elapsed time or hardware hang is established.

The reviewed slot records at `0xC82A4A` with stride 24 give slots 4, 5 and 6 relative
starts `0x8000`, `0xA000`, `0xC000`, record size 512 and count 16. Three separate
prefix tests enter lower API `0xC4505A` with those offsets and length 512, and stop
before its worker call at `0xC450E0`. Its queued address becomes `0xCC8000`,
`0xCCA000`, `0xCCC000`, respectively. Two rejection cases cover not-ready state and
an out-of-range interval. Command-port writes are emulator RAM only; the manager's
end-to-end argument flow and asynchronous worker/flash effects are not executed.
This gives a concrete storage-address hypothesis in the tail excluded by the CAN
interface, strengthening the requirement to preserve that tail separately.

All 17 cases pass. [Metadata](data/clear-flag-preparation.json) fingerprints the image,
script, inherited preparation script, address shim and located decoder. Watchdog calls
are counted/skipped, RAM/controller states are synthetic, and no reset, IRQ or device
operation occurs. The manager transitions are checked separately below.


### Storage manager transitions

`tools/check_storage_manager.py` adds 22 exact-image p-code cases with synthetic RAM.
Nine enter `0xC44A26` in state 1, execute its argument setup and replace the call to
`0xC44EE4` with results 0–7 or 65535. Offset `0x8000`, source page `0x380`,
source offset `0x30A4` and stack length 512 reach that boundary. Every injected result
leaves manager state `0xC5E4` at 2 and common status `0xC4E2` at 1: that transition
does not inspect the lower submission result.

Eight separate state-2 entries, one per slot, update the selected record index, clear
its byte flag, and clear both manager state and common status. These cases explicitly
invoke the manager; they do not establish when firmware schedules that invocation.
Five cases execute lower completion `0xC453AC` with callback unset: supplied results
0/1/2/3/5 reach `0xC616`, while `0xC614` and `0xC624` clear. This lower result cell
is distinct from the common status read by the reset helper. Registration and callback routing are checked separately below; these 22 cases alone
do not establish that connection.

Static instruction inspection also separates the earlier `0xC4505A` API from writing:
its worker `0xC450E8` copies flash bytes into RAM. The state-1 manager instead calls
`0xC44EE4`, whose decoded path checks the target and reaches `0xC45426`; programming
and interrupt progress are not executed by these tests. The address prefixes remain
address-mapping evidence, not a completed write. [Metadata](data/storage-manager.json)
fingerprints the image, script, inherited preparation script, address shim and decoder.
No scheduler, IRQ, flash physics, reset or device operation is modeled. Neither local
return nor a cleared common status independently proves persistence.


### Registered callbacks and page completion

`tools/check_storage_callbacks.py` runs 35 further exact-image p-code cases. One starts
at `0xC0243E`, executes argument setup and initializer `0xC44D9E`, and stops at
`0xC02452`. It registers success callback `0xC44A26` in `0xC610`/`0xC612` and error
callback `0xC44A7C` in `0xC618`/`0xC61A`, with synthetic initial RAM/peripheral registers.
Ten cases execute lower completion `0xC453AC` for results 1/2/3/4/6 in manager states
1/2. The error callback clears manager state and maps those states to common status
2/3 respectively, while the lower result remains in `0xC616`.

Eight cases enter page continuation `0xC4520C` with length 128 and a generated page
at `0xCC8000` plus matching source RAM. They cover a matching page and single-byte
mismatches at offsets 0/63/127, each with status `0xFFFF0A` bit 11 clear/set.
The actual code compares 32 four-byte words: mismatch produces lower result 4,
otherwise the set bit produces result 2. Both paths invoke the error callback, leave
the selected record index unchanged and set common status 3. Matching data with the
bit clear clears lower status and calls success: the state-2 manager advances the
selected index and clears common status. These supplied flash bytes represent a
post-operation snapshot, not an executed program command or physical readback.

All callback cases replace the far-return dispatcher `0xC4FFEC` with explicit dispatch
to the pointer loaded by firmware; callback instructions themselves execute. No claim
of system-stack/interrupt emulation follows. Another 16 cases execute the admission
prefix of `0xC4554A`, starting after its watchdog instruction: only `0xFFFF04` bit 15
set, active state 1 and lower status 5 admit a continuation. Mode 2 selects `0xC45188`;
mode 1 selects `0xC4520C`. They stop before the selected call. The decoded interrupt
wrapper at `0xC4559A` calls this worker and ends in `RETI`; interrupt delivery, actual
register side effects and timing are untested. [Metadata](data/storage-callbacks.json)
fingerprints the image, script, inherited preparation script, address shim and decoder.

The registered callbacks therefore explain how successful continuation or an error can
advance the manager without polling doing that work. This is conditional software
flow evidence, not a demonstrated ECU save. Connected sequencing and immediate failures
are checked below; physical command/status semantics remain unvalidated. No CAN writer
or hardware test is enabled.


### Connected erase/program sequence and immediate rejection

`tools/check_storage_sequence.py` adds 19 exact-image scenarios. Ten execute actual
submission `0xC4491A` with mode 1 for slots 0/1/4/5/6, each at an ordinary record
transition and at index wrap. Slot sizes are 2048/2048/512/512/512 bytes. Source RAM
is generated; the real checksum/header instructions and address selection execute.
The script separately represents the flash array: command-port stores remain commands,
not persistent array bytes. Erase effects supply zeros; program effects install the
64 words actually loaded by `0xC45426`. These are explicit synthetic peripheral effects,
not a validated flash controller. Watchdog is skipped, continuation delivery is explicit,
and the indirect callback dispatcher uses the same shim as the preceding test.

The connected path is submission -> erase continuation `0xC45188` -> zero-page pass
via `0xC453D0`/`0xC45426` -> success callback in manager state 1 -> data-page pass
via `0xC44EE4`/`0xC45426`/`0xC4520C` -> success callback in state 2. For these
lengths the erase step is 128 bytes; each data page is also 128 bytes. The model
checks every target in order, the zero loads, two success callbacks, the selected
record index and the final synthetic array against the updated source RAM. It needs
3 continuation invocations per page across all phases; no measured time is implied.
Two additional connected cases corrupt the first data page of slot 4 or the last data
page of slot 0 after the synthetic program effect. The actual comparison produces
lower result 4/common status 3, stops progression and preserves the old record index.
No retry or interrupted-write recovery is established.

Seven separate cases enter the real state-1 manager with controlled faults, without
running the preceding erase phase. Lower not-ready, unaligned target, invalid length,
null source and already-active guards return 1/6/7/5/4 respectively. No callback or
new command occurs, but the manager still writes state 2 and retains common status 1.
With lower status seeded to 0, a subsequent worker invocation leaves that state alone;
this is a bounded software example, not a demonstrated device hang or proof that the
actual reset path can supply these invalid arguments. Two other cases supply a
nonblank target or `0xFFFF0A` bit 11 set: synchronous error callbacks set common status
2 and clear manager state, then the returning manager overwrites its state with 2.
Lower results remain 1/2. These tests expose local ordering and unchecked submission
results; they do not establish the frequency or feasibility of such states on hardware.

All 19 cases pass. [Metadata](data/storage-sequence.json) fingerprints the image,
script, inherited preparation script, address shim and decoder. Physical flash status,
actual interrupt timing, power loss, recovery selection and persistence across reset
remain open. Record validation and local recovery selection are checked below; this
sequence alone cannot establish atomic saving of all slots.


### Record validation and local recovery selection

`tools/check_storage_recovery.py` executes 46 exact-image scenarios. It enters selector
`0xC44AA0` directly with the reviewed slot descriptor and an output-index pointer.
Actual header scanning, full-record reader `0xC44BC0`, lower read API `0xC4505A`/
`0xC450E8` and checksum helper `0xC44C50` execute against generated flash records.
Peripheral registers are held-ready RAM and callbacks are unset. Full startup,
initializer geometry checks, defaults, reset and flash operations are not executed.

Nine scenarios for each slot 0/1/4/5/6 cover: blank region; a single valid copy;
newer copy with bad marker, bad checksum or a partial record; equal counters;
counters `0xFFFFFFFF` versus `0x00000000`; no valid full copy despite a plausible
header; and an ordinary newer valid copy. The generated record has a 12-byte header:
marker at +0, 32-bit counter at +4 and checksum at +8. The checksum is the sum of
all four-byte words modulo `0x100000000`, excluding its own field. These are local
validation fields, not cryptographic integrity or an established cross-slot transaction.

The selected RAM contents and index match the newer valid copy in the ordinary case;
corrupt marker/checksum/partial newer records fall back to the older valid copy.
Blank/no-valid scenarios return 2; this direct test does not execute the caller's
default-restoration path. For equal counters at indices 1/2 with different generated
payloads, index 2 wins. With the earlier copy at `0xFFFFFFFF` and the later copy at
`0x00000000`, the selector chooses the earlier copy: these comparisons do not implement
wrap-aware recency in the tested pair. This is a synthetic boundary case, not an
observed lifetime failure or complete boot-level rollover analysis.

One further scenario supplies valid old records for all five slots, valid newer ones
for slots 0/1, and partial newer ones for slots 4/5/6. Independent selector calls
choose counters 18 for the former pair and 17 for the latter group. This demonstrates
that this selector can return mixed local record counters; it does not prove that
those counters represent a shared save epoch or that the entire application accepts
that combination after reset. No global consistency check is executed here.

All 46 cases pass. [Metadata](data/storage-recovery.json) fingerprints the image,
script, inherited preparation script, address shim and decoder. Remaining offline
questions include full startup admission and consistency beyond this selector. The
initializer and the three-copy caller are checked below. Interrupted-write behavior
on actual flash and persistence still require bench evidence.


### Initializer flags and three-copy restoration

`tools/check_storage_initialization.py` executes 20 exact-image cases: slots 0/1/4/5/6
with a blank region, a valid copy, a bad newer copy plus valid older one, or no valid
full copy. The actual `0xC447FE` initializer, geometry checks, selector, reader and
checksum execute. The arithmetic helper `0xC5EA16` runs its short branch for the
actual arguments 60000/15000; the checked quotient is 4. This does not cover its other
arithmetic branches. Far-return callback dispatch is replaced explicitly; peripheral
registers are held-ready RAM. No full startup, reset or erase/program effect is modeled.

A usable record returns initializer result 0 and preserves the selected contents.
No usable record takes `0xC44CA4`: it writes the descriptor marker, clears the counter
and bytes +12 through the end, but retains the checksum field at +8. It resets the
selected index, records slot result 2, calls the descriptor callback and returns 10.
The manager is nevertheless marked ready. The five callbacks set the reviewed bits
at `0xFD50` bit 11, `0xFD12` bit 6, `0xFD40` bit 11, `0xFD00` bit 5 and `0xFD08`
bit 10 respectively. They are flag setters, not routines that load application defaults
or save the initialized RAM back to flash. [Metadata](data/storage-initialization.json)
fingerprints both inherited scripts as well as the image, script, address shim and decoder.

`tools/check_storage_consensus.py` adds 48 cases around `0xC4577C(2)`, the counterpart
of the previously checked save call. Forty matrix cases combine all validity masks of
slots 4/5/6 with five equality patterns: all equal, each possible equal pair, or all
different. These replace per-slot initialization and supply its invalid flags. Five
connected cases execute the actual initializers and reads over generated flash records.
The actual caller compares bytes +12 through +511, excluding the headers. It prefers
an equal valid pair; if none exists, it uses the first valid copy in slot order 4/5/6.
It then scatters 315 bytes into descriptor-selected application RAM. Selection branches
and the resulting RAM bytes are checked independently. This is a three-copy restoration
policy, not a comparison of their local record counters or a global save transaction.

One additional connected case keeps those 315 application bytes equal in all copies,
but changes the remaining 185 bytes so that only slots 5/6 match over the full compared
range. The caller chooses slot 5. Thus the comparison includes the tail outside the
315-byte scatter; its field/reserved-byte meaning remains unknown. Equal local counters
are not required for these payload comparisons. No automatic flash repair is executed.

With all three copies invalid, the actual caller runs `0xC08566` rather than scattering
the zeroed record. That handler sets `0xFD72` bit 2 and loads selected ROM parameters,
zero fields and byte sentinels into application RAM. Two direct cases cover configuration
byte `0xC80B96` values 0/1, the latter injected into emulator memory: one group of eight
byte fields becomes `0xFF` or `0x00` respectively. Other selected ROM loads, constant
writes and a bit-mask update are checked. This is execution of the local default handler,
not proof that every initialized field is a safe factory configuration for every ECU.

The script also verifies adjacent decoded calls near `0xC0326C`: mode 2 goes to
`0xC4577C`, followed by mode 2 to `0xC45720`. It does not execute admission to that
branch or the entire startup. [Metadata](data/storage-consensus.json) records the image,
script, inherited recovery/preparation scripts, address shim and decoder. Unknown calls,
uninitialized reads, instruction-budget exhaustion and Ghidra script errors fail the checks.

Local recovery therefore contains an actual cross-copy comparison for slots 4/5/6;
the independent-selector mixed-counter example is not a complete application restore.
The other groups and a bounded startup fragment are checked below; full startup
admission remains outside the model. This work establishes neither atomic saving
across all groups nor interrupted-write behavior on real flash. No CAN write capability
or hardware operation is enabled.


### Other groups and the startup restore fragment

`tools/check_storage_other_groups.py` adds 77 exact-image scenarios with generated
records, actual initialization/read/checksum/scatter instructions and held-ready RAM
registers. Indirect callback dispatch is explicit; no serial transaction, reset,
erase/program effect, interrupt delivery or complete application startup is modeled.

| Slots | Restore entry | Checked policy | Scattered bytes |
|---|---|---|---|
| 0/1 | `0xC45720(2)` -> `0xC455FA(2)` | Separate descriptors and records | 1955/1886 |
| 2/3 | `0xC459FA(2)` | Prefer 2, fall back to valid 3; separate combined flag | 88 |
| 4/5/6 | `0xC4577C(2)` | Matching valid pair, otherwise first valid copy; checked above | 315 |
| 7 | `0xC45C7E(2)` | One field, application default when unusable | 1 |

Eight descriptor cases for slots 0/1 cover blank, valid, bad newer plus valid older,
and no valid full record. The actual functions scatter the selected data, or zeroed
payload prepared by the initializer when no record is usable. They return 0 in these
read-mode cases. Four further cases run their outer wrapper for every validity mask:
`0xC45720(2)` expects each read helper to return 1 and therefore returns 6 even when
the generated valid data is restored correctly. This is a local return-code mismatch,
not evidence that recovery failed or that the whole startup rejects the data.

Sixteen pair cases cover all validity masks and both values of a generated flag in
each record. Slot 2 supplies the 88-byte scatter whenever usable, regardless of slot 3's
counter/data; usable slot 3 replaces it only when slot 2 is unusable. If neither is
usable, initialized zero payload from slot 2 is scattered. Separately, `0xFDE2` bit 0
is set when either usable copy has byte 1 at payload offset 86, obtained from the
reviewed ROM offset field. Thus this flag can reflect slot 3 while the scattered
payload comes from slot 2. The tests do not assign an ECU feature name to that flag.

Six slot-7 cases cover payload values 0/1/`0xA5`, blank and bad-checksum records,
and fallback to an older valid copy. Its descriptor transfers one byte to `0xE016B4`.
When unusable, the actual `0xC086EE` handler supplies `0xFF` instead. The pair and
single-field callers return 0 in these read-mode scenarios; local return alone is
not a record-validity verdict.

Three connected cases enter `0xC03210` with `0xFD1E` bit 4 clear, using all blank,
all valid or mixed-validity records. They stop before the watchdog call at `0xC0327E`.
The actual group-call order is `0xC459FA`, `0xC4577C`, `0xC45720`, `0xC45C7E`,
all with mode 2; initializer slot order is 2/3/4/5/6/0/1/7. Each case passes the
wrapper's return 6 and proceeds to the next caller: no check of that result occurs
in this fragment. The final single-field result is checked and reset configuration
remains unchanged. This executes the local restore sequence, not admission from CPU reset.

Eight gate-prefix cases stop before selected storage callees. Bit 4 clear selects the
internal-flash group path; set selects `0xC13550(2)`. The subsequent branch checks
that flag again, not injected return values 0/3/`0xFFFF`: clear falls back to the group
path, set reaches `0xC1055E(2,0x55D4)` after a skipped watchdog call. The first alternative
helper is not executed; these tests do not establish whether it changes the flag.

Another 32 cases join the earlier producer at `0xC031FC` through the actual intermediate
`0xC0AE5A` to that gate. They vary the injected preceding probe result 0/1/255/256,
configuration 0/1, initial selector bit and two unrelated identity-flag patterns.
Only nonzero low result byte plus configuration 0 selects the alternative path.
The reviewed stock configuration at `0xC80A52` is 1, so these producer-to-gate cases
select the internal-flash groups for that stock byte. Configuration 0 is an emulator
counterfactual. The probe, earlier initialization, complete serial path and later
mutations remain unexecuted; this does not establish every real startup's selected backend.

All 77 cases pass. [Metadata](data/storage-other-groups.json) fingerprints the image,
script, inherited recovery/preparation scripts, address shim and decoder. These results
close the local restore paths for all eight slot records in this exact image. Remaining
questions include complete alternative-backend fallback behavior, full startup context,
later flag writers, field semantics and physical interrupted-write recovery. The first
alternative caller is checked separately below. No general
cross-group atomicity, working CAN write path or physical persistence is established.


### Alternative-backend caller and sentinel defaults

`tools/check_alternative_startup.py` adds 50 exact-image cases. Forty-eight enter the
alternative startup branch at `0xC03218` and execute `0xC13550(2)`, replacing its two
nested restore calls `0xC12F10(2,0x5864)` and `0xC132CC(2)` with supplied results.
Each result varies over 0/1/3/`0xFFFF`. The RAM table at `0xE03000` contains 82 words:
all `0xFFFF`, or a zero word at the first or last position. This table is supplied
independently of the nested results. Watchdog calls are skipped; nested serial reads
and their other side effects are not executed in these cases.

The caller returns 1 only when both injected restores return 1, otherwise 6. Independently
of those results, all 82 sentinel words cause `0xC08560` -> `0xC081F0` to run; a nonsentinel
word at either end prevents that call. The actual default routine, including its short
arithmetic-helper path, executes. Selected ROM/default writes and the caller's clears
of `0xFD66` bit 14 and `0xFD5A` bit 3 are checked. This is an explicit empty-table policy,
not proof that an external chip is absent or that those supplied bytes were read on a bus.

The selector `0xFD1E` bit 4 remains set. Write tracking finds no store to its containing
word in this executed caller/default prefix; the caller then reaches
`0xC1055E(2,0x55D4)`. In this model, nested return errors or the sentinel default path
alone do not cause fallback to the flash groups. This does not exclude a selector write
inside the replaced nested restores, later startup code or interrupts. The test stops
before the next alternative helper and does not establish complete backend selection.

Two further cases execute the real `0xC132CC(2)` body, its RAM initializer `0xC13EB8`
and checksum helper `0xC135FE`, replacing only serial-read calls with constant generated
chunks. Each requests 544 bytes starting at serial address `0x0540`, in 34 reads of
16 bytes. Supplied streams are all `0x00` or all `0xFF`; gathering into `0x00B00C` and
the resulting 42 descriptor fields, totaling 45 bytes, are checked. These two bodies
return 0 and preserve the backend selector. Both patterns avoid other data-dependent
field-processing calls: arbitrary contents, the other nested restore and complete
alternative-backend recovery remain outside the evidence. The actual serial device,
USIC timing and chip presence are not modeled; no serial port is opened.

All 50 cases pass. [Metadata](data/alternative-startup.json) fingerprints the image,
script, inherited preparation script, address shim and decoder. Next useful work is the
descriptor restore, checked below, and further data-dependent field processing, including
any selector writes there. Keep caller-injected errors separate from a real failed serial transaction;
neither a default load nor local result 6 establishes a switch to another backend.


### Alternative descriptor checksum and connected restores

`tools/check_alternative_descriptor.py` adds 12 exact-image cases. Eight enter the
actual `0xC12F10(2,0x5864)` descriptor restore. Serial-read calls are replaced with
generated bytes; arithmetic helpers execute from firmware, using the existing address
and CALLS/RETS shims. The function first requests a two-byte checksum at serial address
`0x0530`, then 528 data bytes from `0x0320` in 33 reads of 16 bytes. These are
call-boundary arguments, not bus captures or a chip-capacity measurement.

For constant `0x00`/`0xFF` payloads, each case supplies the matching 16-bit byte sum,
a one-bit-altered checksum, checksum `0xFFFF`, or changes the last payload byte while
retaining the original checksum. Matching data returns 1; mismatches return 6 and set
`0xFD5A` bit 3. The gathered 528 bytes and 58 descriptor fields totaling 514 bytes
are checked independently. The fields are copied into working RAM even in these
checksum-failure cases: the local error result is not a barrier to that copy. The
last-byte mutation leaves the scattered fields unchanged but changes the checksum
verdict, confirming that the 14-byte tail participates in validation. This is not
proof of uninitialized ECU memory, a field meaning, or downstream acceptance of bad data.

Four additional cases execute the startup caller `0xC13550(2)` with both real nested
restores `0xC12F10` and `0xC132CC`, using constant generated serial streams and matching
or altered descriptor checksum. Each performs 68 modeled read calls: two metadata bytes,
528 descriptor bytes and 544 bytes for the second restore. No nested result is injected
in these connected cases. The first restore returns 1/6 according to its checksum;
the second returns 0 for both supplied streams, so the caller returns 6 in all four.
The all-`0xFF` table triggers the actual default handler; the zero table does not.

All four reach the next alternative call `0xC1055E(2,0x55D4)` with selector `0xFD1E`
bit 4 still set and no tracked write to its containing word. Thus checksum errors,
including a real local checksum branch in this execution model, do not alone switch
these patterns to flash storage. Arbitrary data-dependent paths, the next alternative
restore, later startup and interrupts remain unexecuted. The retained selector is
not an absence proof for every path through the firmware.

All 12 cases pass. [Metadata](data/alternative-descriptor.json) fingerprints the exact
image, script, inherited preparation script, address shim and decoder. The model does
not emulate physical serial transfer, device presence, time, reset or full startup.
The next descriptor restore is checked below; other data-dependent field-processing
paths remain open. Keep checksum failure, RAM mutation and backend selection as separate
observations.


### Read-mode redundancy repair

`tools/check_redundant_descriptor.py` adds 11 exact-image cases entering
`0xC1055E(2,0x55D4)` or `0xC1055E(2,0x569C)`. Arithmetic, selection, comparison and
default code execute. Serial reads/writes use an explicit 4096-byte synthetic array;
delay calls are counted/skipped. This is not a serial chip model, physical capacity
claim, timing measurement or complete startup.

For each descriptor, constant all-`0x00` and all-`0xFF` arrays return 0 with no write
calls; the latter invokes the relevant actual default routine. Three additional cases
per descriptor place the sole checksum-valid, zero-filled copy at each possible copy
position, with distinct nonzero data and altered sums in the other copies. The selected
88/315 application bytes are checked. Both paths retain selector `0xFD1E` bit 4, with
no tracked write to its word in these cases, but their repair behavior differs.

For `0x55D4`, entering read mode with one valid copy leads to actual serial write-call
instructions. The script checks the full write-address/length set: three 96-byte data
regions at `0x0000`/`0x0060`/`0x00C0`, each in six 16-byte chunks, and two-byte sums
at `0x0120`/`0x0130`/`0x0140`. There are 21 write calls and 21 skipped delay calls;
all three copies, including the initially valid one, become the selected data in the
synthetic array. The function returns 2. Thus mode 2 is not a guarantee of read-only
behavior for this application storage helper. Do not invoke it as a harmless bench
read probe or infer write protection from its mode name.

One further `0x55D4` case drops every modeled write effect while still executing the
firmware's verification reads. It attempts the same 21 intervals three times, totaling
63 writes/delays, returns 3 and leaves the synthetic array unchanged. This checks a
bounded software failure path, not actual memory endurance, write timing, interrupted-
write recovery or physical error detection.

For `0x569C`, the three single-valid-copy cases restore the 315 application bytes and
return 0 without serial writes/delays. The synthetic array stays unchanged. Its copy
regions start at `0x0150`/`0x0290`/`0x03D0`, with 320 bytes per region and sum locations
`0x0510`/`0x0520`/`0x0530`. These cases do not prove every branch of this larger helper
is read-only or that arbitrary malformed contents behave identically.

All 11 cases pass. [Metadata](data/redundant-descriptor.json) records synthetic scenario
outcomes and fingerprints the image, script, inherited preparation script, address shim
and decoder. No physical serial transfer, reset, flash operation or full startup runs.
Remaining work includes nonconstant field processing and later startup/selector writes.
The automatic repair path is another reason to keep application initialization separate
from a verified non-writing loader read operation.


### Nested preparation calls and serial side effects

`tools/survey_reset_callees.py` expands the static traversal from `0xC0D572` across
nested direct calls and branch/fallthrough edges. On the exact reviewed I444GI04 image,
it visits 5469 addresses and records 42 direct-call targets, 67 return sites and the
single `SRST` at `0xC0D6B8`. These are syntactic graph counts, not independently
executed functions or proof that all conditional paths are feasible.

Four exact-byte annotations resolve the installed decoder's DSP gap: `0xC5EA76`,
`0xC5EA7A`, `0xC5EA7E` and `0xC5EA84` are CoMULu, CoSHL, CoMACu and CoSUBR.
Their four-byte encodings and sequential flow were checked against Infineon's
[C166S V2 user manual](https://media.digikey.com/pdf/Data%20Sheets/Infineon%20PDFs/C166S_V2_UM_V1.7_Jan2001.pdf),
sections 7 and 8. The graph adds only their fallthrough edges; accumulator, flags,
stack-pointer updates and data reads are not emulated. Neither firmware bytes nor the
installed decoder are patched. The report now has no unresolved direct-flow frontier.

The literal watch for the reviewed reset registers and loader-entry addresses reports
only the existing `SRST`; it does not identify a new reset-configuration write or direct
loader entry. This is not an absence proof: indirect/computed writes, interrupts, other
reset sources and runtime effects remain outside the result. A graph with no unresolved
control transfer does not prove the destinations of computed stores.
[The graph report](data/reset-callees.json) exposes the annotations and direct call edges.

The preparation call chain also reaches serial-transfer helpers `0xC10060` and
`0xC101F8`. This syntactic graph does not specialize every helper condition to the
actual preparation arguments; execution of each transaction for `0x90` is not proved.
Static inspection establishes a concrete side-effect boundary:

- Both access page `0x81` and offsets in the `0x0800` block, including writes to
  `0x0880`, polling `0x0844` and reads from `0x085C`.
- The channel base `0x204800` maps to U1C0 in the XC2000 peripheral manual,
  Table 19-29; its transmit buffer, protocol status and receive buffer offsets match
  section 19.2.1.2 ([manual](https://www.keil.com/dd/docs/datashts/infineon/xc2000_per_um.pdf)).
  This identifies a serial-peripheral path; the physical device and wiring are unverified.
- The first helper writes constants `0x06`, then `0x02`, followed by the high and low
  bytes of its address argument and data from RAM. The second writes `0x03`, sends its
  address argument and collects received data into RAM. These instruction sequences are
  consistent with a serial-memory write/read hypothesis, not identification of a chip.
- Status-dependent loops and watchdog service occur on these paths. Neither elapsed
  time nor successful completion on a real peripheral is established by static traversal.

Consequently the `0x90` preparation path is not established as a RAM-only or harmless
restart. It reaches serial transmit/receive operations in addition to state preparation;
using it as an entry experiment would require identifying the destination and effects.
No peripheral transaction, nonvolatile-memory operation or reset was performed here.
The simpler `0x01` branch still does not itself establish software loader entry.

```
python tools/survey_reset_callees.py IMAGE.bin --report reset-callees.json
```

The tool refuses other image hashes and uses a fresh read-only Ghidra import. Decode
failures and unresolved transfers are reported as frontiers; budget exhaustion and script
errors fail the survey. The success marker means the bounded survey finished, not that
the entire reset path is validated. It exports addresses/metadata, not firmware bytes.
The next offline targets are computed write destinations and the serial device protocol;
neither should be inferred from the reset service's name.

`tools/check_serial_preparation.py` executes the two serial helpers and their reviewed
configuration callees using generated data. All 34 cases pass: 32 held-ready scenarios
cover read/write, four addresses and lengths 0, 1, 2 and 16; 2 held-not-ready prefixes
remain in their status polls. GPIO writes to bit 2 of `0xFFB6` bracket the exchanges:
the write helper separates `0x06` from `0x02` plus address/data with a high/low transition;
the read helper brackets `0x03` plus address/dummy transfers. The system manual maps
that register to P10_OUT; the candidate select is P10.2, not a proven board connection.
Both helpers transfer at least one data byte even for zero length, because their loops
check length after the transfer. The read destination and write source are `0xE023EB`.

[The serial report](data/serial-preparation.json) fingerprints the image and scripts.
The model holds protocol status ready, injects receive bytes and treats command ports
as RAM. Watchdog calls are skipped explicitly; no time, shift register, physical chip,
protection or nonvolatile operation is modeled. These tests enter the helpers directly;
they do not prove that a real `0x90` reset preparation executes a particular transaction.
Identifying the destination still needs board evidence or a separately established device
protocol; the command constants alone are insufficient.

The serial check also verifies caller instructions and a constant table under the
application's reviewed DPP mapping. The table supplies a payload bound of 320 bytes,
data addresses `0x0150`, `0x0290`, `0x03D0`, and metadata addresses `0x0510`,
`0x0520`, `0x0530`. The caller loads these addresses, invokes write then read, compares
returned bytes with a saved RAM copy and records local error value 3 on mismatch.
This is static evidence for a redundant serial-state storage hypothesis. It is not a
capacity measurement, identification of the stored fields or proof that all copies are
written by a real reset. Numeric helpers, complete caller execution and external-memory
completion remain outside the serial-helper test. Keep these device-local serial
addresses separate from the MCU flash address space.

### Reset-specific diagnostic parsers

The concrete local dispatch links are now decoded: selector `0x11` reaches a call at
`0xC505BE`, through `0xC5D48A` into parser `0xC57F8E`; the second path calls at
`0xC603EA`, through `0xC6AD5E` into parser `0xC666DC`. These local call links do not
establish external reachability, the physical transport or all session/security gates.

The earlier writer `0xC5028A` must not be treated as the reset-specific producer:
its decoded caller at `0xC55D54` belongs to a parser checking selector `0x83` at
`0xC55CD6`. Several services reuse the argument cell. Matching the destination address
alone is not a data-flow proof from an incoming reset request.

| Parser | Input and checks in the examined path | Locally accepted argument values |
|---|---|---|
| `0xC57F8E` | Buffer at `0xE01212`; additive checksum, declared payload length 2, header bytes `0x10` and `0xF1`, selector `0x11`; copies the next indexed byte into logical `0xDE64` at `0xC58060` | `0x01`, `0x90` |
| `0xC666DC` | Selector at `0xFA54` equals `0x11`; copies `0xFA55` into logical `0xDEE9` at `0xC666F2` | `0x01`, `0x90`, `0x91` |

The second physical source follows from explicit `EXTP 0x3`; the argument/status cells
are logical addresses under the application's DPP state. This local parser does not
check length or checksum; that does not establish whether its upstream caller does.
Both parsers store an argument before validating it. A value left in RAM after rejection
is not evidence of an accepted operation: the status cell must be checked.

In the bounded model, `0x01` selects the reset routine's short path and `0x90` selects
preparation. The known second-path reset callers explicitly skip `0x91`, despite its
parser accepting it. The post-processing path at `0xC6671A` treats that value separately.
Therefore parser success cannot be equated with execution of `SRST`. The post-processing
hooks at `0xC5808C` and `0xC6671A` also handle `0x90` through additional helpers; their
full effects and upstream ordering are outside the parser execution check.

The reproducible Ghidra check now executes both parsers against generated RAM inputs,
with identity DPP pages and explicit EXTP/address-userop shims. For the first parser,
all 256 argument values are tested in each of 2 length-encoding forms: 512 cases, with
4 accepted instances. The second parser covers 256 values, with 3 accepted instances.
Another 11 negative cases cover corrupted checksum, altered header/selector and invalid
length. The invalid-length fixtures have a recomputed checksum to test that rejection
independently. Observed first-parser local status values are `0x77` for checksum failure,
`0x12` for invalid length and `0x11` for the tested header/selector failures. These are
RAM status values, not verified CAN response bytes.

In this parser part, only parser instructions run: no dispatcher, reset, post-processing
hook, peripheral receive pipeline or authorization procedure is executed. Selected
dispatcher-prefix admission checks are covered separately below. The test does not prove that
any synthetic input can be delivered to these routines on the physical ECU. This adds
local input-validation evidence while leaving the software CAN-loader entry unresolved.
Further useful work is tracing buffer reception and auditing the full dispatch gates,
rather than guessing a programming command from a familiar diagnostic selector.

### Local admission before the diagnostic parsers

The second path's dispatcher entry at `0xC602B4` reads the state byte at physical
`0x00C704` through `EXTP 0x3`. Its local flow is more restrictive than invoking the
parser directly on a filled buffer:

| Point | Condition | Next action on the examined path |
|---|---|---|
| Entry | State equals `0xFF` | Jump to `0xC607A4`, skipping the examined parser-dispatch prefix |
| Entry | State differs from `0xFF` | Call `0xC48478` |
| After that call | Returned `r4` | Store the value at `0xE01656` |
| Subsequent state reread | State equals `0x00` | Call `0xC679C4`, then store its returned selector for dispatch |
| Subsequent state reread | State differs from `0x00` | Jump to alternate handling at `0xC60792` |

One transport-completion producer of the state byte is identified below; changes during
those calls and all other producers are not audited here.
Do not label its values as CAN channels, authorization levels or measured receive states.
The tiny getter at `0xC48478` only reads logical `0xC674` into `r4`; it does not independently
measure a frame. The selector getter at `0xC679C4` reads `0xFA54` under explicit page
selection. Neither getter proves how a message arrived, its ownership or consistent length.
The alternate branch is not executed by this check.

The Ghidra test adds 511 synthetic admission cases: all 256 initial state-byte values
until the first call/skip, and 255 post-getter states excluding the initial skip value.
For the latter the test injects a return value of 2 in `r4`, checks its RAM store and
stops before the selector call or alternate handling. It does not call the getter, model
arrival, establish liveness or execute the complete dispatcher. State changes during the
getter are not covered. The same explicit DPP/EXTP shims and error guards apply.

The first path also starts with a bit comparison at `0xC50442` and a conditional jump
at `0xC50446` before calling its header helpers. The meanings/producers of those bits
and all later admission gates have not been established. Its shared buffer `0xE01212`
is also populated by application message-building paths, for example at `0xC5946C`.
A store to that buffer alone therefore does not identify a peripheral receive path.

These local admission tests do not establish reception on a physical bus, a complete
session/security sequence or software CAN-loader entry. The separate receive/copy trace
below identifies application receive IDs; the boot loader's IDs must not be assigned to
these diagnostic routines.

### Application CAN receive route into the second reset parser

For the exact reviewed I444GI04 image, the second diagnostic path can now be traced
back to the MultiCAN driver in software. This is separate from the flash loader's
startup exchange and does not establish software entry into that loader.

The driver at `0xC47218` reads message-object arbitration/data/DLC registers under page
`0x80`. In the examined standard-identifier extraction path, it masks the high arbitration
word and shifts by 2, then calls `0xC477C8` at `0xC47302` with the ID, length and copied
data. This register interpretation uses the same MultiCAN manual cited below. The
software dispatcher searches 16 records of 10 bytes. Application startup sets
`DPP1=0x320`, so its logical table at `0x6C2C` maps to `0xC82C2C`.

Two records select the transport call at `0xC47836`, rather than the other message handler:

| Receive CAN ID | Application receive handle | Transport PDU | Resulting payload buffer |
|---|---|---|---|
| `0x7DF` | `0x15` | `0x29` | `0xFA54` |
| `0x7E0` | `0x16` | `0x2A` | `0xFA54` |

The transport at `0xC481D2` handles framing and calls the copy/processing routine at
`0xC4861E`. That routine obtains a destination through `0xC49A12` and copies payload
bytes into it. The routing/buffer descriptors come from the application's initialization
record at `0xC049A2`: the decoded copy-table interpreter loads 26 bytes from `0xC499F8`
into `0xC702`. This supplies the receive PDU bindings and the destination pointer.
The completion path calls `0xC49B1E`; its matching-PDU branch stores the supplied result
byte into logical `0xC704` at `0xC49B30`. A successful copy path supplies zero. This
connects the transport completion result to the dispatcher gate described above.
The dynamic copy, routing and RAM accesses assume the modeled `DPP3=0x3` state;
reset/ROM behavior and all intervening page changes are not emulated.

The Ghidra check adds 6 connected software cases: both receive IDs with each of the
3 locally accepted second-parser arguments. A generated short frame enters at
`0xC477C8`; real firmware instructions perform table lookup, framing checks, destination
allocation, payload copying and completion notification. The model verifies payload in
`0xFA54`, length 2 and completion result zero, then runs parser `0xC666DC` and checks
its accepted argument/status. Another 4 cases exercise both IDs with an unsupported
frame-type nibble or a declared short-frame payload too large for the supplied frame.
They leave the initialized completion sentinel and diagnostic buffer unchanged.

CALLS/RETS use an explicit host return stack. The fixture models RAM, DPP/address userops,
the transport enable flag and controller state; it does not execute the actual controller
initialization. Only reviewed software callees are allowed, and unknown userops, calls,
budgets or uninitialized reads fail the run. The actual MultiCAN receive loop is inspected
statically, not executed against a peripheral model. No bus frame, CAN ACK, timing,
physical routing, full diagnostic dispatcher, security procedure, post-processing hook or
reset is tested. These conditions are included in [the report](data/application-reset.json).

This establishes a software route from application CAN reception to the second reset
parser under explicit model assumptions. It does not establish that a reset request will
be admitted end to end on hardware. The transmit-buffer route is checked separately below; full session/security
requirements remain unestablished. The flash loader still uses its separately derived
`0x100`/`0x101` exchange; do not substitute these application IDs for it. The first
argument path through `0xE01212` still needs its own physical receive trace. Matching boot
prefixes do not justify copying these application tables to other firmware versions.

### Application diagnostic transmit buffer

For the same exact image, `0xC49AFC` submits the buffer at `0xFB53` with length from
`0xE01704` and PDU `0x2B` to `0xC47C7E`. That function uses the record at `0xC82D00`
to arm a transport context. The periodic function `0xC47E2A` assembles a frame and
passes it through `0xC48178` to `0xC47586`. The selected driver handle is `0x0C`;
its record at `0xC82C18` supplies ID `0x7E8` and message object 92. Driver
`0xC46F84` copies the ID/DLC/data and issues the transmit-request command. The
response route is therefore connected in software, rather than inferred from an ID constant.

`tools/check_application_can_tx.py` executes this path, including the buffer callback,
controller-state getters, critical-section helpers and completion notification. The 9
passing cases cover generated payload lengths 1, 2 and 7, each with an initially available
object or a held status bit 2 or 8 followed by availability. Frames have DLC 8, a
single-frame length byte and `0xAA` padding. Busy cases retain the assembled frame;
changing the source buffer before the next periodic invocation does not change that
frame's copied payload in these cases. [Metadata](data/application-can-tx.json) records
the exact image, script, shared shim and located decoder hashes.

The model seeds controller states, driver enable, message-object validity/list state and
RAM descriptors, including a standard-frame arbitration register. The driver preserves
frame-format bits; the model does not validate their hardware initialization. Register
command writes are plain RAM writes, not hardware side effects.
No CAN arbitration, ACK, transmit completion interrupt or timing is modeled. Submission
and a normal software notification do not establish wire delivery. The test starts at
the transmit-buffer API with generated payload, not at a diagnostic request: admission,
response construction by the service, session/security and software loader entry remain
unverified. Do not treat the generated bytes as a captured reset response or transplant
these application addresses to other images.

## CAN configuration

Function `0xC00370` accesses the MultiCAN register block. Using the XC2000 peripheral
manual's split-register layout gives the following interpretation:

| Register address | Written value | Interpretation |
|---|---|---|
| `0x200300` | `0x0041`, later clear configuration/init bits | Node 1 configuration |
| `0x20030C` | `0x0001` | Node receive input B; board routing still to verify |
| `0x200310` | `0x3443` | Node bit timing |
| `0x20101A` / `0x201018` | `0x8400` / `0x0000` | Object 0 standard identifier `0x100` |
| `0x20103A` / `0x201038` | `0x4404` / `0x0000` | Object 1 standard identifier `0x101` |

Object 0 is polled by the startup receiver; the startup response passes object index 1
to `0xC004F2`. Thus the code indicates receive ID `0x100` and transmit ID `0x101`.
These identifiers are derived from register writes, not measured on the wire.

For `0x3443`, the bit timing fields are `BRP=3`, `SJW=1`, `TSEG1=4`, `TSEG2=3`,
`DIV8=0`. Accounting for the encoded increments gives 10 time quanta per bit, a
prescaler of 4, and a sample point of 60%. Therefore `bitrate = fCAN / 40`.
The CAN divider write at `0xC00380` sets `0x20000C` to `0x43FE`. This selects
normal divider mode with `STEP=1022`: `fCAN = fSYS / 2`, hence `bitrate = fSYS / 80`.
The clock initialization ends with `PLLCON0=0xA704` after clearing bypass,
`PLLCON1=0x8100` (apart from the self-clearing lock-detection trigger), and
`PLLCON3=0x8003`. The divider fields decode to `N=40`, `P=2`, `K2=4`, and external
oscillator input selected. If that PLL path supplies the system clock,
`fSYS = 5 * fOSC` and `bitrate = fOSC / 16`. The documented bench crystal of
8 MHz would then imply 500 kbit/s. The system clock multiplexer state at flash entry
has not been established; this remains a conditional result, not a measured bitrate.
The bounded delay loops do not yet establish a startup window in milliseconds.

### MCU pin routing

Node 1 port selection `0x0001` selects receive input B. The XC2765X datasheet maps
this to `P0.4`, package pin 63. At `0xC003AE`, the loader writes `0x0090` to
`0xE844`, the port control register for `P2.2`. This selects push-pull alternate
output 1, which the datasheet maps to node 1 transmit, package pin 41.
These are MCU-side mappings, not a continuity measurement to the board transceiver
or connector. They differ from the ROM CAN BSL pin pair documented in FINDINGS.md.
A populated CAN transceiver is necessary for this route; the K-line L9637D serves
a different physical interface and is not used by these CAN register paths.

## Startup and session negotiation

The static receive predicates below use byte offsets from object 0's data registers;
`token` is the received byte at offset 1, echoed into response offset 2.
These are decoder observations, not an instruction to send frames to an ECU.

| Phase | Receive predicate | Response bytes constructed by code |
|---|---|---|
| Startup, `0xC002E4` | byte 0 = `0x01`, byte 2 = `0x96`, byte 3 = `0x69` | `FF 00 token 69 96 69 ?? ??` |
| Session, `0xC005BC` | byte 0 = `0x01`, little-endian word at offset 2 = `0x0002` | `FF 00 token FE 01 00 00 00` |

The first exchange sets a CPU flag and enters the command loop. It does not set the
RAM session flag. Initialization at `0xC00964` clears that state. The second exchange
sets bit 5 at `0xA404`; other command values are ignored until that bit is set.
The second response includes the write at `0xC00616`, which was missing from the
original analyzed listing and was recovered by decoding a cleared scratch window.
This is session-state negotiation; it does not establish the absence of other
command-specific authorization checks.

The transmit helper `0xC004F2` copies 8 bytes into object 1. For the startup response,
only the first 6 bytes are assigned on the examined success path; the final 2 bytes
must not be assumed zero or used as a fixed response signature. The startup parser
checks `NEWDAT` and the stated bytes, but does not explicitly validate the
received DLC on this path. That is not evidence that short CAN frames are valid.

The successful startup path does not explicitly clear object 0's `NEWDAT` flag
before entering the command loop. The loop can therefore encounter that same frame
again after clearing the response buffer. A capture is needed to determine whether
an additional response occurs; a host must not infer session readiness solely from
an assumed single acknowledgement. No wire sequence has been validated.

## Operations and limits

The dispatcher at `0xC00642` compares command values `0x07`, `0x02`, `0x10`, `0x18`
and `0x0E`. Examined paths reach sector erase at `0xC00A58`, page programming at
`0xC00A82`, and CRC calculation using polynomial `0xEDB88320`. The program path buffers
128 bytes. No bulk-flash upload command has been identified in this dispatcher.
This does not prove that all application diagnostic paths lack memory read.

Before a bench experiment: establish the exact clock and pin routing, trace the startup
frame format and receive flags, and review the complete command/authorization paths.
Validate decoding against an independent capture or controlled emulation. Hardware
access and any erase/program operation require separate explicit authorization.


### Decoded command paths

These addresses refer to I444GI04. All operations below require the RAM session flag.
Values spanning multiple bytes are little-endian; the frame token is separate from
address and length fields.

| Command | Handler | Static behavior |
|---|---|---|
| `0x07` | `0xC00668` | Clear the session flag; not a CPU reset |
| `0x02` | `0xC00674` | Set address slot selected by byte 2, with selector less than 2; value from bytes 4–7 |
| `0x10` | `0xC006A6` | Erase from slot 0 for a length from bytes 2–5, stepping by `0x1000` |
| `0x18` | `0xC0077A` | Append a byte count from byte 2, taking data from byte 3 onward; program a full buffer |
| `0x0E` | `0xC00876` | CRC from slot 0 for a length from bytes 2–5; return the result in response bytes 4–7 |

Erase requires a nonempty interval within `[0xC02000, 0xC90000)`, with start and end
aligned to `0x1000`. The end is exclusive and may equal `0xC90000`.
Programming checks the initial address against the same bounds and alignment to
`0x80`. A fragment count greater than 5 or a buffer total greater than 128 is rejected.
The actual page helper is called only when 128 bytes have accumulated. The reported
current address advances by 4 per accepted fragment, irrespective of its byte count.
That asymmetry needs a capture; do not assume arbitrary fragment sizes form a valid
host implementation. Address slot 1 is accepted by the setter, but no consumer for it
was identified in these erase/program/CRC handlers.

CRC checks a nonempty interval against the same bounds, without an alignment test in
the examined handler. Invalid parameters reach status `0x32`; the initial programming
precondition path uses `0x33`; an unrecognized command uses `0x30`. These are observed
status bytes, not a complete specification of errors or authorization.

### Flash completion and error handling

For the exact stock I444GI04 image, `0xC009CA` polls `0xFFFF06` until bits 0–2
are all clear. It then snapshots `0xFFFF08` and `0xFFFF0A`. Bits 0 or 1 in the
first snapshot cause a `0xF5` command write; bits 4 or 5 in that snapshot or bit 4
in the second cause a `0xF0` write and return value 1. Otherwise it returns 0.
The snapshots are not reread after these command writes in the same invocation.
The [XC2000 system manual](https://www.keil.com/dd/docs/datashts/infineon/xc2000_sys_um.pdf),
section 3.9, identifies these registers as IMB_FSR_BUSY, IMB_FSR_OP and
IMB_FSR_PROT, those error bits as SQER/OPER/PROER, and the commands as Clear Status
and Reset to Read. Applying the family manual to the exact derivative still needs
silicon/errata validation.

The erase caller at `0xC00754` and page helper at `0xC00AE2` repeat only the status
call when it returns 1. They do not repeat the erase command or page programming
sequence. In the page helper, `0xC00AA4` also waits for busy bit 2 before loading
64 words. These loops contain no attempt counter or timeout exit; an instruction
budget in the test is not a measured hardware deadline, and reset/interrupt behavior
outside these helpers is not modeled.

`tools/check_flash_completion.py IMAGE --report REPORT` executes the real status
and page-helper instructions with synthetic status registers and the existing C166
address/call shims. The 38 passing scenarios cover 32 combinations of task/error
bits, 3 held-busy prefixes and 3 page scenarios: initially clear, one error followed
by clear, and persistent error. The transient-error case returns after two status
calls with only one page-load sequence and one program-command sequence. The
persistent-error case keeps polling within the bounded prefix. Thus a cleared error
can lead to a normal helper return without retry or readback; this is a modeled
control-flow result, not proof of successful or failed programming on silicon.
Metadata and source hashes: [flash-completion.json](data/flash-completion.json).
The separate 41-case dispatcher harness still bypasses flash helpers; its scope
has not changed. This new test uses plain emulator RAM for flash command ports,
models no flash physics and emits no CAN acknowledgement.

A future writer must verify content independently after an acknowledged operation.
Missing acknowledgement leaves completion uncertain; blindly repeating a page fragment
can append it twice, and a session restart does not clear a partial page. No recovery
procedure after power interruption or a damaged application is established by these
tests. Preserve a complete backup and validate entry/recovery on a bench before
implementing write support.

The loader's admitted interval contains 581632 bytes. A full reference image contains
851968 bytes: boot-sector bytes and the area from `0xC90000` onward are outside the
examined CRC/program interface. That excluded tail is not uniformly blank in I444GI04.
A CAN writer cannot treat a full image as an unrestricted payload.

### Hypothesis: reading a bounded region through short CRC queries

A missing upload command does not by itself make the admitted region unreadable.
The decoded CRC is the reflected CRC32 with all-ones initialization, polynomial
`0xEDB88320`, and a final complement. The handler appears to accept a length of 4.
For a fixed four-byte message, CRC32 is an affine map from 32 bits to 32 bits; its
binary matrix has rank 32, so the four bytes are uniquely recoverable from the CRC.
No exhaustive search over all four-byte messages is needed.

`tools/check_can_crc_hypothesis.py` reconstructs the table/byte update from the listing,
compares it with the standard library CRC, and inverts that independent CRC map using
binary matrix elimination. On macOS the known-answer check, 290 four-byte cases and
all 256 one-byte CRCs passed. For each of I444GI04, I414DB02 and I465AI01, all 145408
four-byte ranges in the admitted interval were recovered exactly from locally computed
CRC values. No recovered bytes are exported by the tool.

This proves the mathematical reconstruction for the modeled algorithm, not a working
ECU reader. Remaining assumptions: the real service accepts short repeated requests,
returns the modeled CRC for the requested flash bytes, and has no additional runtime
restrictions. These mathematical checks do not execute CPU instructions; the separate
bounded p-code test below exercises handlers with peripheral models. CAN traffic remains
untested. The method would cover only the admitted interval; it does not provide a complete backup or read
the protected boot sector. No speed estimate has been measured.

This hypothesis does not justify weakening the current writer's full-backup requirement.
CAN write support needs a separate design for image coverage, recovery, confirmation of
completed writes and compatibility; the offline tools do not enable a CAN transport.

## Comparison across available images

The offline survey on macOS checked SHA256 and size against an external inventory for
120 M74/M74 CAN files. This is a count of files, not independent ECUs or hardware tests.
Their first `0x2000` bytes form 3 exact byte groups:

| Variant | Files | Representative | Boot-region SHA256 |
|---|---|---|---|
| early | 5 | I414DB02 | `a7162971cbbdaeebf8d9ae421220b981df64bcbd4bc876b503a7304ec2a0df99` |
| common | 107 | I444GI04 | `7f1fd47c391a68460ebf3cfb78c0679c24e75470fd9787e368eb48032804310e` |
| common-tail | 8 | I465AI01 | `eb269ea46fe7432742f1d321ebe711cbf8a34b7aa1a03f78e6afaca4e2c84c7b` |

The common and common-tail files have identical first `0xDAA` bytes; differences between
these groups are confined to offsets `0xDAE` through `0x1FFF`. This is byte equality,
not evidence that their tails are disposable. The early group consists of I414DA01,
I414DB02, I414DC03, I444CB02 and I444CC03. I414DA01 is a preproduction sample according
to the inventory; do not promote it to a hardware reference.

Separate disassembly of I414DB02 confirms the reset-cause discriminator, startup
predicates, CAN IDs, node timing, command branches and admitted interval. Addresses
are different: reset discriminator `0xC002A6`, dispatcher `0xC00646`, CRC `0xC0087A`.
Early initialization also calls a peripheral helper at `0xC00DA0` absent from the common
path. This has not been runtime-equated; do not apply one address map to every image.
The common-tail comparison relies on byte identity of the reviewed prefix, not another
independent hardware or CPU execution test.

The evidence tables contain filenames, lengths and hashes only:
[data/can-loader-images.csv](data/can-loader-images.csv) and
[data/can-loader-controls.csv](data/can-loader-controls.csv).
The latter covers 47 control files: 2 M74K and 45 M73, already outside current support.
None matches a reviewed boot-region hash. An unknown hash does not prove a loader is
absent and must not be used to identify a CPU architecture.

Reproduce the byte survey with `tools/survey_can_loader.py DIR --csv report.csv`.
It classifies only the reviewed boot region, never whole-image compatibility. A renamed
file, a familiar size or a matching prefix alone cannot establish a reviewed match.
For the broader support boundary, see [ECU_SUPPORT.md](ECU_SUPPORT.md).


## Bounded execution of the firmware handlers

`tools/check_can_loader_emulation.py` imports each supplied raw image into a fresh,
read-only scratch Ghidra project at `0xC00000`. It runs
`tools/ghidra/EmulateCanLoader.java` through the C166 module's p-code. The firmware's
CRC table generator, CRC byte loop, address checks, session flags, fragment assembly
and transmit-buffer copy execute from image instructions; they are not replaced with
the Python CRC formula.

On Darwin arm64, Ghidra 12.1.4, language `C166:LE:16:default`, all 41 scenarios passed
for each of I444GI04, I414DB02 and I465AI01. Full loaded-memory hashes were checked
against the input files. [The metadata report](data/can-loader-emulation.json) records
those hashes, the script hash and the decoder fingerprint located in the installation.
The latter identifies the located compiled module, not an independent audit of the
entire Ghidra environment. The module must support predicted JMPA instructions and
the address userops implemented by the script; an arbitrary C166 pack is not equivalent.

The execution model has explicit boundaries:

- CALLS/RETS use a host return stack; EXTP/EXTS/DPP addressing uses callbacks.
- RAM starts with an explicit pattern; the harness enters handler/loop entry points,
  rather than executing silicon reset, ROM startup or peripheral clock initialization.
- CAN data/DLC and `NEWDAT` are injected into message-object registers. Clearing
  the flag is immediate; no acceptance filtering, arbitration, bus ACK or timing is modeled.
- Flash erase/program helpers record synthetic operations; the flash-status helper is
  replaced with an immediate ready result. Physical writes, error recovery and readback
  are not tested. No adapter is opened and no reconstructed firmware is exported.
- Unknown userops, instruction-budget exhaustion, script errors or missing success markers
  fail the check. Ghidra's process exit code alone is insufficient to establish success.

This is a bounded instruction-execution test on a community decoder, not complete or
cycle-accurate C166SV2/silicon validation. It strengthens the evidence for the examined
handler paths while leaving real peripheral behavior unverified.

### Observed behavior in that model

- Valid short CRC requests, including an unaligned start, match the standard CRC result.
  Empty, overflowing and out-of-range requests are rejected. Retrying a CRC request
  gives the same result and leaves the selected address unchanged.
- A successful startup leaves the received frame pending. Entering the resident loop
  reprocesses it after clearing RAM state and produces an additional all-zero transmit
  buffer in this model. The startup response's unassigned tail retains the initial RAM
  pattern. Neither behavior establishes the exact traffic on a real bus.
- Before session negotiation, ordinary commands produce no response. Session termination
  disables them again. A short negotiation frame can be accepted when modeled stale data
  provides the remaining required byte; the handler does not test the modeled DLC.
- A complete synthetic page assembled from four-byte fragments reaches the modeled flash
  helper exactly once with the expected address and bytes. A partial page does not commit.
  Duplicate fragments append again, even with the same token; there is no deduplication
  in the tested path. Losing a reply therefore cannot justify blind write retries.
- Ending a session and negotiating again preserves the partial-page counter and active
  page flag. Changing the address slot does not replace the saved address of an already
  active page. Re-entering initialization clears the partial state, but that harness
  operation is not a protocol command or proof of an available software reset route.
- Five-byte fragments append five bytes but advance the reported address by four;
  zero-count fragments also advance it. Buffer overflow, invalid address slots, invalid
  initial page alignment and unknown commands reach their observed error paths.
- An erase-range request plans the expected sector addresses against an immediate-ready
  flash model. This does not validate actual erase completion or error handling.

To reproduce with a matching installed decoder:

```
python tools/check_can_loader_emulation.py IMAGE.bin --report evidence.json
```

Use `GHIDRA_HOME` or `--ghidra` to select the local headless executable. The wrapper uses
scratch projects, verifies the final success sentinel and rejects unreviewed boot hashes.
The report contains hashes and explicitly synthetic model vectors; raw Ghidra logs
and firmware remain local. Reply-matching policy and counterexamples are specified
in [CAN_CLIENT.md](CAN_CLIENT.md).

## Message-object concurrency limits

The following instruction addresses refer to I444GI04; the checked early variant uses
an offset of 4. The register meanings come from the XC2000 peripheral manual,
sections 20.2.8 and 20.2.12 ([manual](https://www.keil.com/dd/docs/datashts/infineon/xc2000_per_um.pdf)).
This is a family-level interpretation, not a measured concurrency result on silicon.

The resident loop reads object 0 control at `0x20101C`, tests bit 3 (`NEWDAT`), calls
the dispatcher, and clears that bit only after the handler returns. `RXPND` is bit 0;
the polled bit is not that flag. The dispatcher reads the object data directly over
multiple instructions rather than copying a complete request into a RAM snapshot.
The examined path does not perform the manual's clear/read/recheck sequence with
`NEWDAT` and `RXUPD` to validate a consistent received frame.

A new synthetic p-code interleaving pauses immediately after the first address request's
token is saved, at `0xC0063E`. Replacing the entire receive object with another complete
address request and resuming produces a successful reply with token `0x41`, while the
selected slot becomes `0xC02004` from the replacement carrying token `0x42`. The original
requested address was `0xC02000`. This demonstrates sensitivity to an object update at
that chosen instruction boundary, not that this timing occurred or is reachable on the
real bus. The harness supplies the update; it does not simulate a CAN receive pipeline.
Identical address retries leave the slot unchanged, but replaying an older address
assignment after a newer one restores the older slot; the tested path has no deduplication.

The transmit helper at `0xC004F2` writes the same object 1 data registers for every reply.
It sets `NEWDAT`, copies the data, sets `MSGVAL`, clears `RTSEL`, and sets transmit enable
and request bits. It contains no wait for the previous transmission to finish and no
software reply queue. Returning from this helper proves only that the copy/request path
ran. The manual distinguishes message-object storage from the node transmit buffer;
these writes alone cannot establish which pending payloads reach the bus. The current
harness records each helper return as a reply vector, not as a completed transmission.

Consequently, unique tokens alone do not establish atomic request consumption. A future
transport must avoid overlapping different requests with queued retries and must justify
receive consistency and transmit visibility. The abstract FIFO exploration does not cover
mixed-frame reads. No burst mode, hardware FIFO guarantee or adapter retry policy follows
from these tests; see [CAN_CLIENT.md](CAN_CLIENT.md).

## Excluded regions and clock evidence boundary

[The region survey](data/can-loader-regions.csv) reports lengths, hashes and counts of
bytes other than `00` or `FF`, without exporting their contents. These are file statistics,
not measurements of erased flash or proof of an area's purpose.

| Physical interval, exclusive end | Expected bytes | Observation across the 120 files |
|---|---|---|
| `0xC00000` to `0xC02000` | 8192 | Boot region excluded by the examined CRC/program bounds |
| `0xC02000` to `0xC90000` | 581632 | Admitted handler region |
| `0xC90000` to `0xCC0000` | 196608 | Present in all files; only 3 contain bytes other than `00`/`FF` |
| `0xCC0000` to `0xCD0000` | 65536 | Present in 41 files; 23 contain bytes other than `00`/`FF`; absent from the other 79 files |

The 3 tail exceptions are I445GBA2, I475MA02 and I486GS09, each with 4 such bytes at
file offsets `0xBFFF8` through `0xBFFFB`. Their purpose is not established; they must
not be discarded as padding on that evidence. In I444GI04 the middle excluded interval
is all zero, whereas the final array contains 16580 bytes other than `00`/`FF`.
The earlier bench documentation associates that array with emulated EEPROM; this file
survey does not independently establish its runtime semantics or donor identity.
Missing bytes in a shorter file do not mean erased or nonexistent memory on a device.

Reproduce using `tools/survey_can_loader.py DIR --regions-csv regions.csv`. The address
map is applied only to reviewed boot fingerprints. A CAN read of the admitted region
would therefore still be a partial image; a CAN writer must preserve or separately
account for the excluded regions and the full-backup requirement.

The clock result remains conditional. The traced loader setup programs the PLL and CAN
dividers, but it does not establish the inherited system-clock multiplexer state. The
available flash image contains no mask-ROM reset implementation. The handler execution
test deliberately starts after that setup and cannot settle it. Confirming 500 kbit/s
still needs the exact startup-state evidence for this derivative or a later measurement;
a model initialized with that clock would merely assume the answer.

An experimental execution of clock initialization did not validate that path: the model
lacked required clock/status-register semantics and reached a polling instruction budget.
This is a harness limitation, not an observed ECU hang. That experimental extension is
not part of the checked handler script and provides no additional bitrate evidence.

## Register references

The register interpretation uses Infineon's XC2000 family manuals, V1.0, 2007-06,
not a claim that every register detail is identical across derivatives:

- [System units](https://www.keil.com/dd/docs/datashts/infineon/xc2000_sys_um.pdf),
  sections 6.2.10.1 and 6.2.10.2.
- [XC2765X datasheet](https://www.keil.com/dd/docs/datashts/infineon/xc2765x_ds.pdf),
  Table 6 and the PLL timing section.
- [Peripheral units](https://www.keil.com/dd/docs/datashts/infineon/xc2000_per_um.pdf),
  MultiCAN register tables and sections on node bit timing and message arbitration.

The exact XC2765X derivative and silicon errata remain constraints on any runtime
interpretation. See [FINDINGS.md](FINDINGS.md) for previous hardware observations.
