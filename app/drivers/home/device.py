import logging
import random
from datetime import datetime, timedelta, timezone

from homey.device import Device

from ...lib import resolve_server_url
from ...lib.prices import Prices
from ...lib.server_client import ServerClient

logger = logging.getLogger(__name__)

_POLL_RATES_INTERVAL = 3600       # 1 hour — full rate schedule + trigger evaluation
_JITTER_MAX = 60                  # seconds of random jitter to spread server load


class OctopusEnergyDevice(Device):

    async def on_init(self) -> None:
        server_url, source = resolve_server_url(self.homey)
        self.client = ServerClient(server_url)
        account_number = self.get_store().get("account_number")
        self.log(f"Device init: {account_number} server_url={server_url} (source={source})")

        self._electricity_prices: Prices | None = None
        self._gas_prices: Prices | None = None

        # Flow-card run listeners are app-global and only need registering once.
        # A redundant registration from another device just raises AlreadyExists,
        # which must NOT abort this device's polling below.
        try:
            self._register_flow_cards()
        except Exception as exc:
            self.log(f"Device flow-card registration skipped: {exc!r}")

        try:
            await self._poll_rates()
            self._schedule_rates()
        except Exception as exc:
            import traceback
            self.error(f"Device initial poll failed: {exc!r}\n{traceback.format_exc()}")
        self.log(f"Device initialized: {account_number}")

    def on_deleted(self) -> None:
        if hasattr(self, "_rates_timer") and self._rates_timer:
            self.homey.clear_timeout(self._rates_timer)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _schedule_rates(self) -> None:
        now = datetime.now(tz=timezone.utc)
        next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        jitter = random.randint(0, _JITTER_MAX)
        delay_ms = int((next_hour.timestamp() - now.timestamp() + jitter) * 1000)

        self._rates_timer = self.homey.set_timeout(self._rates_tick, delay_ms)
        self.log("Next rates poll in %.0fs (+%ds jitter)", delay_ms / 1000, jitter)

    async def _rates_tick(self) -> None:
        try:
            await self._poll_rates()
        except Exception as exc:
            self.error("Rates poll failed: %s", exc)
        self._schedule_rates()

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def _poll_rates(self) -> None:
        store = self.get_store()
        homey_id = store.get("homey_id")
        region = store.get("region")
        account_number = store.get("account_number")
        now = datetime.now(tz=timezone.utc)

        elec_malo = store.get("electricity_malo_id")
        gas_malo = store.get("gas_malo_id")

        if elec_malo:
            try:
                slots = await self.client.get_electricity_rates(
                    homey_id, region, account_number
                )
                self._electricity_prices = Prices(slots)
                self.log(f"Fetched {len(slots)} electricity rate slot(s)")
                await self._update_electricity_capabilities(now)
                await self._fire_electricity_triggers(now)
            except Exception as exc:
                self.error("Failed to fetch electricity rates: %s", exc)

        if gas_malo:
            try:
                slots = await self.client.get_gas_rates(
                    homey_id, region, account_number
                )
                self._gas_prices = Prices(slots)
                self.log(f"Fetched {len(slots)} gas rate slot(s)")
                await self._update_gas_capabilities(now)
                await self._fire_gas_triggers(now)
            except Exception as exc:
                self.error("Failed to fetch gas rates: %s", exc)

    # ------------------------------------------------------------------
    # Capability updates
    # ------------------------------------------------------------------

    async def _update_electricity_capabilities(self, now: datetime) -> None:
        if not self._electricity_prices:
            return
        current = self._electricity_prices.get_at_instant(now)
        if current is not None:
            await self.set_capability_value("measure_price_current.electricity", round(current, 4))
        await self.set_capability_value("measure_price_highest.electricity", round(self._electricity_prices.get_highest(), 4))
        await self.set_capability_value("measure_price_lowest.electricity", round(self._electricity_prices.get_lowest(), 4))

    async def _update_gas_capabilities(self, now: datetime) -> None:
        if not self._gas_prices:
            return
        current = self._gas_prices.get_at_instant(now)
        if current is not None:
            await self.set_capability_value("measure_price_current.gas", round(current, 4))
        await self.set_capability_value("measure_price_highest.gas", round(self._gas_prices.get_highest(), 4))
        await self.set_capability_value("measure_price_lowest.gas", round(self._gas_prices.get_lowest(), 4))

    # ------------------------------------------------------------------
    # Trigger deduplication + firing
    # ------------------------------------------------------------------

    def _current_hour_key(self, now: datetime) -> str:
        return now.strftime("%Y-%m-%dT%H:00")

    async def _fire_electricity_triggers(self, now: datetime) -> None:
        hour_key = self._current_hour_key(now)
        if self.get_store().get("last_triggered_hour_electricity") == hour_key:
            return
        await self.set_store_value("last_triggered_hour_electricity", hour_key)

        if not self._electricity_prices:
            return

        current = self._electricity_prices.get_at_instant(now)
        if current is None:
            return

        state = {"prices": self._electricity_prices, "current": current, "energy_type": "electricity"}

        await self._trigger("electricity_price_below_avg", {}, state)
        await self._trigger("electricity_price_above_avg", {}, state)
        await self._trigger("electricity_price_below_avg_today", {}, state)
        await self._trigger("electricity_price_above_avg_today", {}, state)
        await self._trigger("electricity_price_at_lowest", {}, state)
        await self._trigger("electricity_price_at_highest", {}, state)
        await self._trigger("electricity_price_among_lowest_today", {}, state)
        await self._trigger("electricity_price_among_highest_today", {}, state)

        if current == self._electricity_prices.get_lowest():
            await self._trigger("electricity_price_at_lowest_today", {}, state)
        if current == self._electricity_prices.get_highest():
            await self._trigger("electricity_price_at_highest_today", {}, state)

    async def _fire_gas_triggers(self, now: datetime) -> None:
        hour_key = self._current_hour_key(now)
        if self.get_store().get("last_triggered_hour_gas") == hour_key:
            return
        await self.set_store_value("last_triggered_hour_gas", hour_key)

        if not self._gas_prices:
            return

        current = self._gas_prices.get_at_instant(now)
        if current is None:
            return

        state = {"prices": self._gas_prices, "current": current, "energy_type": "gas"}

        await self._trigger("gas_price_below_avg", {}, state)
        await self._trigger("gas_price_above_avg", {}, state)
        await self._trigger("gas_price_below_avg_today", {}, state)
        await self._trigger("gas_price_above_avg_today", {}, state)
        await self._trigger("gas_price_at_lowest", {}, state)
        await self._trigger("gas_price_at_highest", {}, state)
        await self._trigger("gas_price_among_lowest_today", {}, state)
        await self._trigger("gas_price_among_highest_today", {}, state)

        if current == self._gas_prices.get_lowest():
            await self._trigger("gas_price_at_lowest_today", {}, state)
        if current == self._gas_prices.get_highest():
            await self._trigger("gas_price_at_highest_today", {}, state)

    async def _trigger(self, card_id: str, tokens: dict, state: dict) -> None:
        try:
            card = self.homey.flow.get_device_trigger_card(card_id)
            await card.trigger(self, tokens, state)
        except Exception as exc:
            self.error("Failed to trigger %s: %s", card_id, exc)

    # ------------------------------------------------------------------
    # Flow card registration
    # ------------------------------------------------------------------

    def _register_flow_cards(self) -> None:
        # Flow cards are app-global, so their run listeners must be registered
        # once — not once per device (that throws AlreadyExists). Listeners
        # resolve the relevant device from args["device"] / the trigger state.
        cls = type(self)
        if getattr(cls, "_flow_cards_registered", False):
            return
        cls._flow_cards_registered = True
        self._register_electricity_triggers()
        self._register_gas_triggers()
        self._register_electricity_conditions()
        self._register_gas_conditions()

    def _register_electricity_triggers(self) -> None:
        def make_price_state(state):
            return state["prices"], state["current"]

        def register_avg(card_id: str, use_today: bool) -> None:
            card = self.homey.flow.get_device_trigger_card(card_id)
            async def run(args, state):
                prices, current = make_price_state(state)
                window = prices if use_today else prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
                avg = window.get_average()
                pct = args.get("percentage", 0)
                return current < avg * (1 - pct / 100)
            card.register_run_listener(run)

        register_avg("electricity_price_below_avg", use_today=False)
        register_avg("electricity_price_below_avg_today", use_today=True)

        def register_above_avg(card_id: str, use_today: bool) -> None:
            card = self.homey.flow.get_device_trigger_card(card_id)
            async def run(args, state):
                prices, current = make_price_state(state)
                window = prices if use_today else prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
                avg = window.get_average()
                pct = args.get("percentage", 0)
                return current > avg * (1 + pct / 100)
            card.register_run_listener(run)

        register_above_avg("electricity_price_above_avg", use_today=False)
        register_above_avg("electricity_price_above_avg_today", use_today=True)

        for card_id, is_lowest in [("electricity_price_at_lowest", True), ("electricity_price_at_highest", False)]:
            card = self.homey.flow.get_device_trigger_card(card_id)
            _is_lowest = is_lowest
            async def run(args, state, _low=_is_lowest):
                prices, current = make_price_state(state)
                window = prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
                return current == (window.get_lowest() if _low else window.get_highest())
            card.register_run_listener(run)

        for card_id in ("electricity_price_at_lowest_today", "electricity_price_at_highest_today"):
            card = self.homey.flow.get_device_trigger_card(card_id)
            async def run(args, state):
                return True
            card.register_run_listener(run)

        for card_id, is_lowest in [("electricity_price_among_lowest_today", True), ("electricity_price_among_highest_today", False)]:
            card = self.homey.flow.get_device_trigger_card(card_id)
            _is_lowest = is_lowest
            async def run(args, state, _low=_is_lowest):
                prices, current = make_price_state(state)
                n = args.get("ranked_hours", 1)
                ranked = sorted(prices.get_sorted_values())
                cutoffs = ranked[:n] if _low else ranked[-n:]
                return current in cutoffs
            card.register_run_listener(run)

    def _register_gas_triggers(self) -> None:
        def make_price_state(state):
            return state["prices"], state["current"]

        for card_id, use_today, above in [
            ("gas_price_below_avg", False, False),
            ("gas_price_above_avg", False, True),
            ("gas_price_below_avg_today", True, False),
            ("gas_price_above_avg_today", True, True),
        ]:
            card = self.homey.flow.get_device_trigger_card(card_id)
            _use_today, _above = use_today, above
            async def run(args, state, _ut=_use_today, _ab=_above):
                prices, current = make_price_state(state)
                window = prices if _ut else prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
                avg = window.get_average()
                pct = args.get("percentage", 0)
                if _ab:
                    return current > avg * (1 + pct / 100)
                return current < avg * (1 - pct / 100)
            card.register_run_listener(run)

        for card_id, is_lowest in [("gas_price_at_lowest", True), ("gas_price_at_highest", False)]:
            card = self.homey.flow.get_device_trigger_card(card_id)
            _is_lowest = is_lowest
            async def run(args, state, _low=_is_lowest):
                prices, current = make_price_state(state)
                window = prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
                return current == (window.get_lowest() if _low else window.get_highest())
            card.register_run_listener(run)

        for card_id in ("gas_price_at_lowest_today", "gas_price_at_highest_today"):
            card = self.homey.flow.get_device_trigger_card(card_id)
            async def run(args, state):
                return True
            card.register_run_listener(run)

        for card_id, is_lowest in [("gas_price_among_lowest_today", True), ("gas_price_among_highest_today", False)]:
            card = self.homey.flow.get_device_trigger_card(card_id)
            _is_lowest = is_lowest
            async def run(args, state, _low=_is_lowest):
                prices, current = make_price_state(state)
                n = args.get("ranked_hours", 1)
                ranked = sorted(prices.get_sorted_values())
                cutoffs = ranked[:n] if _low else ranked[-n:]
                return current in cutoffs
            card.register_run_listener(run)

    def _register_electricity_conditions(self) -> None:
        flow = self.homey.flow

        def prices(args):
            device = args.get("device")
            return getattr(device, "_electricity_prices", None) if device else None

        def current(args):
            device = args.get("device")
            return device.get_capability_value("measure_price_current.electricity") if device else None

        async def below_threshold(args):
            price = current(args)
            return price is not None and price < args.get("price", 0)
        flow.get_condition_card("electricity_current_price_below").register_run_listener(below_threshold)

        async def below_avg(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            window = p.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return c < window.get_average() * (1 - args.get("percentage", 0) / 100)
        flow.get_condition_card("electricity_cond_price_below_avg").register_run_listener(below_avg)

        async def below_avg_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return c < p.get_average() * (1 - args.get("percentage", 0) / 100)
        flow.get_condition_card("electricity_cond_price_below_avg_today").register_run_listener(below_avg_today)

        async def at_lowest(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            window = p.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return c == window.get_lowest()
        flow.get_condition_card("electricity_cond_price_at_lowest").register_run_listener(at_lowest)

        async def at_highest(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            window = p.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return c == window.get_highest()
        flow.get_condition_card("electricity_cond_price_at_highest").register_run_listener(at_highest)

        async def at_lowest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return c == p.get_lowest()
        flow.get_condition_card("electricity_cond_price_at_lowest_today").register_run_listener(at_lowest_today)

        async def at_highest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return c == p.get_highest()
        flow.get_condition_card("electricity_cond_price_at_highest_today").register_run_listener(at_highest_today)

        async def among_lowest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            n = args.get("ranked_hours", 1)
            return c in sorted(p.get_sorted_values())[:n]
        flow.get_condition_card("electricity_cond_price_among_lowest_today").register_run_listener(among_lowest_today)

        async def among_highest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            n = args.get("ranked_hours", 1)
            return c in sorted(p.get_sorted_values())[-n:]
        flow.get_condition_card("electricity_cond_price_among_highest_today").register_run_listener(among_highest_today)

        async def among_lowest_during_time(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            window = p.get_for_time_window(args.get("time_from", "00:00"), args.get("time_to", "23:59"))
            n = args.get("ranked_hours", 1)
            return c in sorted(window.get_sorted_values())[:n]
        flow.get_condition_card("electricity_cond_price_among_lowest_during_time").register_run_listener(among_lowest_during_time)

    def _register_gas_conditions(self) -> None:
        flow = self.homey.flow

        def prices(args):
            device = args.get("device")
            return getattr(device, "_gas_prices", None) if device else None

        def current(args):
            device = args.get("device")
            return device.get_capability_value("measure_price_current.gas") if device else None

        async def below_threshold(args):
            price = current(args)
            return price is not None and price < args.get("price", 0)
        flow.get_condition_card("gas_current_price_below").register_run_listener(below_threshold)

        async def below_avg(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            window = p.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return c < window.get_average() * (1 - args.get("percentage", 0) / 100)
        flow.get_condition_card("gas_cond_price_below_avg").register_run_listener(below_avg)

        async def below_avg_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return c < p.get_average() * (1 - args.get("percentage", 0) / 100)
        flow.get_condition_card("gas_cond_price_below_avg_today").register_run_listener(below_avg_today)

        async def at_lowest(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            window = p.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return c == window.get_lowest()
        flow.get_condition_card("gas_cond_price_at_lowest").register_run_listener(at_lowest)

        async def at_highest(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            window = p.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return c == window.get_highest()
        flow.get_condition_card("gas_cond_price_at_highest").register_run_listener(at_highest)

        async def at_lowest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return c == p.get_lowest()
        flow.get_condition_card("gas_cond_price_at_lowest_today").register_run_listener(at_lowest_today)

        async def at_highest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return c == p.get_highest()
        flow.get_condition_card("gas_cond_price_at_highest_today").register_run_listener(at_highest_today)

        async def among_lowest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            n = args.get("ranked_hours", 1)
            return c in sorted(p.get_sorted_values())[:n]
        flow.get_condition_card("gas_cond_price_among_lowest_today").register_run_listener(among_lowest_today)

        async def among_highest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            n = args.get("ranked_hours", 1)
            return c in sorted(p.get_sorted_values())[-n:]
        flow.get_condition_card("gas_cond_price_among_highest_today").register_run_listener(among_highest_today)

        async def among_lowest_during_time(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            window = p.get_for_time_window(args.get("time_from", "00:00"), args.get("time_to", "23:59"))
            n = args.get("ranked_hours", 1)
            return c in sorted(window.get_sorted_values())[:n]
        flow.get_condition_card("gas_cond_price_among_lowest_during_time").register_run_listener(among_lowest_during_time)


homey_export = OctopusEnergyDevice
