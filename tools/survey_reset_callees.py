#!/usr/bin/env python3
"""Survey direct reset/preparation call reachability for one reviewed image; no ECU I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from check_application_reset import IMAGE_SHA256
from check_can_loader_emulation import ghidra_command

SCRIPT = Path(__file__).parent / "ghidra/SurveyResetCallees.java"


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
        with tempfile.TemporaryDirectory(prefix="openm74-reset-callees-") as scratch:
            run = subprocess.run([
                str(command), scratch, "scratch", "-import", str(args.image.resolve()),
                "-loader", "BinaryLoader", "-loader-baseAddr", "0xc00000",
                "-processor", "C166:LE:16:default", "-cspec", "tasking", "-readOnly",
                "-noanalysis", "-scriptPath", str(SCRIPT.parent.resolve()),
                "-postScript", SCRIPT.name,
            ], capture_output=True, text=True, timeout=180)
        output = run.stdout + run.stderr
        results = re.findall(r"CALLEE_RESULT=(\{.*\})", output)
        if (run.returncode or "ERROR" in output or len(results) != 1
                or output.count("PASS reset-callee survey;") != 1
                or re.findall(r"IMAGE_SHA256=([0-9a-f]{64})", output) != [digest]):
            lines = output.splitlines()
            errors = [line for line in lines if "ERROR" in line or "Exception" in line]
            raise ValueError("Ghidra survey failed\n" + "\n".join(errors[:6] + lines[-8:]))
        evidence = json.loads(results[0])
        if (evidence["root"] != "c0d572"
                or not 0 < evidence["visited_addresses"] <= 40000
                or not isinstance(evidence["frontiers"], list)
                or not isinstance(evidence["reset_literal_watch"], list)
                or evidence["reviewed_dsp_fallthrough"] != [
                    dict(address=address, instruction=instruction, length=4, flow="fallthrough")
                    for address, instruction in (
                        ("c5ea76", "comulu r4,[r0+]"), ("c5ea7a", "coshl #0x10"),
                        ("c5ea7e", "comacu r4,[r0+]"), ("c5ea84", "cosubr r2,[r0+]"))]):
            raise ValueError("invalid traversal metadata")
        modules = list((command.parent.parent / "Ghidra").rglob("c166.sla"))
        decoder_hash = (hashlib.sha256(modules[0].read_bytes()).hexdigest()
                        if len(modules) == 1 else None)
        evidence.update(located_decoder_sla_sha256=decoder_hash, image_sha256=digest,
                        script_sha256=hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                        scope="Static syntactic direct-flow traversal from reset routine. "
                              "Four exact-byte, manual-reviewed DSP instructions contribute only "
                              "static fallthrough edges, not execution semantics. "
                              "Indirect/out-of-image transfers are explicit frontiers. "
                              "Literal watch is not data-flow or absence proof. No execution, "
                              "interrupt analysis, reset or peripheral simulation.")
        if args.report:
            args.report.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"{evidence['visited_addresses']} visited addresses; "
              f"{len(evidence['direct_call_targets'])} direct call targets; "
              f"{len(evidence['frontiers'])} unresolved frontiers")
        print("Literal reset watch is not proof that indirect reset writes are absent")
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"reset-callee survey failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
