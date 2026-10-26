#!/usr/bin/env python3
"""Check whether short CRC replies could reconstruct bytes, entirely offline.

    python tools/check_can_crc_hypothesis.py [IMAGE.bin ...]

This models the reviewed loader's CRC math, not its CPU, CAN peripheral or host
protocol. It opens no adapter and exports no reconstructed firmware. Image checks
require a reviewed boot fingerprint. Passing is NOT proof of a working CAN reader.
"""
import argparse
import hashlib
import random
import zlib
from pathlib import Path

from survey_can_loader import BOOT_SIZE, BOOT_VARIANTS

FLASH_BASE = 0xC00000
START = 0xC02000
END = 0xC90000
POLYNOMIAL = 0xEDB88320


def make_table():
    # Transliteration of the shifts/xors in I444GI04 at C00986..C009C8.
    table = []
    for index in range(256):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ (POLYNOMIAL if value & 1 else 0)
        table.append(value)
    return table


TABLE = make_table()


def loader_crc(data):
    # C008E0..C00940: all-ones seed, reflected table update, final complement.
    value = 0xFFFFFFFF
    for byte in data:
        value = (value >> 8) ^ TABLE[(value ^ byte) & 255]
    return value ^ 0xFFFFFFFF


def inverse_rows():
    # CRC is affine for a fixed message length. Remove the zero-message constant,
    # form the binary linear map, then invert it with Gauss-Jordan elimination.
    zero = zlib.crc32(bytes(4))
    columns = [zlib.crc32((1 << bit).to_bytes(4, "little")) ^ zero
               for bit in range(32)]
    rows = [sum(((column >> bit) & 1) << index
                for index, column in enumerate(columns)) | (1 << (32 + bit))
            for bit in range(32)]
    for column in range(32):
        pivot = next((row for row in range(column, 32) if rows[row] & (1 << column)), None)
        if pivot is None:
            raise ValueError("four-byte CRC transform is not invertible")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for row in range(32):
            if row != column and rows[row] & (1 << column):
                rows[row] ^= rows[column]
    if any((row & 0xFFFFFFFF) != 1 << bit for bit, row in enumerate(rows)):
        raise ValueError("matrix elimination failed")
    return zero, [row >> 32 for row in rows]


ZERO, INVERSE = inverse_rows()


def recover_four(crc):
    delta = crc ^ ZERO
    value = sum((bin(mask & delta).count("1") & 1) << bit
                for bit, mask in enumerate(INVERSE))
    return value.to_bytes(4, "little")


def check_math():
    rng = random.Random(0x74)
    samples = [bytes(4), b"\xff" * 4]
    samples += [(1 << bit).to_bytes(4, "little") for bit in range(32)]
    samples += [rng.getrandbits(32).to_bytes(4, "little") for _ in range(256)]
    if loader_crc(b"123456789") != 0xCBF43926:
        raise ValueError("CRC known-answer check failed")
    for sample in samples:
        crc = loader_crc(sample)
        if crc != zlib.crc32(sample) or recover_four(crc) != sample:
            raise ValueError("CRC reconstruction check failed")
    if len({loader_crc(bytes([byte])) for byte in range(256)}) != 256:
        raise ValueError("one-byte CRCs are not unique")
    print(f"CRC known answer, matrix rank 32, {len(samples)} four-byte cases: OK")
    print("All 256 one-byte CRCs are distinct.")


def check_image(path):
    image = path.read_bytes()
    variant = BOOT_VARIANTS.get(hashlib.sha256(image[:BOOT_SIZE]).hexdigest())
    if variant is None:
        raise ValueError(f"{path.name}: boot region is unreviewed")
    if len(image) < END - FLASH_BASE:
        raise ValueError(f"{path.name}: image does not cover the modeled CRC range")
    count = 0
    for offset in range(START - FLASH_BASE, END - FLASH_BASE, 4):
        expected = image[offset:offset + 4]
        # Reply is computed by the transliteration; recovery uses the separately
        # constructed inverse of zlib's map. No bytes from expected enter recovery.
        recovered = recover_four(loader_crc(expected))
        if recovered != expected:
            raise ValueError(f"{path.name}: mismatch at {FLASH_BASE + offset:#x}")
        count += 1
    print(f"{path.name}: {count} local four-byte ranges recovered exactly; variant={variant}")
    print(f"  image SHA256 {hashlib.sha256(image).hexdigest()}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="*", type=Path)
    args = parser.parse_args(argv)
    try:
        check_math()
        for path in args.images:
            check_image(path)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"offline check failed: {exc}\n")
    if not args.images:
        print("No firmware samples supplied; firmware-specific check not performed.")
    print("Hypothesis only: no CPU/peripheral emulation or CAN exchange was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
