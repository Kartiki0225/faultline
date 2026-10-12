"""A small implementation of SAE J1939, the CAN protocol used by heavy-duty diesel engines.

A CAN frame is tiny: a 29-bit identifier and up to 8 bytes of data.
J1939 gives those bits a meaning:

  * the identifier says WHAT the message is (its PGN) and WHO sent it (source address)
  * the 8 data bytes hold several values (SPNs), each at a fixed position with a
    fixed scale and offset

This file covers three things:
  1. building and reading identifiers
  2. packing values into data bytes and unpacking them again
  3. diagnostic messages (DM1) and the transport protocol that carries them
     when they do not fit in 8 bytes
"""

from dataclasses import dataclass

GLOBAL_ADDRESS = 0xFF  # "to everyone"
ENGINE_ADDRESS = 0x00  # source address of engine controller number 1


# ---------------------------------------------------------------- identifiers
#
# The 29-bit identifier, from the highest bit to the lowest:
#
#   priority | reserved | data page | PDU format | PDU specific | source address
#    3 bits     1 bit      1 bit       8 bits       8 bits          8 bits
#
# PGN (Parameter Group Number) = reserved + data page + PDU format + PDU specific.
# If PDU format is below 240, the message is sent to one address, and
# "PDU specific" holds that destination instead of being part of the PGN.


@dataclass(frozen=True)
class FrameId:
    priority: int
    pgn: int
    source: int
    destination: int


def build_id(priority: int, pgn: int, source: int, destination: int = GLOBAL_ADDRESS) -> int:
    pdu_format = (pgn >> 8) & 0xFF
    if pdu_format < 240:
        pgn = (pgn & 0x3FF00) | destination
    return (priority << 26) | (pgn << 8) | source


def parse_id(can_id: int) -> FrameId:
    priority = (can_id >> 26) & 0x7
    pgn = (can_id >> 8) & 0x3FFFF
    source = can_id & 0xFF
    destination = GLOBAL_ADDRESS

    pdu_format = (pgn >> 8) & 0xFF
    if pdu_format < 240:
        destination = pgn & 0xFF
        pgn = pgn & 0x3FF00
    return FrameId(priority, pgn, source, destination)


# ---------------------------------------------------------------- signals
#
# Positions are written the way the J1939 documents write them: bytes are
# numbered 1 to 8 and bits 1 to 8, and multi-byte values are little-endian
# (low byte first).
#
#   physical value = raw value * scale + offset

ERROR = "error"       # the sensor is faulty
NOT_AVAILABLE = None  # the sender does not have this value


def is_number(value) -> bool:
    """True for a real reading, False for ERROR and NOT_AVAILABLE."""
    return isinstance(value, (int, float))


@dataclass(frozen=True)
class Signal:
    spn: int          # Suspect Parameter Number: the official id of this value
    key: str          # short name used in code and JSON
    name: str
    start_byte: int
    bit_length: int
    scale: float
    offset: float
    unit: str
    start_bit: int = 1

    @property
    def shift(self) -> int:
        """How far the value sits from the low end of the 64 data bits."""
        return (self.start_byte - 1) * 8 + (self.start_bit - 1)

    @property
    def mask(self) -> int:
        return (1 << self.bit_length) - 1


@dataclass(frozen=True)
class Message:
    pgn: int
    name: str
    title: str
    priority: int
    period: float  # seconds between broadcasts
    signals: tuple[Signal, ...]


