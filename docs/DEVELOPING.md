# Developing openm74

Everything a contributor needs and a user does not: the machine-readable interface, how to
build the application, and what to run before committing. For using the tool, see the
[README](../README.md).

Like the rest of `docs/`, this file is English only — see [CLAUDE.md](../CLAUDE.md) for why.

---

## Driving it from another program

`--progress json` splits the two audiences that were previously sharing one stream:

```
stdout   exactly one JSON object per line, nothing else, ever
stderr   the human log, unchanged and still worth reading
```

```
uv run openm74 --port COM3 --progress json --flash image.bin --yes
{"baud": 9600, "event": "stage", "ok": true, "stage": "handshake"}
{"addr": 12591104, "baud": 115200, "bytes": 843776, "estimate": 422, "event": "stage", "resume": false, "sectors": 206, "stage": "writing"}
{"addr": 12591104, "done": 1, "eta": 28, "event": "progress", "op": "write", "retries": 0, "skipped": false, "total": 206, "unit": "sectors"}
{"addr": 12644352, "detail": "no memory behind this address", "event": "problem", "fsr_op": 3, "fsr_prot": 0, "image_bytes": 4085, "kind": "unmapped", "known": "reserved by the device (XC2000 flash module 0, physical sector 15)"}
{"event": "result", "message": "every sector written was verified", "ok": true, "skipped": 0, "unmapped": 1, "unmapped_image_bytes": 4085, "what": "write", "written": 205}
```

Those lines were **captured from a run**, not written by hand — a previous version of this
section was hand-written, claimed to be the real thing, and got the `problem` event wrong in
four ways at once, which is a poor thing to hand someone writing a parser. Note in
particular:

* keys are emitted **sorted** (`json.dumps(..., sort_keys=True)`), and addresses are
  integers, not hex strings;
* `problem` events carry **`detail`**, not `message` — `message` belongs to `result`. Mixing
  the two is how a vocabulary this small stops being a contract;
* a `problem` may carry extra keys (`known`, `fsr_op`, `fsr_prot`); treat unknown keys as
  additive and do not require them.

Four kinds, and the vocabulary is deliberately tiny so it can stay still: `stage` for a
coarse milestone, `progress` with `done`/`total`/`unit`, `problem` for something survivable
worth surfacing, and `result` for the outcome of a phase that can pass or fail.

**The verdict rule, which matters more than the schema:** an operation succeeded *iff* at
least one `result` arrived and none of them said `ok: false`. **Absence of results is not
success** — that is the case where the run died before finishing anything.

**The process exit status agrees with it**: zero only when the job it was asked to do
succeeded. It used to be zero for every outcome, so `openm74 --flash img.bin --yes && echo
ok` printed `ok` over an ECU that had just been left unwritten — worth knowing if you have
a script written against an older build.

Getting that right took two passes, and the second one is worth naming because the trap is
easy to fall into again. `sys.exit(True)` exits **1**: `bool` is a subclass of `int`, and
`sys.exit` passes an int straight through as the status. So the natural-looking
`return ok` at the end of a branch reports every success as a failure and every failure as
a success, and reads correctly at each individual site. Every branch now goes through one
normaliser (`exit_status`), and a test pins the mapping.

This exists because the GUI used to recover its progress bar with a regex over prose and
decide success by searching the log for a sentence, which meant rewording a `print`
statement silently broke the progress bar, and a success printed by an *earlier* operation
could be reported for a later one that failed. A flasher that can say "done, verified"
about a write that did not finish has no business shipping. Group [18] of the fake-ECU tests
holds the contract in place.

## Building the application

One script, whichever platform you are on — a second builder is how the platform nobody
built this week quietly stops producing a working artifact.

```
uv sync
uv run python tools/build_exe.py
```

| Platform | Produces |
|---|---|
| Windows | `dist/openm74.exe` |
| macOS | `dist/openm74.app` and the `.zip` of it, hashed |
| Linux | `dist/openm74` |

**On Debian and Ubuntu** you also need two system packages that are not Python
dependencies and so are not in `uv sync`: `python3-tk` for the interface, and
`libpython3-dev` — PyInstaller needs Python's *shared* library, which these distributions
package separately from the interpreter, and without it a onefile build stops before it
starts. The build script recognises that particular failure and names the package.

The build script runs the result before calling it built, and the self-test checks the
things that can *only* break inside a bundle: that the assembled stages travelled (without
them the byte-for-byte check against the assembler's output silently becomes a no-op), that
serial-port enumeration still works (on macOS pyserial reaches IOKit through `ctypes`, which
the bundler explicitly declines to trace), and that Tk can open a window at all. A
successful PyInstaller run says nothing about any of that.

None of these are **signed by a paid identity.** Windows SmartScreen will call the .exe an
unknown publisher and single-file packing occasionally trips antivirus heuristics; macOS
will refuse the .app on first launch until you right-click → Open. Signing costs money this
project does not spend. PyInstaller builds are also **not reproducible**, so "verify it by
rebuilding" is not an honest instruction — which is exactly why the dependency list is
Python's standard library plus pyserial, and building your own copy takes a minute.

The macOS bundle is ad-hoc signed (which is what lets it run on Apple Silicon at all) and
declares its minimum macOS version, read out of the built binary rather than guessed —
without it an older Mac does not refuse the app politely, it dies in the dynamic linker and
looks like a corrupt download.

## Tests

```
uv run python tests/test_monitor_offline.py    # blob vs assembler, patch sites, gate, framing
uv run python tests/test_monitor_fakeecu.py    # end-to-end against a simulated ECU
uv run python tools/check_gui_platform.py      # the GUI's platform assumptions, on this OS
uv run python tools/build_stages.py            # the .bin files vs the .asm they came from
```

The simulated ECU speaks the real wire protocol, models flash the way the part behaves
(erase reads back as `0x00`, programming only turns bits on) and can dirty the line the
way real hardware does. It has caught real defects that hardware runs did not.

The platform check exists because the GUI's two failure modes across platforms are silent
ones: modifier-key bits differ (Command arrives as a different bit than Control, so a
Ctrl-only handler makes the log uncopyable on macOS) and the connector diagram is laid out
in pixels against fonts that are whatever each platform calls its UI face. Neither raises
an exception — the drawing just becomes wrong. So it synthesises the keystrokes and measures
every string against the cell it has to fit in.

---

