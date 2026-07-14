from datetime import datetime, timezone


class Prices:
    """Helper for price calculations over a list of rate slots."""

    def __init__(self, slots: list[dict]) -> None:
        self._slots = sorted(slots, key=lambda s: s["valid_from"])

    def get_slots(self) -> list[dict]:
        """Raw rate slots (JSON-serializable) — used to pass prices through
        Homey flow-trigger state, which must survive JSON serialization."""
        return self._slots

    def get_average(self) -> float:
        if not self._slots:
            return 0.0
        return sum(s["value_inc_vat"] for s in self._slots) / len(self._slots)

    def get_lowest(self) -> float:
        if not self._slots:
            return 0.0
        return min(s["value_inc_vat"] for s in self._slots)

    def get_highest(self) -> float:
        if not self._slots:
            return 0.0
        return max(s["value_inc_vat"] for s in self._slots)

    def get_at_instant(self, dt: datetime) -> float | None:
        for slot in self._slots:
            slot_from = datetime.fromisoformat(slot["valid_from"].replace("Z", "+00:00"))
            slot_to_raw = slot.get("valid_to")
            if slot_to_raw:
                slot_to = datetime.fromisoformat(slot_to_raw.replace("Z", "+00:00"))
            else:
                slot_to = None

            if slot_to is None or slot_from <= dt < slot_to:
                return slot["value_inc_vat"]
        return None

    def get_for_next_n_hours(self, from_dt: datetime, n: int) -> "Prices":
        """Slots in effect at any point during the next n hours.

        Selects slots whose [valid_from, valid_to) interval overlaps
        [from_dt, from_dt + n h) — crucially this includes the slot currently
        in effect (its valid_from is in the past, but it still covers `now`),
        and an open-ended slot (valid_to is null, e.g. a fixed price).
        """
        start = from_dt.timestamp()
        cutoff = start + n * 3600

        def _ts(value: str) -> float:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

        def overlaps(slot: dict) -> bool:
            vf = _ts(slot["valid_from"])
            vt = _ts(slot["valid_to"]) if slot.get("valid_to") else None
            return vf < cutoff and (vt is None or vt > start)

        return Prices([s for s in self._slots if overlaps(s)])

    def get_sorted_values(self) -> list[float]:
        return sorted(s["value_inc_vat"] for s in self._slots)

    def get_for_time_window(self, time_from: str, time_to: str) -> "Prices":
        """Filter slots whose valid_from time (HH:MM) is within the window."""
        from_h, from_m = map(int, time_from.split(":"))
        to_h, to_m = map(int, time_to.split(":"))
        from_minutes = from_h * 60 + from_m
        to_minutes = to_h * 60 + to_m

        def in_window(slot: dict) -> bool:
            dt = datetime.fromisoformat(slot["valid_from"].replace("Z", "+00:00"))
            slot_minutes = dt.hour * 60 + dt.minute
            if from_minutes <= to_minutes:
                return from_minutes <= slot_minutes < to_minutes
            return slot_minutes >= from_minutes or slot_minutes < to_minutes

        return Prices([s for s in self._slots if in_window(s)])

    # ------------------------------------------------------------------
    # Guarded predicates — the single source of truth for trigger/condition
    # logic. Each guards the degenerate cases so callers can't misfire:
    #   * empty window  -> never true (avg/lowest/highest have no meaning)
    #   * flat window    -> lowest/highest/among never true (no price to pick;
    #                       otherwise a fixed price is trivially both the
    #                       cheapest AND priciest and would fire both)
    # ------------------------------------------------------------------

    def is_empty(self) -> bool:
        return not self._slots

    def has_variation(self) -> bool:
        """True only when there are at least two distinct prices."""
        return len({s["value_inc_vat"] for s in self._slots}) > 1

    def is_below_average(self, current: float, pct: float = 0) -> bool:
        if self.is_empty():
            return False
        return current < self.get_average() * (1 - pct / 100)

    def is_above_average(self, current: float, pct: float = 0) -> bool:
        if self.is_empty():
            return False
        return current > self.get_average() * (1 + pct / 100)

    def is_cheapest(self, current: float) -> bool:
        return self.has_variation() and current <= self.get_lowest()

    def is_priciest(self, current: float) -> bool:
        return self.has_variation() and current >= self.get_highest()

    def is_among_cheapest(self, current: float, n: int) -> bool:
        if not self.has_variation():
            return False
        values = self.get_sorted_values()
        threshold = values[min(max(n, 1), len(values)) - 1]  # nth-lowest price
        return current <= threshold

    def is_among_priciest(self, current: float, n: int) -> bool:
        if not self.has_variation():
            return False
        values = self.get_sorted_values()
        threshold = values[len(values) - min(max(n, 1), len(values))]  # nth-highest
        return current >= threshold