MESSAGES = {
    message.pgn: message
    for message in [
        Message(61444, "EEC1", "Electronic Engine Controller 1", 3, 0.1, (
            Signal(899, "torque_mode", "Engine Torque Mode", 1, 4, 1, 0, ""),
            Signal(512, "demand_torque", "Driver's Demand Torque", 2, 8, 1, -125, "%"),
            Signal(513, "actual_torque", "Actual Engine Torque", 3, 8, 1, -125, "%"),
            Signal(190, "rpm", "Engine Speed", 4, 16, 0.125, 0, "rpm"),
        )),
        Message(61443, "EEC2", "Electronic Engine Controller 2", 3, 0.1, (
            Signal(91, "pedal", "Accelerator Pedal Position", 2, 8, 0.4, 0, "%"),
            Signal(92, "load", "Engine Load at Current Speed", 3, 8, 1, 0, "%"),
        )),
        Message(65262, "ET1", "Engine Temperature 1", 6, 1.0, (
            Signal(110, "coolant_temp", "Engine Coolant Temperature", 1, 8, 1, -40, "°C"),
        )),
        Message(65263, "EFL/P1", "Engine Fluid Level/Pressure 1", 6, 0.5, (
            Signal(100, "oil_pressure", "Engine Oil Pressure", 4, 8, 4, 0, "kPa"),
            Signal(111, "coolant_level", "Engine Coolant Level", 8, 8, 0.4, 0, "%"),
        )),
        Message(65270, "IC1", "Inlet/Exhaust Conditions 1", 6, 0.5, (
            Signal(102, "boost", "Intake Manifold Pressure", 2, 8, 2, 0, "kPa"),
            Signal(105, "intake_temp", "Intake Manifold Temperature", 3, 8, 1, -40, "°C"),
            Signal(107, "air_filter_dp", "Air Filter Differential Pressure", 5, 8, 0.05, 0, "kPa"),
            Signal(173, "exhaust_temp", "Exhaust Gas Temperature", 6, 16, 0.03125, -273, "°C"),
        )),
        Message(65266, "LFE", "Fuel Economy (Liquid)", 6, 0.1, (
            Signal(183, "fuel_rate", "Engine Fuel Rate", 1, 16, 0.05, 0, "L/h"),
        )),
        Message(65271, "VEP1", "Vehicle Electrical Power 1", 6, 1.0, (
            Signal(168, "battery", "Battery Voltage", 5, 16, 0.05, 0, "V"),
        )),
    ]
}

SIGNALS = {signal.key: signal for message in MESSAGES.values() for signal in message.signals}


def reserved_raws(signal: Signal) -> tuple[int, int]:
    """J1939 keeps the highest raw values for 'error' and 'not available'."""
    if signal.bit_length == 16:
        return 0xFE00, 0xFF00
    return signal.mask - 1, signal.mask


def max_valid_raw(signal: Signal) -> int:
    if signal.bit_length == 16:
        return 0xFAFF
    if signal.bit_length == 8:
        return 0xFA
    return signal.mask - 2


def encode(message: Message, values: dict) -> bytes:
    """Pack physical values into the 8 data bytes of a message.

    Bytes that carry nothing stay 0xFF, which J1939 reads as "not available".
    """
    data = (1 << 64) - 1  # all 64 bits set
    for signal in message.signals:
        value = values.get(signal.key, NOT_AVAILABLE)
        error_raw, missing_raw = reserved_raws(signal)

        if value is NOT_AVAILABLE:
            raw = missing_raw
        elif value == ERROR:
            raw = error_raw
        else:
            raw = round((value - signal.offset) / signal.scale)
            raw = max(0, min(max_valid_raw(signal), raw))

        data &= ~(signal.mask << signal.shift)  # clear the bits of this signal
        data |= raw << signal.shift             # write the new value
    return data.to_bytes(8, "little")


def raw_value(signal: Signal, data: bytes) -> int:
    """Treat the 8 bytes as one 64-bit number, then shift and mask out one signal."""
    return (int.from_bytes(data, "little") >> signal.shift) & signal.mask


def to_physical(signal: Signal, raw: int):
    error_raw, missing_raw = reserved_raws(signal)
    if raw >= missing_raw:
        return NOT_AVAILABLE
    if raw >= error_raw:
        return ERROR
    return round(raw * signal.scale + signal.offset, 3)


def decode(message: Message, data: bytes) -> dict:
    """Unpack the data bytes of a message into physical values."""
    return {signal.key: to_physical(signal, raw_value(signal, data)) for signal in message.signals}


# ---------------------------------------------------------------- diagnostics (DM1)
#
# DM1 (PGN 65226) lists the faults that are active right now.
#
#   byte 1      lamp status, 2 bits each: malfunction, red stop, amber warning, protect
#   byte 2      lamp flash status (0xFF = do not flash)
#   then 4 bytes for each fault (a DTC, Diagnostic Trouble Code):
#       SPN  19 bits   which value has the problem
#       FMI   5 bits   what kind of problem (too high, too low, short circuit...)
#       CM    1 bit    SPN conversion method, always 0 in current versions
#       OC    7 bits   occurrence count: how many times it has happened

DM1_PGN = 65226


