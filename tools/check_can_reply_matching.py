#!/usr/bin/env python3
"""Check a finite read-only host model against synthetic firmware p-code replies.

No adapter/transport is implemented. Timeouts are injected events, not clock settings.
The model never reuses a token and deliberately cannot read an entire firmware image.
"""
import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import zlib

from check_can_crc_hypothesis import END, START, recover_four

REPORT = Path(__file__).resolve().parent.parent / "docs/data/can-loader-emulation.json"


class ModelFault(ValueError):
    pass


class Tokens:
    def __init__(self):
        self.next = 0

    def allocate(self):
        if self.next == 256:
            raise ModelFault("token space exhausted; freshness contract unresolved")
        token = self.next
        self.next += 1
        return token


@dataclass(frozen=True)
class Request:
    data: bytes
    can_id: int = 0x100


class ReadModel:
    """One outstanding request, finite token budget, no device I/O or write encoder."""

    def __init__(self, addresses, max_retries=2):
        self.addresses = tuple(addresses)
        if not self.addresses or len(self.addresses) > 127:
            raise ModelFault("model supports 1..127 four-byte ranges per lifetime")
        if any(not isinstance(a, int) or a < START or a + 4 > END for a in self.addresses):
            raise ModelFault("range outside the reviewed CRC window")
        if max_retries < 0:
            raise ModelFault("negative retry budget")
        self.tokens = Tokens()
        self.max_retries = max_retries
        self.state = "new"
        self.index = 0
        self.pending = None
        self.retries = 0
        self.result = []
        self.rejected = Counter()

    def fail(self, reason):
        self.state = "failed"
        self.pending = None
        raise ModelFault(reason)

    def issue(self, state):
        token = self.tokens.allocate()
        address = self.addresses[self.index]
        data = {
            "startup": bytes([1, token, 0x96, 0x69, 0, 0, 0, 0]),
            "session": bytes([1, token, 2, 0, 0, 0, 0, 0]),
            "address": bytes([2, token, 0, 0]) + address.to_bytes(4, "little"),
            "crc": bytes([14, token, 4, 0, 0, 0, 0, 0]),
        }[state]
        self.state, self.pending, self.retries = state, Request(data), 0
        return self.pending

    def start(self):
        if self.state != "new":
            raise ModelFault("model lifetime cannot be restarted")
        return self.issue("startup")

    def timeout(self):
        if self.pending is None:
            raise ModelFault("no outstanding request")
        if self.state == "startup":
            self.fail("startup opportunity unknown; no automatic re-entry")
        if self.retries == self.max_retries:
            self.fail("read-only retry budget exhausted")
        self.retries += 1
        return self.pending  # Exact request and token; no later request is issued.

    def receive(self, data, can_id=0x101, extended=False):
        reason = None
        if self.pending is None:
            reason = "no_pending"
        elif extended or can_id != 0x101:
            reason = "wrong_id"
        elif len(data) != 8:
            reason = "wrong_length"
        elif data[0] != 255:
            reason = "wrong_header"
        elif data[2] != self.pending.data[1]:
            reason = "wrong_token"
        if reason:
            self.rejected[reason] += 1
            return None
        if data[1]:
            self.fail(f"device status {data[1]:02x}; payload is not accepted")
        if self.state == "startup":
            if data[3:6] != bytes([0x69, 0x96, 0x69]):
                self.rejected["wrong_shape"] += 1
                return None
            return self.issue("session")
        if self.state == "session":
            if data[3:] != bytes([0xFE, 1, 0, 0, 0]):
                self.rejected["wrong_shape"] += 1
                return None
            return self.issue("address")
        if self.state == "address":
            # Bytes 3..7 can retain the previous CRC. They do not identify this command.
            return self.issue("crc")
        if data[3] != 4:
            self.rejected["wrong_shape"] += 1
            return None
        self.result.append(recover_four(int.from_bytes(data[4:], "little")))
        self.index += 1
        if self.index == len(self.addresses):
            self.state, self.pending = "done", None
            return None
        return self.issue("address")


def require(condition, message):
    if not condition:
        raise ModelFault(message)


def expect_fault(action):
    try:
        action()
    except ModelFault:
        return
    raise ModelFault("expected rejection did not occur")


def advance(model, rows, count):
    request = model.start()
    for row in rows[:count]:
        require(request.data.hex() == row["request"], "request differs from p-code fixture")
        next_request = None
        for reply in row["replies"]:
            event = model.receive(bytes.fromhex(reply))
            if event is not None:
                next_request = event
        request = next_request
    return request


