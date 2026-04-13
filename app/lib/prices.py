from datetime import datetime, timezone


class Prices:
    """Helper for price calculations over a list of rate slots."""

    def __init__(self, slots: list[dict]) -> None:
        self._slots = sorted(slots, key=lambda s: s["valid_from"])

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
        cutoff = from_dt.timestamp() + n * 3600
        slots = [
            s for s in self._slots
            if datetime.fromisoformat(s["valid_from"].replace("Z", "+00:00")).timestamp() >= from_dt.timestamp()
            and datetime.fromisoformat(s["valid_from"].replace("Z", "+00:00")).timestamp() < cutoff
        ]
        return Prices(slots)

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
