"""A simple model of a heavy-duty diesel engine.

This is the "physical" engine: it has no idea about CAN or fault codes.
Every step() moves the engine forward by a small amount of time.
The numbers are rough but behave like the real thing: more throttle means more
speed, boost, heat and fuel, and a failure slowly pushes a value out of range.

Heat is sped up on purpose, so a fault develops in seconds instead of minutes.
"""

import random
from dataclasses import dataclass, field

IDLE_RPM = 700
MAX_RPM = 2100
AMBIENT_TEMP = 30.0

# The failures that can be switched on from the dashboard.
FAULTS = {
    "coolant_leak": "Coolant leak",
    "oil_pressure_loss": "Oil pump failure",
    "air_filter_clog": "Clogged air filter",
    "alternator_failure": "Alternator failure",
    "coolant_sensor_fault": "Coolant sensor wire broken",
}


def approach(current: float, target: float, seconds: float, dt: float) -> float:
    """Move current towards target. A larger 'seconds' means a slower response."""
    return current + (target - current) * min(1.0, dt / seconds)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class Engine:
    state: str = "off"          # off, cranking or running
    throttle: float = 0.0       # accelerator pedal, 0 to 1
    torque_limit: float = 1.0   # the ECU lowers this to protect the engine (derate)
    faults: set = field(default_factory=set)

    rpm: float = 0.0
    load: float = 0.0           # 0 to 1
    coolant_temp: float = 78.0
    coolant_level: float = 100.0
    oil_pressure: float = 0.0
    boost: float = 0.0
    intake_temp: float = 35.0
    air_filter_dp: float = 0.0
    exhaust_temp: float = 60.0
    fuel_rate: float = 0.0
    battery: float = 25.2

    # How far each failure has progressed, 0 (healthy) to 1 (fully failed).
    oil_pump_wear: float = 0.0
    filter_clog: float = 0.0
    cranking_time: float = 0.0

    def start(self) -> None:
        if self.state == "off":
            self.state = "cranking"
            self.cranking_time = 0.0

    def stop(self) -> None:
        self.state = "off"

    def repair(self) -> None:
        """Fix everything that was broken."""
        self.faults.clear()
        self.coolant_level = 100.0
        self.oil_pump_wear = 0.0
        self.filter_clog = 0.0

    def step(self, dt: float) -> None:
        self._progress_failures(dt)

        if self.state == "cranking":
            self.cranking_time += dt
            if self.cranking_time > 0.9:
                self.state = "running"

        running = self.state == "running"
        pedal = min(self.throttle, self.torque_limit) if running else 0.0

        # Speed and load follow the pedal.
        if running:
            target_rpm = IDLE_RPM + (MAX_RPM - IDLE_RPM) * pedal
        else:
            target_rpm = 220 if self.state == "cranking" else 0
        self.rpm = approach(self.rpm, target_rpm, 1.2 if running else 0.5, dt)
        speed = self.rpm / MAX_RPM  # 0 to 1
        self.load = approach(self.load, (0.12 + 0.85 * pedal) if running else 0.0, 0.6, dt)

        # Air: the turbo builds boost with load. A clogged filter starves the engine
        # of air, so boost falls and the exhaust runs hotter.
        airflow = self.load * (0.3 + 0.7 * speed)
        self.boost = approach(self.boost, 190 * airflow * (1 - 0.35 * self.filter_clog), 0.8, dt)
        self.air_filter_dp = (0.4 * speed + 1.6 * airflow + 9.5 * self.filter_clog * (0.55 + 0.45 * airflow)) if running else 0.0
        self.intake_temp = approach(self.intake_temp, 35 + 25 * self.load + 10 * self.filter_clog, 4.0, dt)
        exhaust_target = (120 + 430 * self.load * (0.6 + 0.4 * speed) + 130 * self.filter_clog) if running else AMBIENT_TEMP
        self.exhaust_temp = approach(self.exhaust_temp, exhaust_target, 2.5 if running else 20.0, dt)
        self.fuel_rate = (2.5 + 60 * self.load * speed) if running else 0.0

        # Oil pressure comes from a pump driven by the engine, so it rises with speed.
        healthy_pressure = (110 + 250 * speed ** 0.8) if self.rpm > 150 else 0.0
        self.oil_pressure = approach(self.oil_pressure, healthy_pressure * (1 - 0.85 * self.oil_pump_wear), 0.4, dt)

        # Coolant: burning fuel makes heat, the radiator removes it once the
        # thermostat opens (82 to 92 °C). Less coolant means less cooling.
        heat_in = (0.25 + 0.06 * self.fuel_rate) if running else 0.0
        thermostat = clamp((self.coolant_temp - 82) / 10, 0, 1)
        cooling = clamp((self.coolant_level - 35) / 50, 0.08, 1)
        heat_out = thermostat * cooling * 0.11 * (self.coolant_temp - AMBIENT_TEMP) * (0.5 + 0.5 * speed)
        heat_out += 0.004 * (self.coolant_temp - AMBIENT_TEMP)
        self.coolant_temp += (heat_in - heat_out) * dt

        # Electrical: the alternator charges the 24 V batteries while the engine runs.
        if self.state == "cranking":
            battery_target, seconds = 21.5, 0.2
        elif running and "alternator_failure" not in self.faults:
            battery_target, seconds = 28.2, 2.0
        elif running:
            battery_target, seconds = 22.8, 8.0
        else:
            battery_target, seconds = 25.2, 6.0
        self.battery = approach(self.battery, battery_target, seconds, dt)

        self._add_sensor_noise()

    def _progress_failures(self, dt: float) -> None:
        if "coolant_leak" in self.faults:
            self.coolant_level = max(20.0, self.coolant_level - 4.0 * dt)
        if "oil_pressure_loss" in self.faults:
            self.oil_pump_wear = min(1.0, self.oil_pump_wear + 0.07 * dt)
        if "air_filter_clog" in self.faults:
            self.filter_clog = min(1.0, self.filter_clog + 0.12 * dt)

    def _add_sensor_noise(self) -> None:
        """Real sensors never read perfectly steady."""
        if self.state == "running":
            self.rpm += random.uniform(-3, 3)
            self.oil_pressure += random.uniform(-1.5, 1.5)
            self.boost = max(0.0, self.boost + random.uniform(-0.6, 0.6))
