"""Suggests the flow setpoint raise of heating circuit 1 (register 30684).

The raise lifts the heat generator's setpoint above the flow setpoint the
circuit requests, to make up for water that cools down or mixes on its way to
the circuit, e.g. in a buffer. This module only works out a suggestion; it
never writes to the controller.

While the heat pump heats the circuit, it is in one of these states, judged
over the last minutes once the start-up phase is over:

- Surplus: the heat generator runs clearly above its setpoint and keeps
  rising, and the circuit reaches its setpoint. Even at its lowest output the
  heat pump delivers more than the circuit takes, the buffer charges until
  the heat pump switches off. A raise only lifts the temperature level, so
  the suggestion is the lowest raise.
- Balance: the heat generator holds its setpoint. Now the raise decides what
  reaches the circuit. The temperature the circuit could get with the mixing
  valve fully open (its flow temperature if the valve is open, otherwise the
  buffer temperature plus the usual difference between the two) lies as far
  below the heat generator setpoint as needed for the suggested raise:
  raise = heat generator setpoint - available temperature (+ an allowance for
  the setpoint being rounded down to 0.5 K).
- Anything else (starting up, between the two, not heating): no new
  measurement.

The start-up phase ends once the heat pump has come down to the lowest output
seen during a surplus, at the latest after a fixed time.

Each measurement updates a table of the raise per outdoor temperature range,
kept across restarts. Without a current measurement, the suggestion comes from
that table, so it is already right when the heat pump starts. Before the
table holds any value, there is no suggestion.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any

# Heat pump status code "heating mode" (HEAT_PUMP_STATUS_MAP).
STATUS_HEATING = 1

# Measurements start once the start-up phase with higher modulation is over:
# when the modulation is down to the lowest value seen during a surplus (plus
# MODULATION_MARGIN), but not before MIN_SETTLE_TIME, which skips the odd
# values right after switching on; without that value, after SETTLE_TIME.
MIN_SETTLE_TIME = timedelta(minutes=5)
SETTLE_TIME = timedelta(minutes=20)
MODULATION_MARGIN = 1.0
# Values are judged over this window. A surplus is clear sooner than a
# balance, so the window must be filled to SURPLUS_MIN_WINDOW before a
# surplus is decided and to BALANCE_MIN_WINDOW before anything else.
WINDOW = timedelta(minutes=15)
SURPLUS_MIN_WINDOW = timedelta(minutes=5)
BALANCE_MIN_WINDOW = timedelta(minutes=10)

# Heat generator actual minus setpoint, in K.
SURPLUS_OFFSET = 2.5
SURPLUS_RISE = 0.3  # minimum rise of the actual temperature over the window
BALANCE_OFFSET = 1.5
BALANCE_MAX_CHANGE = 0.5  # maximum change of the actual temperature over the window

# The mixing valve counts as fully open when the circuit's flow temperature
# is at most this far above the buffer temperature (the buffer sensor reads
# somewhat lower than the flow sensor), the circuit is below its setpoint and
# the controller keeps opening the valve.
MIXER_OPEN_MARGIN = 1.2
MIXER_OPEN_MIN_DEFICIT = 0.2
# Flow temperature minus buffer temperature while the valve is open, used
# before it has been measured.
DEFAULT_BUFFER_OFFSET = 0.6

# The heat generator setpoint is rounded down to 0.5 K, i.e. by 0.25 K on
# average.
ROUNDING_ALLOWANCE = 0.25
# The circuit sets the heat generator setpoint while that is its flow
# setpoint plus raise, rounded down to 0.5 K, within this tolerance. Only then
# does the heat generator's behaviour say something about this circuit.
DEMAND_TOLERANCE = 0.3

MIN_RAISE = 0.0
MAX_RAISE = 5.0

# Outdoor temperature ranges of the table, in K. The outdoor temperature is
# the controller's own sensor, which its heating curve uses as well. Mounted
# on a wall, it can vary much less than the air temperature, so the ranges
# are kept narrow.
BIN_WIDTH = 1.0
# Weight of a new measurement in the table and in the buffer offset, used at
# most every LEARN_INTERVAL so that a long phase doesn't push out the rest.
LEARN_RATE = 0.1
LEARN_INTERVAL = timedelta(minutes=5)

MODE_IDLE = "idle"
MODE_STARTING = "starting"
MODE_SURPLUS = "surplus"
MODE_BALANCE = "balance"
MODE_TRANSITION = "transition"
MODE_UNCLEAR = "unclear"
# The heat generator setpoint is set by another demand (another heating
# circuit, or an offset on the heat generator), not by this circuit.
MODE_OTHER_DEMAND = "other_demand"


@dataclass
class Sample:
    """One set of values from a poll."""

    time: datetime
    actual: float  # heat generator actual temperature
    setpoint: float  # heat generator setpoint
    flow: float  # circuit flow temperature
    flow_setpoint: float  # circuit flow setpoint
    buffer: float
    mixer: float  # mixing valve signal, > 0 = opening


@dataclass
class Suggestion:
    """The suggested raise and why."""

    value: float | None
    mode: str
    attributes: dict[str, Any] = field(default_factory=dict)


class FlowSetpointRaiseAdvisor:
    """Works out the suggested raise from the polled values."""

    def __init__(self, stored: dict[str, Any] | None = None) -> None:
        stored = stored or {}
        self.table: dict[int, float] = {
            int(key): float(value) for key, value in stored.get("table", {}).items()
        }
        self.buffer_offset: float = float(
            stored.get("buffer_offset", DEFAULT_BUFFER_OFFSET)
        )
        lowest = stored.get("lowest_modulation")
        self.lowest_modulation: float | None = None if lowest is None else float(lowest)
        self._samples: deque[Sample] = deque()
        self._run_start: datetime | None = None
        self._settled = False
        self._last_learned: datetime | None = None

    def to_store(self) -> dict[str, Any]:
        return {
            "table": {str(key): round(value, 2) for key, value in self.table.items()},
            "buffer_offset": round(self.buffer_offset, 2),
            "lowest_modulation": self.lowest_modulation,
        }

    def update(
        self,
        now: datetime,
        status: float | None,
        outdoor: float | None,
        current_raise: float | None,
        modulation: float | None,
        sample: Sample | None,
    ) -> Suggestion:
        """Processes one poll and returns the suggestion."""
        if status is None or int(status) != STATUS_HEATING or sample is None:
            self._run_start = None
            self._samples.clear()
            return self._from_table(MODE_IDLE, outdoor, current_raise)

        if self._run_start is None:
            self._run_start = now
            self._settled = False
        running = now - self._run_start
        if not self._settled:
            self._settled = running >= SETTLE_TIME or (
                running >= MIN_SETTLE_TIME and self._at_lowest_output(modulation)
            )
            if not self._settled:
                return self._from_table(
                    MODE_STARTING, outdoor, current_raise, running=running
                )

        self._samples.append(sample)
        while self._samples and now - self._samples[0].time > WINDOW:
            self._samples.popleft()
        span = self._samples[-1].time - self._samples[0].time
        if span < SURPLUS_MIN_WINDOW:
            return self._from_table(
                MODE_STARTING, outdoor, current_raise, running=running
            )

        samples = list(self._samples)
        # Learned only while this circuit sets the heat generator setpoint.
        if current_raise is not None:
            demand_gap = fmean(
                s.setpoint - _rounded_down(s.flow_setpoint + current_raise)
                for s in samples
            )
            if abs(demand_gap) > DEMAND_TOLERANCE:
                return self._from_table(
                    MODE_OTHER_DEMAND,
                    outdoor,
                    current_raise,
                    demand_gap=round(demand_gap, 2),
                )
        offset = fmean(s.actual - s.setpoint for s in samples)
        change = samples[-1].actual - samples[0].actual
        deficit = fmean(s.flow_setpoint - s.flow for s in samples)
        flow_over_buffer = fmean(s.flow - s.buffer for s in samples)
        mixer = fmean(s.mixer for s in samples)
        details = {
            "running_minutes": round(running.total_seconds() / 60),
            "heat_generator_offset": round(offset, 2),
            "heat_generator_change": round(change, 2),
            "deficit": round(deficit, 2),
            "flow_over_buffer": round(flow_over_buffer, 2),
            "mixer_signal": round(mixer, 1),
        }

        # Surplus only counts while the circuit reaches its setpoint: with a
        # large loss on the way, the circuit can still fall short although
        # the heat generator runs above its setpoint.
        if (
            offset >= SURPLUS_OFFSET
            and change >= SURPLUS_RISE
            and deficit < MIXER_OPEN_MIN_DEFICIT
        ):
            if modulation is not None and modulation > 0:
                self.lowest_modulation = (
                    modulation
                    if self.lowest_modulation is None
                    else min(self.lowest_modulation, modulation)
                )
            self._learn(now, outdoor, MIN_RAISE)
            return self._result(MIN_RAISE, MODE_SURPLUS, outdoor, current_raise, details)

        if span < BALANCE_MIN_WINDOW:
            return self._from_table(MODE_STARTING, outdoor, current_raise, **details)

        if offset > BALANCE_OFFSET or abs(change) > BALANCE_MAX_CHANGE:
            return self._from_table(
                MODE_TRANSITION, outdoor, current_raise, **details
            )

        setpoint = fmean(s.setpoint for s in samples)
        mixer_open = (
            flow_over_buffer <= MIXER_OPEN_MARGIN
            and deficit >= MIXER_OPEN_MIN_DEFICIT
            and mixer > 0
        )
        details["mixer_open"] = mixer_open
        if mixer_open:
            if self._may_learn(now):
                self.buffer_offset += LEARN_RATE * (
                    flow_over_buffer - self.buffer_offset
                )
            available = fmean(s.flow for s in samples)
        elif deficit < MIXER_OPEN_MIN_DEFICIT:
            # The valve throttles: with it open, the circuit would get about
            # the buffer temperature.
            available = fmean(s.buffer for s in samples) + self.buffer_offset
        else:
            # Below the setpoint without the valve being clearly open.
            return self._from_table(MODE_UNCLEAR, outdoor, current_raise, **details)

        needed = setpoint - available + ROUNDING_ALLOWANCE
        details["available_temperature"] = round(available, 2)
        value = self._clamp(needed)
        self._learn(now, outdoor, value)
        return self._result(value, MODE_BALANCE, outdoor, current_raise, details)

    def _result(
        self,
        value: float,
        mode: str,
        outdoor: float | None,
        current_raise: float | None,
        details: dict[str, Any],
    ) -> Suggestion:
        return Suggestion(
            round(value, 1), mode, self._attributes(outdoor, current_raise, details)
        )

    def _from_table(
        self,
        mode: str,
        outdoor: float | None,
        current_raise: float | None,
        running: timedelta | None = None,
        **details: Any,
    ) -> Suggestion:
        if running is not None:
            details["running_minutes"] = round(running.total_seconds() / 60)
        value = self._table_value(outdoor)
        return Suggestion(
            None if value is None else round(self._clamp(value), 1),
            mode,
            self._attributes(outdoor, current_raise, details),
        )

    def _attributes(
        self, outdoor: float | None, current_raise: float | None, details: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            **details,
            "current_raise": current_raise,
            "outdoor_range": self._range_label(outdoor),
            "buffer_offset": round(self.buffer_offset, 2),
            "lowest_modulation": self.lowest_modulation,
            "table": {
                self._range_label(key * BIN_WIDTH): round(value, 1)
                for key, value in sorted(self.table.items())
            },
        }

    def _at_lowest_output(self, modulation: float | None) -> bool:
        return (
            modulation is not None
            and self.lowest_modulation is not None
            and 0 < modulation <= self.lowest_modulation + MODULATION_MARGIN
        )

    def _may_learn(self, now: datetime) -> bool:
        return self._last_learned is None or now - self._last_learned >= LEARN_INTERVAL

    def _learn(self, now: datetime, outdoor: float | None, value: float) -> None:
        if outdoor is None or not self._may_learn(now):
            return
        self._last_learned = now
        key = self._bin(outdoor)
        old = self.table.get(key)
        self.table[key] = value if old is None else old + LEARN_RATE * (value - old)

    def _table_value(self, outdoor: float | None) -> float | None:
        if outdoor is None or not self.table:
            return None
        key = self._bin(outdoor)
        if key in self.table:
            return self.table[key]
        lower = [k for k in self.table if k < key]
        upper = [k for k in self.table if k > key]
        if lower and upper:
            low, high = max(lower), min(upper)
            share = (key - low) / (high - low)
            return self.table[low] + share * (self.table[high] - self.table[low])
        return self.table[max(lower) if lower else min(upper)]

    @staticmethod
    def _bin(outdoor: float) -> int:
        return int(outdoor // BIN_WIDTH)

    @staticmethod
    def _range_label(outdoor: float | None) -> str | None:
        if outdoor is None:
            return None
        low = (outdoor // BIN_WIDTH) * BIN_WIDTH
        return f"{low:g}..{low + BIN_WIDTH:g}"

    @staticmethod
    def _clamp(value: float) -> float:
        return min(max(value, MIN_RAISE), MAX_RAISE)


# Automatic adjustment (AutoRaise): the raise goes down right away when the
# heat pump has a surplus or clearly needs less, and up only in small steps,
# once the heat pump has been running for a while, so a single long run
# doesn't push it up too far.
AUTO_DOWN_MARGIN = 0.3  # a balance suggestion this far below lowers the raise
AUTO_UP_MARGIN = 0.1  # a balance suggestion this far above raises it
AUTO_UP_STEP = 0.2
AUTO_UP_INTERVAL = timedelta(minutes=10)
AUTO_MIN_RUN = timedelta(minutes=60)


class AutoRaise:
    """Decides which raise to write from the suggestion."""

    def __init__(self) -> None:
        self._last_raised: datetime | None = None

    def next_value(
        self,
        now: datetime,
        suggestion: Suggestion,
        current: float | None,
        lower: float,
        upper: float,
    ) -> float | None:
        """The raise to write now, or None to leave it as it is."""
        if current is None:
            return None
        current = round(current, 1)
        if suggestion.mode == MODE_SURPLUS:
            target = lower
        elif suggestion.mode == MODE_BALANCE and suggestion.value is not None:
            target = min(max(suggestion.value, lower), upper)
        else:
            # No measurement: only keep the raise within the bounds.
            target = min(max(current, lower), upper)
            return self._changed(target, current)

        if target < current and (
            suggestion.mode == MODE_SURPLUS or current - target >= AUTO_DOWN_MARGIN
        ):
            return self._changed(target, current)
        if suggestion.mode == MODE_BALANCE and target - current >= AUTO_UP_MARGIN:
            running = suggestion.attributes.get("running_minutes", 0)
            if running * 60 < AUTO_MIN_RUN.total_seconds():
                return None
            if (
                self._last_raised is not None
                and now - self._last_raised < AUTO_UP_INTERVAL
            ):
                return None
            self._last_raised = now
            return self._changed(min(target, current + AUTO_UP_STEP), current)
        return None

    @staticmethod
    def _changed(value: float, current: float) -> float | None:
        value = round(value, 1)
        return None if value == current else value


def _rounded_down(value: float) -> float:
    """The value rounded down to 0.5 K, like the heat generator setpoint."""
    return math.floor(value * 2 + 1e-6) / 2