@dataclass(frozen=True)
class Dtc:
    spn: int
    fmi: int
    count: int = 1


@dataclass(frozen=True)
class Lamps:
    malfunction: bool = False
    red_stop: bool = False
    amber_warning: bool = False
    protect: bool = False


def encode_dtc(dtc: Dtc) -> bytes:
    return bytes([
        dtc.spn & 0xFF,
        (dtc.spn >> 8) & 0xFF,
        ((dtc.spn >> 16) & 0x07) << 5 | (dtc.fmi & 0x1F),
        min(dtc.count, 126) & 0x7F,
    ])


def decode_dtc(data: bytes) -> Dtc:
    spn = data[0] | (data[1] << 8) | ((data[2] >> 5) << 16)
    return Dtc(spn=spn, fmi=data[2] & 0x1F, count=data[3] & 0x7F)


def encode_dm1(lamps: Lamps, dtcs: list[Dtc]) -> bytes:
    lamp_byte = (lamps.malfunction << 6) | (lamps.red_stop << 4) | (lamps.amber_warning << 2) | lamps.protect
    payload = bytes([lamp_byte, 0xFF])
    for dtc in dtcs:
        payload += encode_dtc(dtc)
    if not dtcs:
        payload += bytes(4)  # SPN 0, FMI 0 means "no active faults"
    return payload.ljust(8, b"\xff")


def decode_dm1(payload: bytes) -> tuple[Lamps, list[Dtc]]:
    lamps = Lamps(
        malfunction=(payload[0] >> 6) & 0x3 == 1,
        red_stop=(payload[0] >> 4) & 0x3 == 1,
        amber_warning=(payload[0] >> 2) & 0x3 == 1,
        protect=payload[0] & 0x3 == 1,
    )
    dtcs = []
    for start in range(2, len(payload) - 3, 4):
        dtc = decode_dtc(payload[start:start + 4])
        if dtc.spn != 0:
            dtcs.append(dtc)
    return lamps, dtcs


# ---------------------------------------------------------------- transport protocol
#
# One CAN frame holds 8 bytes, but DM1 with two or more faults is longer.
# J1939 then splits the message:
#
#   TP.CM (PGN 60416), control byte 32 = BAM "Broadcast Announce Message":
#       says which PGN is coming, how many bytes and how many packets
#   TP.DT (PGN 60160), one frame per packet:
#       byte 1 is the packet number, bytes 2 to 8 are 7 bytes of the message

TP_CM_PGN = 60416
TP_DT_PGN = 60160
BAM = 32


def split_bam(pgn: int, payload: bytes) -> list[tuple[int, bytes]]:
    """Split a long message into (pgn, data) frames: one announcement, then the packets."""
    packets = [payload[i:i + 7] for i in range(0, len(payload), 7)]
    announce = bytes([BAM, len(payload) & 0xFF, len(payload) >> 8, len(packets), 0xFF]) + pgn.to_bytes(3, "little")

    frames = [(TP_CM_PGN, announce)]
    for number, packet in enumerate(packets, start=1):
        frames.append((TP_DT_PGN, bytes([number]) + packet.ljust(7, b"\xff")))
    return frames


class BamReceiver:
    """Rebuilds long messages from TP.CM and TP.DT frames, one transfer per sender."""

    def __init__(self) -> None:
        self.transfers = {}  # source address -> transfer in progress

    def feed(self, frame_id: FrameId, data: bytes) -> tuple[int, bytes] | None:
        """Give it every received frame. Returns (pgn, payload) when a message is complete."""
        if frame_id.pgn == TP_CM_PGN and data[0] == BAM:
            self.transfers[frame_id.source] = {
                "pgn": int.from_bytes(data[5:8], "little"),
                "size": data[1] | (data[2] << 8),
                "packets": data[3],
                "next": 1,
                "payload": b"",
            }
        elif frame_id.pgn == TP_DT_PGN and frame_id.source in self.transfers:
            transfer = self.transfers[frame_id.source]
            if data[0] != transfer["next"]:  # a packet was lost or arrived out of order
                del self.transfers[frame_id.source]
                return None
            transfer["payload"] += data[1:8]
            transfer["next"] += 1
            if data[0] == transfer["packets"]:
                del self.transfers[frame_id.source]
                return transfer["pgn"], transfer["payload"][:transfer["size"]]
        return None
