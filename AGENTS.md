# Working on openm74

This tool writes to the flash of an engine controller someone else has to drive home in.
Everything below follows from that.

## The standard this repository holds itself to

**Measure, then claim.** Every statement in the documentation about how this hardware
behaves should be traceable to something that was run — a bench session, a probe, a test.
Where a claim cannot be backed that way, say so in the same sentence. `docs/FINDINGS.md`
§6 is the record of what happened when this rule was not followed: three confident,
well-written, wrong explanations, each of which survived multiple reviews because prose
review cannot catch a wrong fact.

**Documents carry the current state, not the road to it.** When something turns out to be
wrong, correct it and *remove* the wrong version rather than layering a retraction on top.
A reader arriving at `QUIRKS.md` needs to know how this hardware behaves; a trail of
superseded beliefs is noise they have to filter, and the risk is that they act on a
paragraph that was left in for honesty.

The one thing worth keeping is a **prescription**: where a mistake is easy to repeat, say
what not to do and why, in the present tense — "do not assign a timeout per read; on
Windows that reprograms the port mid-transfer" rather than "we used to, and here is what
happened". Same information, none of the archaeology.

**Comments carry the why and the evidence, not the what.** `# increment the counter` is
noise. `# MEASURED: an 8-bit checksum let a corrupted page through after ~180 retries,
which is exactly the odds` is why the code looks the way it does and why changing it back
would be a regression. Match the density and voice of the file you are editing.

**No silent caps, no silent skips.** If the tool bounds something — retries, a sector it
cannot write, a region it decided not to touch — it says so, in the log and in the event
stream. A flasher that quietly does less than asked while printing a green line is the
specific failure this project exists to replace.

**Every byte uploaded into an ECU has source in this repository.** Eight blobs: three
stages and five bring-up diagnostics, all under `stages/`, all checked byte-for-byte by
`tools/build_stages.py`. If you add another, it comes with its `.asm` or it does not go
in. This was not true until publication review, and closing it took deleting three blobs
and reconstructing five.

## Layout

| Path | What lives there |
|---|---|
| `src/openm74/klinebsl.py` | the engine and the CLI: transport, the monitor's command protocol, flash operations, the event stream. One module, ~3500 lines, deliberately not split — every hardware-proven behaviour is in it and the seams (transport / monitor / flash / CLI) are worth naming before cutting, not after |
| `src/openm74/session.py` | per-operation settings, output routing and cancellation |
| `src/openm74/checksum.py` | ROM checksum discovery and calculation, stored-field comparison and P0601 monitor-mask detection |
| `src/openm74/gui.py` | the Tk interface. Drives the engine through the same session entry point the CLI does, so a button cannot drift from a terminal |
| `src/openm74/i18n.py` | the string catalogue, ru and en. The interface is translated; the engine's log is not, on purpose — see the file's own header |
| `stages/*.asm` | everything that runs on the ECU |
| `tools/` | build, checks and bench probes. Not shipped to users |
| `tests/` | run without an ECU, a dump or a serial port |
| `docs/` | Bilingual: every page has a `*.ru.md` beside it. **The English is the source of truth** — when the two disagree, the English is right and the Russian is a bug. That arrangement is what makes the second copy survivable, and `tools/check_docs_parity.py` enforces the machine-checkable part of it |

## Before you commit

```
uv run python tests/test_monitor_offline.py     # blob vs assembler, patch sites, gate, framing
uv run python tests/test_monitor_fakeecu.py     # end-to-end against a simulated ECU
uv run python tests/test_checksum.py            # ROM checksum arithmetic, reserved sector
uv run python tests/test_session.py             # failures, session I/O, signal cleanup
uv build                                       # sdist and wheel
uv run python tools/check_package.py            # isolated wheel install and resources
uv run python tools/check_checksum_corpus.py    # checksum + P0601 mask on 120 factory images; skips cleanly without them
uv run python tools/check_gui_platform.py       # this platform's GUI assumptions
uv run python tools/build_stages.py             # needs a C166 assembler; skips cleanly without
uv run --with ruff ruff check .                 # correctness lint; no automatic formatter
uv run python tools/check_docs_parity.py       # English vs Russian docs: numbers, addresses, links
```

The simulated ECU speaks the real wire protocol, models flash the way the part behaves
(erase reads back `0x00`, programming only turns bits on) and can dirty the line the way
hardware does. It has caught defects hardware runs did not.

`check_gui_platform.py` exists because the GUI's cross-platform failures are silent ones —
a modifier bit, a font metric, a menu that grows a tear-off entry on X11 only. Run it on
the platform you are on; it expects *different* answers per platform and says which.

## Hardware, if you have the bench

