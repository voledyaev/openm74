#!/usr/bin/env python3
"""Explore a bounded abstract FIFO fence; no firmware, adapter or wire encoder.

Logical request identities are oracle-only: the host sees kind/token, not epoch.
This checks conditional safety, not liveness or a real backend's guarantees.
"""
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class State:
    current: int
    retried: bool
    tx: tuple
    rx: tuple


def explore(*, tx_fifo=True, rx_fifo=True, fence=True, retry_fence=False,
            duplicate_fence=False, data_tokens=2):
    # Three epochs reuse two data tokens; fences have a distinct response signature.
    sequence = tuple([("data", 0), ("data", 1), ("fence", 255)] * 2
                     + [("data", 0), ("data", 1)])
    sequence = tuple((kind, token % data_tokens if kind == "data" else token)
                     for kind, token in sequence)
    if not fence:
        sequence = tuple(item for item in sequence if item[0] != "fence")
    initial = State(0, False, (0,), ())
    queue = deque([initial])
    parents = {initial: None}
    completed = 0

    def witness(state, event):
        events = [event]
        while parents[state] is not None:
            state, event = parents[state]
            events.append(event)
        return list(reversed(events))

    while queue:
        state = queue.popleft()
        current, retried, tx, rx = (state.current, state.retried, state.tx, state.rx)
        if current == len(sequence):
            completed += 1
            continue
        successors = []
        if not retried and (sequence[current][0] == "data" or retry_fence):
            successors.append((State(current, True, tx + (current,), rx),
                               f"retry {current}"))
        for i in range(min(len(tx), 1) if tx_fifo else len(tx)):
            message = tx[i]
            tail = tx[:i] + tx[i + 1:]
            # Each delivered request generates exactly one ordered reply, unless
            # deliberately violating the fence-uniqueness assumption below.
            copies = 2 if duplicate_fence and sequence[message][0] == "fence" else 1
            successors.append((State(current, retried, tail, rx + (message,) * copies),
                               f"execute {message}"))
            successors.append((State(current, retried, tail, rx), f"lose request {message}"))
        for i in range(min(len(rx), 1) if rx_fifo else len(rx)):
            message = rx[i]
            tail = rx[:i] + rx[i + 1:]
            successors.append((State(current, retried, tx, tail), f"lose reply {message}"))
            if sequence[message] == sequence[current]:
                event = f"accept reply {message} as {current}"
                if message != current:
                    return len(parents), completed, witness(state, event)
                following = current + 1
                outgoing = tx + (following,) if following < len(sequence) else tx
                successors.append((State(following, False, outgoing, tail), event))
            else:
                successors.append((State(current, retried, tx, tail),
                                   f"ignore reply {message}"))
        for successor, event in successors:
            if successor not in parents:
                parents[successor] = (state, event)
                queue.append(successor)
    return len(parents), completed, None


def main():
    for label, options in [
        ("FIFO with fence", {}),
        ("FIFO without fence", dict(fence=False)),
        ("FIFO with fence retries", dict(retry_fence=True)),
        ("FIFO with adjacent fence duplicates", dict(duplicate_fence=True)),
    ]:
        states, completed, failure = explore(**options)
        if failure or not completed:
            raise AssertionError((label, states, completed, failure))
        print(f"{label}: {states} states, {completed} terminal states; "
              "no stale reply accepted")
    for label, options in [
        ("request reordering", dict(tx_fifo=False)),
        ("reply reordering", dict(rx_fifo=False)),
        ("same token on adjacent data requests", dict(fence=False, data_tokens=1)),
    ]:
        states, _, failure = explore(**options)
        if not failure:
            raise AssertionError(f"missing counterexample: {label}")
        print(f"{label}: stale acceptance after {len(failure)} events "
              f"({states} explored states)")
        print("  " + " -> ".join(failure))
    print("Bounded abstract safety only; no ECU/backend guarantee or long-read implementation")


if __name__ == "__main__":
    main()
