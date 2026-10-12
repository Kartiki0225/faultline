"""Tests for the whole chain: engine -> ECU -> CAN frames -> gateway.

No threads and no waiting: a fake bus hands every frame straight to the gateway,
and the ECU is stepped by hand, so a minute of engine time runs in milliseconds.
"""

import pytest

import diagnostics
from ecu import TICK, Ecu
from gateway import Gateway, explain


class DirectBus:
    """Stands in for the CAN bus: whatever the ECU sends, the gateway receives."""

    def __init__(self) -> None:
        self.gateway = None
        self.clock = 0.0

    def send(self, message) -> None:
        self.gateway.handle(message.arbitration_id, bytes(message.data), self.clock)


class Bench:
    """An engine on a test bench with a service tool plugged in."""

    def __init__(self, throttle: float = 0.5) -> None:
        self.bus = DirectBus()
        self.ecu = Ecu(self.bus)
        self.gateway = Gateway(bus=None)
        self.bus.gateway = self.gateway
        self.ecu.engine.start()
        self.ecu.engine.throttle = throttle

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds / TICK)):
            self.ecu.step(TICK)
            self.bus.clock += TICK

    def run_until(self, condition, limit: float = 120) -> float:
        """Run until condition() is true. Returns how long it took, fails if it never happens."""
        start = self.bus.clock
        while not condition():
            if self.bus.clock - start > limit:
                pytest.fail("the expected condition never happened")
            self.run(TICK)
        return self.bus.clock - start

    def codes(self) -> set:
        return {(dtc.spn, dtc.fmi) for dtc in self.gateway.dtcs}


@pytest.fixture
def bench():
    bench = Bench()
    bench.run(15)  # let the engine settle
    return bench


def test_healthy_engine_reports_sensible_values_and_no_faults(bench):
    signals = bench.gateway.signals
    assert 1350 < signals["rpm"] < 1450
    assert 80 < signals["coolant_temp"] < 95
    assert 250 < signals["oil_pressure"] < 330
    assert bench.codes() == set()
    assert not bench.gateway.lamps.amber_warning and not bench.gateway.lamps.red_stop


def test_more_throttle_means_more_speed_boost_and_fuel():
    idle, full = Bench(throttle=0.0), Bench(throttle=1.0)
    idle.run(15)
    full.run(15)
    for key in ("rpm", "boost", "fuel_rate", "exhaust_temp"):
        assert full.gateway.signals[key] > idle.gateway.signals[key]


def test_messages_arrive_at_their_j1939_rates(bench):
    periods = {row["name"]: row["period_ms"] for row in bench.gateway.snapshot()["ids"]}
    assert periods["EEC1"] == 100
    assert periods["EFL/P1"] == 500
    assert periods["ET1"] == 1000
    assert periods["DM1"] == 1000


def test_a_short_spike_does_not_set_a_fault(bench):
    bench.ecu.engine.coolant_temp = 106  # above the 104 limit, but only for a moment
    bench.run(1.0)
    bench.ecu.engine.coolant_temp = 88
    bench.run(3.0)
    assert bench.codes() == set()


def test_oil_pump_failure_warns_then_derates_then_shuts_down(bench):
    bench.ecu.set_fault("oil_pressure_loss", True)

    bench.run_until(lambda: (100, 18) in bench.codes())
    assert bench.gateway.lamps.amber_warning
    assert bench.ecu.engine.state == "running"

    bench.run_until(lambda: (100, 1) in bench.codes())
    assert bench.gateway.lamps.red_stop
    assert bench.gateway.signals["torque_mode"] == 8  # torque limiting

    bench.run_until(lambda: bench.ecu.engine.state == "off", limit=10)
    assert bench.ecu.protection_shutdown

    bench.run(10)
    assert (100, 1) in bench.codes(), "the fault that stopped the engine must stay visible"


def test_coolant_leak_is_predicted_before_the_ecu_reports_overheating(bench):
    bench.ecu.set_fault("coolant_leak", True)

    bench.run_until(lambda: (111, 18) in bench.codes())
    bench.run_until(lambda: bench.gateway.snapshot()["warnings"])
    assert (110, 16) not in bench.codes(), "the early warning must come first"

    seconds_until_fault = bench.run_until(lambda: (110, 16) in bench.codes())
    assert seconds_until_fault > 3


def test_several_faults_arrive_through_the_transport_protocol(bench):
    bench.ecu.set_fault("coolant_leak", True)
    bench.run_until(lambda: len(bench.codes()) >= 2)

    names = {row["name"] for row in bench.gateway.snapshot()["ids"]}
    assert {"TP.CM", "TP.DT"} <= names


def test_broken_sensor_wire_is_reported_as_an_electrical_fault(bench):
    bench.ecu.set_fault("coolant_sensor_fault", True)
    bench.run_until(lambda: (110, 3) in bench.codes())
    assert bench.gateway.signals["coolant_temp"] == "error"


def test_repair_clears_faults_and_the_engine_can_restart(bench):
    bench.ecu.set_fault("oil_pressure_loss", True)
    bench.run_until(lambda: bench.ecu.engine.state == "off")

    bench.ecu.repair()
    bench.ecu.start_engine()
    bench.run(10)

    assert bench.ecu.engine.state == "running"
    assert bench.codes() == set()
    kinds = [event["kind"] for event in bench.gateway.events]
    assert "set" in kinds and "cleared" in kinds


def test_every_fault_the_ecu_can_raise_has_a_description(bench):
    for monitor in bench.ecu.monitors:
        assert (monitor.spn, monitor.fmi) in diagnostics.FAULT_GUIDE
        assert monitor.fmi in diagnostics.FMI_TEXT


def test_frame_can_be_explained_step_by_step():
    result = explain(0x0CF00400, bytes.fromhex("F1A5A0E02EFFFFFF"))
    assert (result["priority"], result["pgn"], result["source"], result["name"]) == (3, 61444, 0, "EEC1")
    speed = next(signal for signal in result["signals"] if signal["spn"] == 190)
    assert (speed["bytes"], speed["raw"], speed["value"]) == ("4-5", 12000, 1500.0)


def test_slope_of_a_straight_line():
    samples = [(t, 90 + 0.5 * t) for t in range(10)]
    assert diagnostics.slope(samples) == pytest.approx(0.5)


def test_a_recorded_log_decodes_to_the_same_story(bench):
    from decode_log import summarise

    bench.ecu.set_fault("oil_pressure_loss", True)
    bench.run_until(lambda: bench.ecu.engine.state == "off")

    summary = summarise(bench.gateway.log_text())

    assert summary["frames"] == bench.gateway.frame_number
    assert [(e["kind"], e["spn"], e["fmi"]) for e in summary["events"]] == [("set", 100, 18), ("set", 100, 1)]
    lowest_oil, highest_oil = summary["ranges"]["oil_pressure"]
    assert lowest_oil < 73 and highest_oil > 250
