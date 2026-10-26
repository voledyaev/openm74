#!/usr/bin/env python3
"""Execute synthetic storage-initialization scenarios for one reviewed image; no ECU I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from check_application_reset import IMAGE_SHA256
from check_can_loader_emulation import ghidra_command

SCRIPT = Path(__file__).parent / "ghidra/CheckStorageInitialization.java"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--ghidra", type=Path)
    parser.add_argument("--report", type=Path, help="metadata/addresses, no firmware export")
    args = parser.parse_args(argv)
    if args.report and args.report.resolve() == args.image.resolve():
        parser.error("report must not overwrite the input image")
    try:
        digest = hashlib.sha256(args.image.read_bytes()).hexdigest()
        if digest != IMAGE_SHA256:
            raise ValueError("unreviewed image; use the exact reviewed I444GI04")
        command = ghidra_command(args.ghidra)
        with tempfile.TemporaryDirectory(prefix="openm74-storage-initialization-") as scratch:
            run = subprocess.run([
                str(command), scratch, "scratch", "-import", str(args.image.resolve()),
                "-loader", "BinaryLoader", "-loader-baseAddr", "0xc00000",
                "-processor", "C166:LE:16:default", "-cspec", "tasking", "-readOnly",
                "-noanalysis", "-scriptPath", str(SCRIPT.parent.resolve()),
                "-postScript", SCRIPT.name,
            ], capture_output=True, text=True, timeout=180)
        output = run.stdout + run.stderr
        results = re.findall(r"INIT_RESULT=(\{.*\})", output)
        if (run.returncode or "ERROR" in output or "uninitialized" in output.lower()
                or len(results) != 1
                or output.count("PASS storage initialization;") != 1
                or re.findall(r"IMAGE_SHA256=([0-9a-f]{64})", output) != [digest]):
            lines = output.splitlines()
            errors = [line for line in lines if "ERROR" in line or "Exception" in line]
            raise ValueError("Ghidra survey failed\n" + "\n".join(errors[:6] + lines[-8:]))
        evidence = json.loads(results[0])
        if evidence != dict(cases=20, slots=[0, 1, 4, 5, 6], scenarios_per_slot=4):
            raise ValueError("unexpected scenario counts")
        modules = list((command.parent.parent / "Ghidra").rglob("c166.sla"))
        decoder_hash = (hashlib.sha256(modules[0].read_bytes()).hexdigest()
                        if len(modules) == 1 else None)
        evidence.update(parent_script_sha256=hashlib.sha256(
                            SCRIPT.with_name("CheckStorageRecovery.java").read_bytes()).hexdigest(),
                        preparation_script_sha256=hashlib.sha256(
                            SCRIPT.with_name("CheckResetPreparation.java").read_bytes()).hexdigest(),
                        located_decoder_sla_sha256=decoder_hash, image_sha256=digest,
                        script_sha256=hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                        shim_sha256=hashlib.sha256(
                            SCRIPT.with_name("EmulateCanLoader.java").read_bytes()).hexdigest(),
                        scope="Real initializer geometry/read/checksum and default callback code "
                              "over generated records with held-ready RAM peripheral registers. "
                              "Indirect callback dispatch shim. No full startup, reset, flash "
                              "writes, timing or ECU I/O.")
        if args.report:
            args.report.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print("PASS 20 storage initialization cases; "
              "generated records and explicit callback dispatch")
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"storage-initialization check failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
