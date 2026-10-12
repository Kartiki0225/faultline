"""The engine control unit (ECU).

Twenty times a second it:
  1. reads the engine's sensors
  2. runs its onboard diagnostics (is any value out of range for too long?)
  3. protects the engine (limits torque, or shuts it down)
  4. broadcasts everything on the CAN bus as J1939 messages
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable

import can

import j1939
from engine import FAULTS, IDLE_RPM, MAX_RPM, Engine

TICK = 0.05            # seconds between steps
CLEAR_AFTER = 2.0      # a fault clears after its value has been normal this long
DERATE_LIMIT = 0.4     # torque allowed while a serious fault is active
DM1_PERIOD = 1.0       # seconds between diagnostic messages


@dataclass
class Monitor:
    """One diagnostic check. It becomes an active fault (a DTC) when its condition
    stays true for 'set_after' seconds, so a single noisy reading never trips it."""

    spn: int
    fmi: int
    lamp: str                       # "amber" (warning) or "red" (stop the engine)
    set_after: float
    condition: Callable[[dict], bool]
    derate: bool = False            # limit torque while active
    shutdown_after: float | None = None  # stop the engine if still active after this long

    bad_for: float = 0.0
    good_for: float = 0.0
    active_for: float = 0.0
    active: bool = False
    count: int = 0

    def update(self, sensors: dict, dt: float) -> None:
        if self.condition(sensors):
            self.bad_for += dt
            self.good_for = 0.0
            if not self.active and self.bad_for >= self.set_after:
                self.active = True
                self.count += 1
        else:
            self.good_for += dt
            self.bad_for = 0.0
            if self.active and self.good_for >= CLEAR_AFTER:
                self.active = False
        self.active_for = self.active_for + dt if self.active else 0.0

    def reset(self) -> None:
        self.bad_for = self.good_for = self.active_for = 0.0
        self.active = False


def above(key: str, limit: float) -> Callable[[dict], bool]:
    return lambda sensors: j1939.is_number(sensors[key]) and sensors[key] > limit


def below(key: str, limit: float) -> Callable[[dict], bool]:
    return lambda sensors: j1939.is_number(sensors[key]) and sensors[key] < limit


def oil_pressure_below(at_idle: float, at_max_rpm: float) -> Callable[[dict], bool]:
    """Normal oil pressure rises with engine speed, so the limit has to rise too."""
    def check(sensors: dict) -> bool:
        if sensors["rpm"] < 500:
            return False  # engine not running: no oil pressure is expected
        fraction = (sensors["rpm"] - IDLE_RPM) / (MAX_RPM - IDLE_RPM)
        return sensors["oil_pressure"] < at_idle + (at_max_rpm - at_idle) * fraction
    return check


def build_monitors() -> list[Monitor]:
    return [
        Monitor(111, 18, "amber", 2.0, below("coolant_level", 75)),
        Monitor(110, 16, "amber", 2.0, above("coolant_temp", 104)),
        Monitor(110, 0, "red", 2.0, above("coolant_temp", 110), derate=True, shutdown_after=8.0),
        Monitor(110, 3, "amber", 1.0, lambda sensors: sensors["coolant_temp"] == j1939.ERROR),
        Monitor(100, 18, "amber", 2.0, oil_pressure_below(113, 180)),
        Monitor(100, 1, "red", 2.0, oil_pressure_below(73, 120), derate=True, shutdown_after=4.0),
        Monitor(107, 16, "amber", 3.0, above("air_filter_dp", 5.0)),
        Monitor(168, 18, "amber", 3.0, lambda sensors: sensors["rpm"] > 500 and sensors["battery"] < 24.0),
    ]


class Ecu(threading.Thread):
    def __init__(self, bus: can.BusABC) -> None:
        super().__init__(daemon=True)
        self.bus = bus
        self.engine = Engine()
        self.monitors = build_monitors()
        self.lock = threading.Lock()  # the web server changes the engine from another thread
        self.protection_shutdown = False

        self.ticks = 0
        self.packet_queue = []  # transport protocol packets waiting to be sent

    # ------------------------------------------------------------ commands

    def start_engine(self) -> None:
        with self.lock:
            self.protection_shutdown = False
            self.engine.start()

    def stop_engine(self) -> None:
        with self.lock:
            self.engine.stop()

    def set_throttle(self, value: float) -> None:
        with self.lock:
            self.engine.throttle = max(0.0, min(1.0, value))

    def set_fault(self, name: str, on: bool) -> None:
        if name not in FAULTS:
            return
        with self.lock:
            if on:
                self.engine.faults.add(name)
            else:
                self.engine.faults.discard(name)

    def repair(self) -> None:
        with self.lock:
            self.engine.repair()
            self.protection_shutdown = False
            for monitor in self.monitors:
                monitor.reset()

    def status(self) -> dict:
        """What the simulator panel shows. This does not come from the CAN bus."""
        with self.lock:
            return {
                "state": self.engine.state,
                "throttle": self.engine.throttle,
                "faults": sorted(self.engine.faults),
                "derate": self.engine.torque_limit < 1.0,
                "protection_shutdown": self.protection_shutdown,
            }

    # ------------------------------------------------------------ main loop

    def run(self) -> None:
        next_tick = time.monotonic()
        while True:
            with self.lock:
                self.step(TICK)
            next_tick += TICK
            time.sleep(max(0.0, next_tick - time.monotonic()))

    def step(self, dt: float) -> None:
        self.engine.step(dt)
        sensors = self.read_sensors()
        self.run_diagnostics(sensors, dt)
        self.broadcast(sensors)
        self.ticks += 1

    def read_sensors(self) -> dict:
        engine = self.engine
        sensors = {
            "rpm": max(0.0, engine.rpm),
            "pedal": engine.throttle * 100,
            "load": engine.load * 100,
            "demand_torque": engine.throttle * 100,
            "actual_torque": engine.load * 100,
            "coolant_temp": engine.coolant_temp,
            "coolant_level": engine.coolant_level,
            "oil_pressure": max(0.0, engine.oil_pressure),
            "boost": engine.boost,
            "intake_temp": engine.intake_temp,
            "air_filter_dp": engine.air_filter_dp,
            "exhaust_temp": engine.exhaust_temp,
            "fuel_rate": engine.fuel_rate,
            "battery": engine.battery,
        }
        if "coolant_sensor_fault" in engine.faults:
            sensors["coolant_temp"] = j1939.ERROR  # a broken wire gives no usable reading
        return sensors

    def run_diagnostics(self, sensors: dict, dt: float) -> None:
        # After a protection shutdown the faults stay frozen until someone restarts
        # the engine, so the reason for the shutdown is not lost.
        if not self.protection_shutdown:
            for monitor in self.monitors:
                monitor.update(sensors, dt)

        active = [monitor for monitor in self.monitors if monitor.active]
        self.engine.torque_limit = DERATE_LIMIT if any(m.derate for m in active) else 1.0

        must_stop = any(m.shutdown_after is not None and m.active_for >= m.shutdown_after for m in active)
        if must_stop and self.engine.state == "running":
            self.engine.stop()
            self.protection_shutdown = True

        if self.engine.throttle < 0.02:
            sensors["torque_mode"] = 0   # low idle governor
        elif self.engine.torque_limit < self.engine.throttle:
            sensors["torque_mode"] = 8   # torque limiting: the engine is derated
        else:
            sensors["torque_mode"] = 1   # following the accelerator pedal

    # ------------------------------------------------------------ CAN output

    def send(self, priority: int, pgn: int, data: bytes) -> None:
        can_id = j1939.build_id(priority, pgn, j1939.ENGINE_ADDRESS)
        self.bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=True))

    def is_due(self, period: float) -> bool:
        """True on the ticks where a message with this period should go out."""
        return self.ticks % round(period / TICK) == 0

    def broadcast(self, sensors: dict) -> None:
        for message in j1939.MESSAGES.values():
            if self.is_due(message.period):
                self.send(message.priority, message.pgn, j1939.encode(message, sensors))

        if self.is_due(DM1_PERIOD):
            self.send_dm1()
        elif self.packet_queue:
            # One packet per tick keeps them 50 ms apart, the minimum gap J1939 asks for.
            pgn, data = self.packet_queue.pop(0)
            self.send(7, pgn, data)

    def send_dm1(self) -> None:
        active = [monitor for monitor in self.monitors if monitor.active]
        lamps = j1939.Lamps(
            red_stop=any(m.lamp == "red" for m in active),
            amber_warning=any(m.lamp == "amber" for m in active),
        )
        payload = j1939.encode_dm1(lamps, [j1939.Dtc(m.spn, m.fmi, m.count) for m in active])

        if len(payload) <= 8:
            self.send(6, j1939.DM1_PGN, payload)
        else:
            # Too long for one frame: send the announcement now and queue the packets.
            announce, *packets = j1939.split_bam(j1939.DM1_PGN, payload)
            self.send(7, *announce)
            self.packet_queue = packets
