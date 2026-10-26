#!/usr/bin/env python3
"""Run reviewed CAN-loader instructions in scratch Ghidra projects, without hardware.

    python tools/check_can_loader_emulation.py IMAGE.bin [IMAGE.bin ...] --report out.json

Requires Ghidra and a C166 language module supporting prediction-bit JMPA, EXTP/EXTS
and the address userops described in docs/CAN_LOADER.md. No network or transport is used.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from survey_can_loader import BOOT_SIZE, BOOT_VARIANTS

SCRIPT = Path(__file__).parent / "ghidra" / "EmulateCanLoader.java"
EXPECTED_CASES = 41


def ghidra_command(explicit):
    if explicit:
        command = explicit.expanduser().resolve()
    elif os.environ.get("GHIDRA_HOME"):
        command = Path(os.environ["GHIDRA_HOME"]) / "support" / "analyzeHeadless"
    elif shutil.which("analyzeHeadless"):
        command = Path(shutil.which("analyzeHeadless"))
    else:
        # Homebrew's Ghidra executable is inside libexec, not on the shell's PATH.
        command = Path("/opt/homebrew/opt/ghidra/libexec/support/analyzeHeadless")
    if not command.is_file():
        raise ValueError("set GHIDRA_HOME or pass --ghidra /path/to/support/analyzeHeadless")
    return command.resolve()


def emulate(command, image):
    data = image.read_bytes()
    boot_hash = hashlib.sha256(data[:BOOT_SIZE]).hexdigest()
    if boot_hash not in BOOT_VARIANTS:
        raise ValueError(f"{image.name}: boot region is unreviewed")
    digest = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory(prefix="openm74-can-emu-") as scratch:
        args = [str(command), scratch, "scratch", "-import", str(image.resolve()),
                "-loader", "BinaryLoader", "-loader-baseAddr", "0xc00000",
                "-processor", "C166:LE:16:default", "-cspec", "tasking", "-readOnly",
                "-noanalysis", "-scriptPath", str(SCRIPT.parent.resolve()),
                "-postScript", SCRIPT.name]
        run = subprocess.run(args, capture_output=True, text=True, timeout=180)
    output = run.stdout + run.stderr
    success = re.findall(r"PASS cases=(\d+);", output)
    loaded = re.findall(r"IMAGE_SHA256=([0-9a-f]{64})", output)
    traces = re.findall(r"SYNTHETIC_TRACE=(\{.*\})", output)
    engine = re.findall(r"ENGINE=(\S+) LANGUAGE=(\S+)", output)
    # analyzeHeadless may exit zero even after a script exception. The final sentinel
    # and loaded-memory hash are part of the check, not just the process exit status.
    if (run.returncode or success != [str(EXPECTED_CASES)] or loaded != [digest]
            or len(engine) != 1 or len(traces) != 1 or "ERROR" in output
            or "WARN  Uninitialized memory" in output):
        lines = output.splitlines()
        errors = [line for line in lines if "ERROR" in line or "Exception" in line]
        detail = "\n".join(errors[:6] + lines[-8:])
        raise ValueError(f"{image.name}: Ghidra check failed\n{detail}")
    trace = json.loads(traces[0])
    synthetic = bytes(n * 3 + 7 for n in range(32))
    if (trace.get("kind") != "synthetic-pcode-model"
            or trace.get("payload_sha256") != hashlib.sha256(synthetic).hexdigest()):
        raise ValueError(f"{image.name}: invalid synthetic fixture provenance")
    return dict(file=image.name, size=len(data), sha256=digest, boot_sha256=boot_hash,
                variant=BOOT_VARIANTS[boot_hash], cases=int(success[0]),
                ghidra=engine[0][0], language=engine[0][1], synthetic_trace=trace)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--ghidra", type=Path, help="path to support/analyzeHeadless")
    parser.add_argument("--report", type=Path,
                        help="write hashes and synthetic model vectors as JSON")
    args = parser.parse_args(argv)
    if args.report and args.report.resolve() in {p.resolve() for p in args.images}:
        parser.error("the report must not overwrite an input image")
    if len({p.name for p in args.images}) != len(args.images):
        parser.error("use distinct image filenames in one evidence report")
    try:
        command = ghidra_command(args.ghidra)
        results = []
        for image in args.images:
            print(f"Emulating {image.name} ...", flush=True)
            results.append(emulate(command, image))
            print(f"  {EXPECTED_CASES} cases passed", flush=True)
        # Fingerprint the installed decoder without exporting its filesystem location.
        modules = list((command.parent.parent / "Ghidra").rglob("c166.sla"))
        module_hash = (hashlib.sha256(modules[0].read_bytes()).hexdigest()
                       if len(modules) == 1 else None)
        report = dict(host=f"{platform.system()} {platform.machine()}",
                      script_sha256=hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                      located_decoder_sla_sha256=module_hash,
                      scope="Firmware p-code with address/call shims; "
                            "modeled CAN and flash effects; no silicon, timing, "
                            "bus exchange or readback verification",
                      images=results)
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"emulation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