def check_trace(trace):
    synthetic = bytes(n * 3 + 7 for n in range(32))
    require(trace["kind"] == "synthetic-pcode-model", "not a synthetic p-code fixture")
    require(trace["payload_sha256"] == hashlib.sha256(synthetic).hexdigest(), "payload identity")
    rows = trace["rows"]
    require(len(rows) == 7, "fixture shape")
    model = ReadModel([START, START + 4])
    advance(model, rows, 6)
    require(model.state == "done" and b"".join(model.result) == synthetic[:8], "normal read")
    require(model.rejected["wrong_header"] == 1, "pending-startup zero response not counted")
    expect_fault(model.start)

    # A token reused after the selected address changes cannot distinguish these CRCs.
    old = bytes.fromhex(rows[3]["replies"][0])
    reused = bytes.fromhex(rows[6]["replies"][0])
    require(old[:4] == reused[:4] and old[4:] != reused[4:], "collision evidence")
    require(recover_four(int.from_bytes(old[4:], "little")) != synthetic[4:8],
            "delayed old CRC must demonstrate a wrong reconstructed range")
    # The address ACK itself also has a CRC-like tail, inherited from the earlier query.
    address_ack = bytes.fromhex(rows[4]["replies"][0])
    require(address_ack[3] == 4 and address_ack[4:] == old[4:], "stale address-ACK payload")

    for outstanding in (2, 3):  # Drop an address ACK or a CRC reply, then replay it twice.
        model = ReadModel([START, START + 4])
        request = advance(model, rows, outstanding)
        require(model.timeout() == request, "retry altered request/token")
        reply = bytes.fromhex(rows[outstanding]["replies"][0])
        next_request = model.receive(reply)
        require(next_request is not None, "read-only retry did not advance")
        require(model.receive(reply) is None, "duplicate reply advanced a later request")
        require(model.rejected["wrong_token"] == 1, "duplicate reply was not accounted for")

    for altered, can_id, extended in [
        (old[:-1], 0x101, False), (old, 0x100, False), (old, 0x101, True),
        (bytes([0]) + old[1:], 0x101, False),
        (old[:2] + bytes([99]) + old[3:], 0x101, False),
        (old[:3] + bytes([0]) + old[4:], 0x101, False),
    ]:
        model = ReadModel([START, START + 4])
        request = advance(model, rows, 3)
        require(model.receive(altered, can_id, extended) is None, "malformed reply advanced")
        require(model.pending == request and not model.result, "malformed reply committed data")
        require(sum(model.rejected.values()) == 2, "rejection was silent")  # Includes startup zero.

    model = ReadModel([START, START + 4])
    advance(model, rows, 3)
    error = old[:1] + bytes([0x32]) + old[2:]
    expect_fault(lambda: model.receive(error))
    require(model.state == "failed" and not model.result, "error payload was accepted")
    model = ReadModel([START])
    model.start()
    expect_fault(model.timeout)
    model = ReadModel([START], max_retries=1)
    advance(model, rows, 1)
    model.timeout()
    expect_fault(model.timeout)


def check_budget():
    expect_fault(lambda: ReadModel([START] * 128))
    expect_fault(lambda: ReadModel([END - 3]))
    tokens = Tokens()
    require([tokens.allocate() for _ in range(256)] == list(range(256)), "token uniqueness")
    expect_fault(tokens.allocate)
    model = ReadModel([START + 4 * n for n in range(127)])
    request = model.start()
    sent = []
    while request is not None:
        sent.append(request.data)
        token = request.data[1]
        if model.state == "startup":
            reply = bytes([255, 0, token, 0x69, 0x96, 0x69, 0, 0])
        elif model.state == "session":
            reply = bytes([255, 0, token, 0xFE, 1, 0, 0, 0])
        elif model.state == "address":
            reply = bytes([255, 0, token, 4, 0, 0, 0, 0])
        else:
            crc = zlib.crc32(model.addresses[model.index].to_bytes(4, "little"))
            reply = bytes([255, 0, token, 4]) + crc.to_bytes(4, "little")
        request = model.receive(reply)
    require(model.state == "done" and len(sent) == 256, "maximum finite batch")
    require(len({frame[1] for frame in sent}) == 256, "token was reused")
    require({frame[0] for frame in sent} <= {1, 2, 14}, "non-read-only command emitted")
    expect_fault(model.start)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        script = REPORT.parent.parent.parent / "tools/ghidra/EmulateCanLoader.java"
        require(evidence["script_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest(),
                "p-code evidence belongs to another script revision")
        require(bool(evidence["images"]), "empty evidence report")
        check_budget()
        for sample in evidence["images"]:
            check_trace(sample["synthetic_trace"])
            print(f"{sample['file']}: finite read model and injected reply faults passed")
        print("All reply-model checks passed; no token reassignment, adapter or firmware output")
        print("Long reads still require a measured reply-freshness contract.")
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"reply-model check failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
