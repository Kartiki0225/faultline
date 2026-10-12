"""The service tool's side of the wire.

The gateway only sees raw CAN frames. It decodes them, keeps the latest value of
every signal, tracks which faults are active, and can explain any frame bit by bit.
"""

import threading
import time
from collections import deque
from dataclasses import asdict

import can

import diagnostics
import j1939

COOLANT_WATCH_FROM = 95.0  # start predicting once coolant is above its normal band
COOLANT_LIMIT = 104.0


def hex_bytes(data: bytes) -> str:
    return data.hex(" ").upper()


class Gateway(threading.Thread):
    def __init__(self, bus: can.BusABC) -> None:
        super().__init__(daemon=True)
        self.bus = bus
        self.lock = threading.Lock()
        self.bam = j1939.BamReceiver()

        self.signals = {}                      # latest decoded value of every signal
        self.frames = deque(maxlen=20000)      # (number, time, can id, data), newest last
        self.frame_number = 0
        self.ids = {}                          # per identifier: count, last data, period
        self.lamps = j1939.Lamps()
        self.dtcs = []
        self.events = deque(maxlen=40)         # fault history shown on the dashboard
        self.coolant_history = deque(maxlen=20)

    def run(self) -> None:
        while True:
            message = self.bus.recv(timeout=0.5)
            if message is not None:
                self.handle(message.arbitration_id, bytes(message.data), time.time())

    def handle(self, can_id: int, data: bytes, now: float) -> None:
        frame_id = j1939.parse_id(can_id)
        with self.lock:
            self.frame_number += 1
            self.frames.append((self.frame_number, now, can_id, data))
            self._count(can_id, data, now)

            if frame_id.pgn in j1939.MESSAGES:
                values = j1939.decode(j1939.MESSAGES[frame_id.pgn], data)
                self.signals.update(values)
                if j1939.is_number(values.get("coolant_temp")):
                    self.coolant_history.append((now, values["coolant_temp"]))
            elif frame_id.pgn == j1939.DM1_PGN:
                self._update_faults(*j1939.decode_dm1(data), now)
            else:
                complete = self.bam.feed(frame_id, data)
                if complete and complete[0] == j1939.DM1_PGN:
                    self._update_faults(*j1939.decode_dm1(complete[1]), now)

    def _count(self, can_id: int, data: bytes, now: float) -> None:
        entry = self.ids.setdefault(can_id, {"count": 0, "time": now, "period": 0.0})
        if entry["count"]:
            entry["period"] = now - entry["time"]
        entry.update(count=entry["count"] + 1, time=now, data=data)

    def _update_faults(self, lamps: j1939.Lamps, dtcs: list[j1939.Dtc], now: float) -> None:
        before = {(dtc.spn, dtc.fmi) for dtc in self.dtcs}
        after = {(dtc.spn, dtc.fmi) for dtc in dtcs}
        for dtc in dtcs:
            if (dtc.spn, dtc.fmi) not in before:
                self._log(now, "set", dtc, lamps.red_stop)
        for dtc in self.dtcs:
            if (dtc.spn, dtc.fmi) not in after:
                self._log(now, "cleared", dtc, False)
        self.lamps, self.dtcs = lamps, dtcs

    def _log(self, now: float, kind: str, dtc: j1939.Dtc, red_stop: bool) -> None:
        info = diagnostics.describe(dtc, red_stop)
        self.events.append({"time": now, "kind": kind, "spn": dtc.spn, "fmi": dtc.fmi, "title": info["title"]})

    # ------------------------------------------------------------ for the dashboard

    def snapshot(self, after_frame: int = 0) -> dict:
        """Everything the dashboard needs, plus the frames that arrived since 'after_frame'."""
        with self.lock:
            now = time.time()
            new_frames = []
            for number, when, can_id, data in reversed(self.frames):
                if number <= after_frame or len(new_frames) >= 40:
                    break
                new_frames.append([number, round(when, 3), f"{can_id:08X}", hex_bytes(data)])

            warnings = []
            warning = diagnostics.early_warning(list(self.coolant_history), COOLANT_WATCH_FROM, COOLANT_LIMIT)
            if warning:
                warnings.append({"signal": "coolant_temp", **warning})

            return {
                "signals": dict(self.signals),
                "lamps": asdict(self.lamps),
                "faults": self._fault_list(),
                "warnings": warnings,
                "events": list(self.events),
                "ids": [self._id_row(can_id, entry) for can_id, entry in sorted(self.ids.items())],
                "frames": new_frames[::-1],
                "frame_number": self.frame_number,
                "frames_per_second": self._frames_in_last_second(now),
            }

    def _fault_list(self) -> list[dict]:
        """Active faults with their explanations, the most serious first."""
        faults = [diagnostics.describe(dtc, self.lamps.red_stop) for dtc in self.dtcs]
        return sorted(faults, key=lambda fault: not fault["severe"])

    def _frames_in_last_second(self, now: float) -> int:
        count = 0
        for _, when, _, _ in reversed(self.frames):
            if now - when > 1.0:
                break
            count += 1
        return count

    def _id_row(self, can_id: int, entry: dict) -> dict:
        frame_id = j1939.parse_id(can_id)
        return {
            "id": f"{can_id:08X}",
            "pgn": frame_id.pgn,
            "name": message_name(frame_id.pgn),
            "data": hex_bytes(entry["data"]),
            "count": entry["count"],
            "period_ms": round(entry["period"] * 100) * 10,  # to the nearest 10 ms
        }

    def log_text(self) -> str:
        """The recorded frames in the 'candump' text format used by Linux CAN tools."""
        with self.lock:
            return "".join(f"({when:.6f}) can0 {can_id:08X}#{data.hex().upper()}\n" for _, when, can_id, data in self.frames)


