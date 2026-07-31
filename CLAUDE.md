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
| `src/openm74/gui.py` | the Tk interface. Drives the engine through the same code path the CLI does, so a button cannot drift from a terminal |
| `src/openm74/i18n.py` | the string catalogue, ru and en. The interface is translated; the engine's log is not, on purpose — see the file's own header |
| `stages/*.asm` | everything that runs on the ECU |
| `tools/` | build, checks and bench probes. Not shipped to users |
| `tests/` | run without an ECU, a dump or a serial port |
| `docs/` | Bilingual: every page has a `*.ru.md` beside it. **The English is the source of truth** — when the two disagree, the English is right and the Russian is a bug. That arrangement is what makes the second copy survivable, and `tools/check_docs_parity.py` enforces the machine-checkable part of it |

## Before you commit

```
uv run python tests/test_monitor_offline.py     # blob vs assembler, patch sites, gate, framing
uv run python tests/test_monitor_fakeecu.py     # end-to-end against a simulated ECU
uv run python tools/check_gui_platform.py       # this platform's GUI assumptions
uv run python tools/build_stages.py             # needs a C166 assembler; skips cleanly without
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
