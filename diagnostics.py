"""What the service tool knows: how to explain a fault code to a human,
and how to spot a problem before it becomes a fault.
"""

import j1939

# FMI (Failure Mode Identifier): the kind of failure. These meanings are fixed by J1939.
FMI_TEXT = {
    0: "Above normal range, most severe",
    1: "Below normal range, most severe",
    2: "Data erratic or incorrect",
    3: "Voltage above normal, or shorted to high source",
    4: "Voltage below normal, or shorted to low source",
    5: "Current below normal, or open circuit",
    15: "Above normal range, least severe",
    16: "Above normal range, moderately severe",
    17: "Below normal range, least severe",
    18: "Below normal range, moderately severe",
    31: "Condition exists",
}

# (SPN, FMI) -> what it means and what to do about it.
FAULT_GUIDE = {
    (110, 16): (
        "Coolant temperature high",
        "The engine is running hotter than it should.",
        "Reduce load. Check coolant level, radiator and fan.",
    ),
    (110, 0): (
        "Coolant temperature critical",
        "The engine is overheating. Torque is limited and the engine will shut down to protect itself.",
        "Stop the engine safely. Do not open the radiator cap while hot. Find the cause of coolant loss.",
    ),
    (110, 3): (
        "Coolant temperature sensor circuit",
        "The sensor signal is out of its electrical range, usually a broken wire or unplugged connector.",
        "Inspect the sensor connector and wiring. The engine cannot detect overheating until this is fixed.",
    ),
    (111, 18): (
        "Coolant level low",
        "The coolant level in the expansion tank has dropped.",
        "Check hoses, radiator and water pump for leaks, then top up coolant.",
    ),
    (100, 18): (
        "Oil pressure low",
        "Oil pressure is below normal for this engine speed.",
        "Check the oil level. If the level is correct, inspect the oil pump and filter.",
    ),
    (100, 1): (
        "Oil pressure critical",
        "Oil pressure is too low to protect the bearings. The engine will shut down.",
        "Stop the engine immediately. Running without oil pressure destroys the engine.",
    ),
    (107, 16): (
        "Air filter restricted",
        "The pressure drop across the air filter is too high, so the engine is short of air.",
        "Replace the air filter element.",
    ),
    (168, 18): (
        "Battery voltage low",
        "The batteries are not being charged while the engine is running.",
        "Check the alternator, its belt and the battery cables.",
    ),
}


def spn_name(spn: int) -> str:
    for signal in j1939.SIGNALS.values():
        if signal.spn == spn:
            return signal.name
    return f"SPN {spn}"


def describe(dtc: j1939.Dtc, red_stop: bool) -> dict:
    """Everything the dashboard shows for one fault code."""
    title, meaning, action = FAULT_GUIDE.get(
        (dtc.spn, dtc.fmi),
        (spn_name(dtc.spn), "No description for this code.", "Refer to the service manual."),
    )
    return {
        "spn": dtc.spn,
        "fmi": dtc.fmi,
        "count": dtc.count,
        "parameter": spn_name(dtc.spn),
        "fmi_text": FMI_TEXT.get(dtc.fmi, "Unknown failure mode"),
        "title": title,
        "meaning": meaning,
        "action": action,
        "severe": red_stop and dtc.fmi in (0, 1),
    }


def slope(samples: list[tuple[float, float]]) -> float:
    """Least-squares slope of (time, value) samples: how fast the value changes per second."""
    n = len(samples)
    mean_t = sum(t for t, _ in samples) / n
    mean_v = sum(v for _, v in samples) / n
    spread = sum((t - mean_t) ** 2 for t, _ in samples)
    if spread == 0:
        return 0.0
    return sum((t - mean_t) * (v - mean_v) for t, v in samples) / spread


def early_warning(samples: list[tuple[float, float]], watch_from: float, limit: float) -> dict | None:
    """Predict when a rising value will cross its limit.

    The ECU only reports a fault once the limit is crossed. By fitting a line
    through the last few seconds of readings, the tool can warn earlier.
    """
    if len(samples) < 5:
        return None
    current = samples[-1][1]
    rate = slope(samples)
    if current < watch_from or current >= limit or rate < 0.15:
        return None
    seconds_left = (limit - current) / rate
    if seconds_left > 90:
        return None
    return {"rate": round(rate, 2), "seconds_left": round(seconds_left), "limit": limit}