NAMES = {
    j1939.DM1_PGN: ("DM1", "Active Diagnostic Trouble Codes"),
    j1939.TP_CM_PGN: ("TP.CM", "Transport Protocol, Connection Management"),
    j1939.TP_DT_PGN: ("TP.DT", "Transport Protocol, Data Transfer"),
}


# What the engine is currently obeying (SPN 899). Only the modes this ECU uses.
TORQUE_MODES = {0: "low idle governor", 1: "accelerator pedal", 8: "torque limiting (derate)"}


def message_name(pgn: int) -> str:
    if pgn in j1939.MESSAGES:
        return j1939.MESSAGES[pgn].name
    return NAMES.get(pgn, ("?", ""))[0]


def explain(can_id: int, data: bytes) -> dict:
    """Break one frame down the way a person would decode it by hand."""
    frame_id = j1939.parse_id(can_id)
    pdu_format = (frame_id.pgn >> 8) & 0xFF
    result = {
        "id": f"{can_id:08X}",
        "data": hex_bytes(data),
        "bits": f"{can_id:029b}",
        "priority": frame_id.priority,
        "pgn": frame_id.pgn,
        "pgn_hex": f"{frame_id.pgn:04X}",
        "source": frame_id.source,
        "destination": frame_id.destination,
        "addressing": "sent to one address (PDU1)" if pdu_format < 240 else "broadcast to all (PDU2)",
        "name": message_name(frame_id.pgn),
        "title": "",
        "signals": [],
        "notes": [],
    }

    if frame_id.pgn in j1939.MESSAGES:
        message = j1939.MESSAGES[frame_id.pgn]
        result["title"] = message.title
        for signal in message.signals:
            raw = j1939.raw_value(signal, data)
            value = j1939.to_physical(signal, raw)
            last_byte = signal.start_byte + (signal.bit_length - 1) // 8
            result["signals"].append({
                "spn": signal.spn,
                "name": signal.name,
                "bytes": str(signal.start_byte) if last_byte == signal.start_byte else f"{signal.start_byte}-{last_byte}",
                "bit_length": signal.bit_length,
                "raw": raw,
                "raw_hex": f"{raw:0{(signal.bit_length + 3) // 4}X}",
                "scale": signal.scale,
                "offset": signal.offset,
                "unit": signal.unit,
                "value": value,
                "meaning": TORQUE_MODES.get(raw, "") if signal.key == "torque_mode" else "",
            })
    elif frame_id.pgn == j1939.DM1_PGN:
        result["title"] = NAMES[frame_id.pgn][1]
        lamps, dtcs = j1939.decode_dm1(data)
        lit = [name.replace("_", " ") for name, on in asdict(lamps).items() if on]
        result["notes"].append(f"Byte 1 = lamps: {', '.join(lit) if lit else 'all off'}")
        for dtc in dtcs:
            result["notes"].append(f"Bytes 3-6 = SPN {dtc.spn}, FMI {dtc.fmi}, occurred {dtc.count} time(s)")
        if not dtcs:
            result["notes"].append("Bytes 3-6 are zero: no active faults")
    elif frame_id.pgn == j1939.TP_CM_PGN:
        result["title"] = NAMES[frame_id.pgn][1]
        if data[0] == j1939.BAM:
            carried = int.from_bytes(data[5:8], "little")
            size = data[1] | (data[2] << 8)
            result["notes"].append("Byte 1 = 32: broadcast announce message (BAM)")
            result["notes"].append(f"A {size}-byte message is coming in {data[3]} packets")
            result["notes"].append(f"Bytes 6-8 = PGN {carried} ({message_name(carried)})")
    elif frame_id.pgn == j1939.TP_DT_PGN:
        result["title"] = NAMES[frame_id.pgn][1]
        result["notes"].append(f"Byte 1 = packet number {data[0]}")
        result["notes"].append("Bytes 2-8 = the next 7 bytes of the long message")
    return result
