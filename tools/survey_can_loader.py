#!/usr/bin/env python3
"""Fingerprint reviewed M74 boot regions without loading code or contacting hardware.

    python tools/survey_can_loader.py DIR [DIR ...] --csv report.csv

Directories contribute their immediate *.bin files. Full first-8-KiB hashes identify
reviewed regions; filenames and file sizes never establish a match. An unknown result
means unreviewed bytes, not absence of a loader or an unsupported microcontroller.
Only hashes and neutral filenames are exported, never paths or firmware contents.
"""
import argparse
import csv
import hashlib
from collections import Counter
from pathlib import Path

BOOT_SIZE = 0x2000
# These are equality classes of bytes, not a support/compatibility database.
BOOT_VARIANTS = {
    "a7162971cbbdaeebf8d9ae421220b981df64bcbd4bc876b503a7304ec2a0df99": "early",
    "7f1fd47c391a68460ebf3cfb78c0679c24e75470fd9787e368eb48032804310e": "common",
    "eb269ea46fe7432742f1d321ebe711cbf8a34b7aa1a03f78e6afaca4e2c84c7b": "common-tail",
}
FIELDS = ("file", "size", "sha256", "boot_size", "boot_sha256", "variant")
REGIONS = (("boot", 0, 0x2000), ("crc_window", 0x2000, 0x90000),
           ("excluded_tail", 0x90000, 0xC0000), ("excluded_array", 0xC0000, 0xD0000))
REGION_FIELDS = ("file", "file_sha256", "region", "start", "end_exclusive",
                 "expected_bytes", "bytes_present", "non_00_ff_bytes", "sha256")


def inspect(path):
    data = path.read_bytes()
    boot = data[:BOOT_SIZE]
    digest = hashlib.sha256(boot).hexdigest()
    return dict(file=path.name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                boot_size=len(boot), boot_sha256=digest,
                variant=BOOT_VARIANTS.get(digest, "unknown"))


def inspect_regions(path):
    data = path.read_bytes()
    if hashlib.sha256(data[:BOOT_SIZE]).hexdigest() not in BOOT_VARIANTS:
        raise ValueError(f"{path.name}: unreviewed mapping; region survey refused")
    file_hash = hashlib.sha256(data).hexdigest()
    rows = []
    for label, start, end in REGIONS:
        part = data[start:end]
        rows.append(dict(file=path.name, file_sha256=file_hash, region=label,
                         start=f"0x{0xC00000 + start:X}",
                         end_exclusive=f"0x{0xC00000 + end:X}", expected_bytes=end - start,
                         bytes_present=len(part), non_00_ff_bytes=sum(
                             byte not in (0, 255) for byte in part),
                         sha256=hashlib.sha256(part).hexdigest()))
    return rows


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--csv", type=Path, help="write hash-only evidence to this file")
    parser.add_argument("--regions-csv", type=Path,
                        help="write region hashes/counts for reviewed images only")
    args = parser.parse_args(argv)
    paths = set()
    for path in args.paths:
        if path.is_dir():
            found = list(path.glob("*.bin"))
            if not found:
                parser.error(f"no *.bin files in {path}")
            paths.update(p.resolve() for p in found)
        elif path.is_file():
            paths.add(path.resolve())
        else:
            parser.error(f"not a file or directory: {path}")
    names = Counter(p.name for p in paths)
    if any(count > 1 for count in names.values()):
        parser.error("duplicate filenames: use separate reports to keep image identities "
                     "unambiguous")
    outputs = [p.resolve() for p in (args.csv, args.regions_csv) if p]
    if any(path in paths for path in outputs):
        parser.error("the report must not overwrite an input image")
    if len(outputs) != len(set(outputs)):
        parser.error("reports must use distinct output files")
    try:
        ordered = sorted(paths, key=lambda p: p.name)
        rows = [inspect(p) for p in ordered]
        region_rows = []
        if args.regions_csv:
            for path, image_row in zip(ordered, rows):
                parts = inspect_regions(path)
                if parts[0]["file_sha256"] != image_row["sha256"]:
                    raise ValueError(f"{path.name}: input changed during survey")
                region_rows.extend(parts)
        if args.csv:
            write_csv(args.csv, FIELDS, rows)
        if args.regions_csv:
            write_csv(args.regions_csv, REGION_FIELDS, region_rows)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"survey failed: {exc}\n")
    print(f"Files inspected: {len(rows)}")
    for variant, count in sorted(Counter(row["variant"] for row in rows).items()):
        print(f"  {variant}: {count}")
    print("Static byte matches only; no hardware or transport support is established.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