A real ECU may be connected. **Do not open a serial port without being asked to.** Every
operation needs a manual power cycle first (the ROM loader arms only on a true power-on
reset), so the human has to be present anyway — ask, and wait.

Read paths are safe. `--monitor`, `--mon-read`, `--linktest` and everything in
`tools/probe_*.py` compile a segment mask of zero into the uploaded stage, which means the
ECU itself refuses erase and program. Writing needs `--yes`. `--write`/`--flash` read the
whole flash out and verify it against the ECU before erasing anything; `--write-selftest`
touches one page or sector and saves just that, to `m74_selftest_*.bin`, for the same reason.

## Platform notes

**Windows** is where most users are, and where the screenshots in the README should come
from. `uv run python tools/build_exe.py` produces `dist/openm74.exe`; it is not signed, so
SmartScreen will call it an unknown publisher.

**Linux** needs two system packages that cannot be Python dependencies: `python3-tk` and
`libpython3-dev`. Do not "fix" this by making uv fetch its own interpreter — that was
tried and measured, and the resulting Tk 9.0 cannot see the system fonts, so the window
falls back to bitmap faces and the built binary does not start. The reasoning is recorded
at the bottom of `pyproject.toml`.

**macOS** builds an ad-hoc signed `.app`; the first launch needs right-click → Open.

## Screenshots

Done, from Windows, in both languages: `screenshot-{main,wiring,reading,writing}-{ru,en}.png`
in `docs/img/`, used in the matching half of the README.

```
uv run python tools/screenshots.py                      # main window + the Wiring help tab
uv run python tools/screenshots.py --grab out.png       # whatever window is on screen now
uv run python tools/screenshots.py --redact f.png x y w h
```

Retake them when the window changes — a screenshot is documentation that rots without
anything failing to say so. The tool drives the real window through the real code path, so
this is one command rather than a small research project.

**The two that show work in progress cannot be staged**: they need an ECU, a power cycle
and a person. They were taken during a genuine write session — the pre-flight backup for
the reading one, sector 12 of 206 for the writing one — by a throwaway driver that
automated only the dialogs and the shutter. Nothing about a mid-run state is faked; if
those pictures need retaking, it costs a bench session.

**Redact before publishing.** The window shows the image file that was chosen, and on a
real bench that path runs through somebody's home directory, account name included. All
four pairs were taken with the image staged at a neutral path (`C:\openm74\...`) instead of
blocking it out afterwards, which is the better fix whenever you have the choice.

The photographs here have their serial numbers covered, and **a redaction has to be
opaque**. Measured on the files in `docs/img/`: the black bars stand at a per-channel σ of
0.2–0.8, which is JPEG noise on a solid fill; a red bar on the board photo stood at σ ≈ 16
with pixel values from 48 to 254, and the label under it was legible. Draw the rectangle
with a fully opaque fill and re-measure the variance inside it before publishing — do not
judge a redaction by looking at it.

**And check the metadata, on every image, not just the ones that look sensitive.** A phone
photo of a circuit board carried GPS to about 5 m. Strip by rewriting pixels only
(`Image.new` + `putdata` + `save`) rather than trusting an `exif=` argument, and verify with
`Image.open(p).getexif()` afterwards. If it has already been committed, the fix is to amend
or rewrite that commit — a clean file committed on top leaves the original blob reachable.

Also worth knowing: `OPENM74_DIR` moves the folder whose name the log prints on its first
line, and language is pinned with `i18n.set_lang()` rather than the environment, because
`main()` — which the screenshot tool deliberately does not call — is what normally decides
it.

## Things that look like improvements and are not

* Translating `docs/` *without* a parity check. The rule used to be "English only", and the
  reason was sound: two copies of measured claims drift, and the drifted one lies. The
  audience for this ECU reads Russian and needs the wiring in a language it reads, which is
  a better reason, so the rule changed — but only together with `tools/check_docs_parity.py`,
  which compares every number, hex address, backticked identifier and link target between
  the halves. Adding a page in one language only now fails that check.
* Removing the power-cycle prompt — measured, there is no software route to a fresh
  loader on this part. `docs/PROTOCOL.md` §1 has the measurement.
* Making `--oneshot` faster with a mid-session baud change — the streaming stage has no
  command loop to receive one.
* Trusting the known address of the reserved sector instead of measuring it. The name is
  a label on a measurement; a tool that skips a sector because a constant says so will
  skip a real failure just as happily.


## Handoff context and working rules

This file is the repository's persistent instruction and handoff entry point. Keep it
current when project decisions change. Talk to the user in Russian; retain the existing
language of source files and documentation. Preserve hand-laid-out comments: there is no
automatic formatter; Ruff configuration in `pyproject.toml` is intentional.

