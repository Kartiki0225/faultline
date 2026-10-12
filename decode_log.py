"""Decode a recorded CAN log (the file from "Download log") into readable values.

Run:  python decode_log.py faultline.log

It prints the range of every signal and the time each fault was set or cleared.
The same decoder that feeds the live dashboard is used here.
"""

import re
import sys

import j1939
from gateway import Gateway

# One line of a candump log looks like:  (1760290000.123456) can0 0CF00400#F1A5A0E02EFFFFFF
LINE = re.compile(r"\((\d+\.\d+)\)\s+\S+\s+([0-9A-Fa-f]{8})#([0-9A-Fa-f]{16})")


def read_log(text: str):
    """Yield (time, can id, data bytes) for every frame in the log."""
    for line in text.splitlines():
        match = LINE.match(line.strip())
        if match:
            yield float(match[1]), int(match[2], 16), bytes.fromhex(match[3])


def summarise(text: str) -> dict:
    gateway = Gateway(bus=None)
    ranges = {}  # signal key -> [lowest, highest]
    first = last = None
    frames = 0

    for when, can_id, data in read_log(text):
        first = when if first is None else first
        last = when
        frames += 1
        gateway.handle(can_id, data, when)

        pgn = j1939.parse_id(can_id).pgn
        if pgn in j1939.MESSAGES:
            for key, value in j1939.decode(j1939.MESSAGES[pgn], data).items():
                if j1939.is_number(value):
                    low, high = ranges.get(key, (value, value))
                    ranges[key] = (min(low, value), max(high, value))

    return {
        "frames": frames,
        "seconds": (last - first) if frames else 0.0,
        "start": first,
        "ranges": ranges,
        "events": list(gateway.events),
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python decode_log.py <file.log>")
    with open(sys.argv[1], encoding="utf-8") as file:
        summary = summarise(file.read())

    print(f"{summary['frames']} frames over {summary['seconds']:.1f} seconds\n")
    print(f"{'Signal':34} {'Lowest':>10} {'Highest':>10}")
    for key, (low, high) in summary["ranges"].items():
        signal = j1939.SIGNALS[key]
        print(f"{signal.name:34} {low:>10g} {high:>10g} {signal.unit}")

    print("\nFaults")
    if not summary["events"]:
        print("  none")
    for event in summary["events"]:
        seconds = event["time"] - summary["start"]
        print(f"  {seconds:7.1f} s  {event['kind']:8} SPN {event['spn']} FMI {event['fmi']}  {event['title']}")


if __name__ == "__main__":
    main()
