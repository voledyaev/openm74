#!/usr/bin/env python3
"""Check reviewed application-reset branches in a scratch Ghidra project, without an ECU."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from check_can_loader_emulation import ghidra_command

SCRIPT = Path(__file__).parent / "ghidra/CheckApplicationReset.java"
IMAGE_SHA256 = "166bbdd475930e5fc2f15035435505fc999e07d8a2d915595246f45fe0bff59b"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--ghidra", type=Path)
    parser.add_argument("--report", type=Path, help="metadata only, no firmware bytes")
    args = parser.parse_args(argv)
    if args.report and args.report.resolve() == args.image.resolve():
        parser.error("report must not overwrite the input image")
    try:
        digest = hashlib.sha256(args.image.read_bytes()).hexdigest()
        if digest != IMAGE_SHA256:
            raise ValueError("unreviewed image; this check covers one exact I444GI04 image")
        command = ghidra_command(args.ghidra)
        with tempfile.TemporaryDirectory(prefix="openm74-app-reset-") as scratch:
            run = subprocess.run([
                str(command), scratch, "scratch", "-import", str(args.image.resolve()),
                "-loader", "BinaryLoader", "-loader-baseAddr", "0xc00000",
                "-processor", "C166:LE:16:default", "-cspec", "tasking", "-readOnly",
                "-noanalysis", "-scriptPath", str(SCRIPT.parent.resolve()),
                "-postScript", SCRIPT.name,
            ], capture_output=True, text=True, timeout=180)
        output = run.stdout + run.stderr
        result = re.findall(r"RESET_RESULT=(\{.*\})", output)
        if (run.returncode or "ERROR" in output or "WARN  Uninitialized memory" in output
                or re.findall(r"IMAGE_SHA256=([0-9a-f]{64})", output) != [digest]
                or re.findall(r"PASS application-reset cases=(\d+);", output) != ["1024"]
                or len(result) != 1):
            lines = output.splitlines()
            errors = [line for line in lines if "ERROR" in line or "Exception" in line]
            detail = "\n".join(errors[:6] + lines[-8:])
            raise ValueError("Ghidra failed or missing evidence markers\n" + detail)
        evidence = json.loads(result[0])
        if (evidence["cases"], evidence["preparation_entry"],
                evidence["direct_reset_tail"]) != (1024, 4, 1020):
            raise ValueError("unexpected branch coverage")
        if len(evidence["static_preparation_helpers"]) != 5:
            raise ValueError("missing static preparation survey")
        parsers = evidence["diagnostic_parsers"]
        if (parsers["first_cases"], parsers["first_accepted"], parsers["second_cases"],
                parsers["second_accepted"], parsers["negative_cases"]) != (512, 4, 256, 3, 11):
            raise ValueError("unexpected diagnostic parser coverage")
        admission = evidence["admission_gates"]
        if (admission["entry_cases"], admission["post_length_cases"]) != (256, 255):
            raise ValueError("unexpected admission-gate coverage")
        transport = evidence["can_transport"]
        if (transport["cases"] != 6 or transport["negative_cases"] != 4
                or transport["rx_ids"] != [0x7DF, 0x7E0]):
            raise ValueError("unexpected software receive-route coverage")
        evidence.update(image_sha256=digest,
                        script_sha256=hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                        scope="One reviewed image; reset-prefix and synthetic-RAM "
                              "diagnostic parser/admission and software CAN receive p-code; "
                              "reset prefix stops before "
                              "reset-tail NOP or preparation. Candidate call/opcode scan is not "
                              "complete control-flow analysis. Bounded helper CFG surveys exclude "
                              "callees and do not execute preparation. "
                              "No device or reset emulation.")
        if args.report:
            args.report.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print("1024 application-reset branch cases passed; SRST was not executed")
        print("768 synthetic parser cases and 11 negative cases passed")
        print("511 synthetic admission-gate cases passed; hardware reception is not emulated")
        print("6 software CAN receive/parser cases and 4 invalid-frame cases passed")
        print("7 decoded direct-call candidates; 5 bounded static helper CFG surveys")
        print("No software loader-entry claim; preparation helper callees are outside the survey")
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"application-reset check failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