The user wants to develop this project into professional software for auto repair shops.
That is a product direction, not an approved implementation plan. The current implementation
is a K-line ROM-BSL tool; soldering a missing L9637D onto a board is a DIY bench workaround,
not a factory or service-shop workflow. Firmware-level CAN reprogramming and an external
adapter for boards without that transceiver remain questions, not verified capabilities.
Distinguish the ROM loader from any application-level programming service; do not promise
read/write support from a plausible protocol explanation alone.

Re-derive numerical claims from other agents or previous sessions before repeating them.
A matching symptom is not proof of a cause. State the host, sample count and measurement
method when making hardware claims; evidence from one ECU does not establish factory-wide
behaviour. Existing current-state documentation takes precedence over superseded session
notes. Do not restore private source links, sibling-project names or vendor-forum references
previously removed from this public repository, including in this file or commit messages.
Do not commit raw session logs or private memory files.

### Transport and the shared bench

The bench moves between this Mac and a separate physical Windows PC, using the same adapter
and ECU. When reviewing a copied checkout, inspect its diff; Windows can change executable
bits without changing file content. Record which host produced a measurement.

`SteadyPort` exists because assigning the underlying pyserial port's `timeout` between a
write and its reply reconfigures the adapter on Windows. Never restore per-read/per-retry
assignments on the underlying port. Wrapper timeout assignments are virtual and intentional.
Before attributing corruption to K-line physics, compare the same bench on a second OS.

On this Mac's bench, the FTDI adapter has sometimes accepted open/read of port attributes
but rejected setting those same attributes with `EINVAL` after prolonged use. A device node
or a successful `stty` read does not prove it is healthy. The user's recovery is to remove
both USB power and bench-supply power, then reconnect; USB removal alone can leave it
part-powered. Any port-setting diagnostic still requires the explicit serial-port
authorization above. Check the adapter before spending a requested ECU power cycle.

The bench reference image is `I444GI04.bin`, 851968 bytes, SHA256
`36c7797441c5e64d23becdf54c323c8ee1e05daed5dfd2438fe231f11a93c91c`.
Its former Downloads copy was reported removed; do not assume it is present. Verify any
replacement against the hash. A dump from the repeatedly reflashed bench ECU is current
state, not a factory baseline. On the measured unit, the 4096-byte sector at `0xC0F000`
is reserved; a full reference write matched the other 847872 bytes. Continue detecting
unmapped regions from the device's responses rather than skipping this address by constant.
Firmware images, backups and partial dumps remain excluded from version control.

### ROM checksum constraints

The flasher reports checksums; it does not repair or rewrite the supplied image. A partial
write must check the backup with the payload overlaid; `--flash` must account for the ECU's
preserved boot sector. Short images must produce a "not checked" result, not an exception
that hides the short-read report. Keep checksum verdicts in the existing stage/problem
event stream and translate their GUI rendering through `i18n.py`.

A stale checksum does not by itself prove P0601 will be reported: distinguish monitor
enabled, masked off and unknown. The delayed fault was observed once on one car; the
masked-off interpretation comes from disassembly. Keep those limits next to the claims.

Run `tools/check_checksum_corpus.py DIR` or set `OPENM74_CORPUS` when an external corpus
is available. Without one, report the check as skipped. The documented sample is 120 images,
119 discovered routines and 105 matches against 109 published checksum entries. Treat
reserved-sector bytes as the chip's measured `9b 1e`, not the file's zeros; that distinction
can make another implementation's totals differ without either calculation being wrong.
The corpus and the algorithm's original research are external; obtain their location from
local context or the user without adding private provenance to this repository.

### Known follow-up work

* CAN/firmware research is paused at the offline milestone. See the checkpoint below
  and `docs/RESEARCH.md` before interpreting any older investigation follow-up.
* `docs/ECU_SUPPORT.md` records actual image/hardware coverage. Mikas and Marelli remain
  unverified candidates; matching silicon or loader bytes do not establish support.

* README screenshots show released v1.0.1, predating the workspace layout. Retake on Windows;
  the in-progress reading/writing shots require an authorized real bench session.
* The `SWRSTCON` experiment remains unperformed and requires a monitor register-write
  command. Do not describe software re-entry to BSL as working.
* The XC2768X layout discriminator assumes the same unmapped word at `0xCC0000` and
  `0xCD0000`; that has not been measured on such a part. See `docs/FINDINGS.md`.
* The checkout declares version `1.0.1`. Check tags and source differences when discussing
  release contents; changes on the branch do not automatically appear in release binaries.
  Release checksums must use LF on every platform so `sha256sum -c` works across hosts.

