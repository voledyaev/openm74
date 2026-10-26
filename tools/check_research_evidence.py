#!/usr/bin/env python3
"""Check archived research metadata against local sources; no firmware or Ghidra required.

This detects stale script fingerprints and broken corpus references. It does not rerun
firmware, validate a decoder or turn synthetic results into hardware evidence.
"""
import csv
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/data"
SCRIPTS = ROOT / "tools/ghidra"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(path):
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = {}
    for row in rows:
        name = row["file"]
        if Path(name).name != name or name in result:
            raise ValueError(f"{path.name}: duplicate or non-basename file {name}")
        for key in ("sha256", "boot_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", row[key]):
                raise ValueError(f"{path.name}: malformed {key}")
        result[name] = row
    return result


def check():
    images = inventory(DATA / "can-loader-images.csv")
    controls = inventory(DATA / "can-loader-controls.csv")
    if len(images) != 120 or len(controls) != 47:
        raise ValueError("unexpected corpus coverage; review the documented scope")
    scripts = {}
    for path in SCRIPTS.glob("*.java"):
        name = path.stem
        report = {"EmulateCanLoader": "can-loader-emulation",
                  "SurveyResetCallees": "reset-callees"}.get(name)
        if report is None:
            report = re.sub(r"(?<!^)(?=[A-Z])", "-", name.removeprefix("Check")).lower()
        scripts[report] = path
    reports = {path.stem: path for path in DATA.glob("*.json")}
    if reports.keys() != scripts.keys():
        raise ValueError("each research script needs exactly one archived JSON report")
    for name, path in sorted(reports.items()):
        report = json.loads(path.read_text(encoding="utf-8"))
        script = scripts[name]
        if report["script_sha256"] != digest(script) or not report.get("scope"):
            raise ValueError(f"{path.name}: stale script hash or missing model scope")
        # Check the whole inherited implementation, not only the entry script.
        recorded = {value for key, value in report.items() if key.endswith("_sha256")}
        current = script
        while True:
            match = re.search(r"public class \w+ extends (\w+)", current.read_text())
            if not match or match[1] == "GhidraScript":
                break
            current = SCRIPTS / (match[1] + ".java")
            if digest(current) not in recorded:
                raise ValueError(f"{path.name}: missing/stale ancestor {current.name}")
        image_hashes = ([report["image_sha256"]] if "image_sha256" in report else
                        [entry["sha256"] for entry in report["images"]])
        known = {row["sha256"] for row in images.values()}
        if any(value not in known for value in image_hashes):
            raise ValueError(f"{path.name}: image absent from reviewed inventory")
        decoder = report.get("located_decoder_sla_sha256")
        if decoder is not None and not re.fullmatch(r"[0-9a-f]{64}", decoder):
            raise ValueError(f"{path.name}: malformed decoder fingerprint")
    with (DATA / "can-loader-regions.csv").open(encoding="utf-8", newline="") as stream:
        regions = list(csv.DictReader(stream))
    seen = set()
    for row in regions:
        key = row["file"], row["region"]
        if key in seen or row["file_sha256"] != images[row["file"]]["sha256"]:
            raise ValueError("duplicate region or stale image reference")
        seen.add(key)
        expected, present = int(row["expected_bytes"]), int(row["bytes_present"])
        if int(row["end_exclusive"], 16) - int(row["start"], 16) != expected:
            raise ValueError("region bounds disagree with length")
        if not 0 <= int(row["non_00_ff_bytes"]) <= present <= expected:
            raise ValueError("region presence/blankness counts disagree")
    if len(regions) != 4 * len(images):
        raise ValueError("expected four coverage regions per image")
    print(f"PASS {len(reports)} reports: source ancestry and corpus references agree; "
          "no firmware execution or hardware validation")


def main():
    try:
        check()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"research evidence check failed: {exc}") from exc


if __name__ == "__main__":
    main()