Do not push, publish, rewrite history or run ECU operations merely to validate an ordinary
source/documentation change; those actions need authorization covering that operation.

## Private continuity context

Read `~/.codex/project-context/openm74/INDEX.md` when earlier decisions or investigation details are
needed. This local context is private and must not be copied into this repository.
Current source and explicit later user decisions take precedence over historical
notes. Old task requests and approvals are not pending instructions. Keep this
file current with durable decisions. Communicate with the user in Russian.
Do not append automatic AI coauthor or generator signatures to commit messages.


## Session API

`klinebsl.main(argv=None, *, log=None, events=None, session=None)` accepts arguments without
the executable name and optional text streams. Create a fresh `Session` for each operation;
it owns runtime settings, output and a thread-safe stop event. The GUI constructs it before
starting its non-daemon worker and calls `session.request_stop()` when closing. Never clear
that request at worker entry: it may have arrived before the thread started. Reusing a
session is rejected. Context-local access serves low-level engine helpers; never replace
process-wide `sys.argv`, `sys.stdout` or `sys.stderr` from a worker.

One active bench operation per process remains an explicit ownership guard. GUI close is
asynchronous: keep draining events until the engine closes its transport and the worker
returns. Do not impose a deadline that kills a write between erase and program. A read is
allowed to finish before closing. macOS builds use onedir bundles; Windows and Linux remain
onefile executables.


## Workspace interface

The main window has a connection sidebar, Read/Write selection, operation-specific inputs
and one action button. Selecting an operation must never start I/O. The journal is a separate
window with a persistent Text buffer: hiding it must preserve the log and not affect a run.
Disable the form during an operation. Language/theme rebuilds preserve selected operation,
path, port, mode and resume choice; the dangerous ECU override still resets. Form controls
use the built-in clam renderer to follow the selected palette on all platforms; title bars
and native menus remain platform-owned. Keep the device identity label tied to the engine's identity event rather than adapter enumeration.


## Application identity

The approved icon is Chip 74: a pale memory chip with geometric dark 74 digits and
blue pins on a rounded graphite background. Keep it reproducible in
`tools/make_icon.py`; regenerate the committed PNG, ICO and ICNS together after edits.
The mark uses paths rather than platform fonts. Inspect small taskbar sizes as well
as the large master when changing it.

## Offline CAN research: paused milestone

As of 2026-10-27, the user has paused further CAN/firmware reverse engineering at the
offline milestone. Focus on maintaining the working K-line product. Do not resume an
open-ended disassembly campaign from older follow-up notes. Resume when the user asks;
ECU/CAN/serial operations still require their own explicit authorization.

Start at [docs/RESEARCH.md](docs/RESEARCH.md) for the evidence index, reproduction commands,
remaining questions and restart gates. Detailed findings remain in docs/CAN_LOADER.md;
CAN_CLIENT.md defines only an offline contract, CAN_BENCH_PLAN.md an unperformed plan,
and ECU_SUPPORT.md the actual sample/hardware coverage. None enables production CAN I/O.

The stock I444GI04 studied offline has SHA256
166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b; it is NOT the historical
bench reference above. A matching boot fingerprint does not validate application code.
Do not publish firmware, raw listings, local source paths, identifiers or private notes.

Run tools/check_research_evidence.py before committing research changes: it checks archived
script/ancestor fingerprints and corpus references without firmware/Ghidra. A pass does not
rerun p-code or validate hardware. Never refresh a report hash to conceal changed code;
rerun the corresponding wrapper on its reviewed image and retain the model boundaries.

Durable limits to preserve in code and docs:

* K-line ROM BSL and flash-resident CAN are distinct. Software CAN-loader entry, board
  routing, clock selection, real CAN exchange and interrupted-write recovery remain unproven.
* The proposed CRC client never reassigns tokens: at most 127 four-byte ranges (508 bytes)
  per model lifetime. Reconnect/new instance/sleep does not establish reply freshness.
  An injected RX-object race can mix one frame's token with another frame's address;
  abstract FIFO ordering alone does not prove a consistent receive snapshot.
* The admitted CAN interval excludes parts of a full image, including storage data in
  the flash tail. CAN CRC reconstruction is not a complete pre-write recovery backup.
* A normal helper return, cleared status or software TX callback is not physical flash
  verification or bus delivery. Models explicitly replace peripheral effects/selected calls.
* C1055E(2,55D4), despite its read mode, can issue serial writes to repair redundant copies
  in the offline model. Do not use application initialization as a non-writing entry probe.
  Application reset/preparation may save state and is not a harmless loader-entry command.
* Mikas/Marelli have no inspected samples or bench confirmation here. Candidate names and
  matching silicon are not support claims. Retain the current ECU identity/write gates.
