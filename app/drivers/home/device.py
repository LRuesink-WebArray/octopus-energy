import logging
import random
from datetime import datetime, timedelta, timezone

import homey

from ...lib.prices import Prices
from ...lib.server_client import ServerClient

logger = logging.getLogger(__name__)

_POLL_RATES_INTERVAL = 3600       # 1 hour — full rate schedule + trigger evaluation
_POLL_CONSUMPTION_INTERVAL = 1800 # 30 minutes — consumption data
_JITTER_MAX = 60                  # seconds of random jitter to spread server load


class OctopusEnergyDevice(homey.Device):

    async def on_init(self) -> None:
        server_url = self.homey.settings.get("server_url") or "https://octopus-energy-server.example.com"
        self.client = ServerClient(server_url)

        self._electricity_prices: Prices | None = None
        self._gas_prices: Prices | None = None

        self._register_flow_cards()
        await self._poll_rates()
        await self._poll_consumption()
        self._schedule_rates()
        self._schedule_consumption()
        self.log("Device initialized: %s", self.get_store_value("account_number"))

    def on_deleted(self) -> None:
        if hasattr(self, "_rates_timer") and self._rates_timer:
            self.homey.clear_timeout(self._rates_timer)
        if hasattr(self, "_consumption_timer") and self._consumption_timer:
            self.homey.clear_timeout(self._consumption_timer)

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

    def _schedule_consumption(self) -> None:
        jitter = random.randint(0, _JITTER_MAX)
        delay_ms = (_POLL_CONSUMPTION_INTERVAL + jitter) * 1000
        self._consumption_timer = self.homey.set_timeout(self._consumption_tick, delay_ms)

    async def _rates_tick(self) -> None:
        try:
            await self._poll_rates()
        except Exception as exc:
            self.error("Rates poll failed: %s", exc)
        self._schedule_rates()

    async def _consumption_tick(self) -> None:
        try:
            await self._poll_consumption()
        except Exception as exc:
            self.error("Consumption poll failed: %s", exc)
        self._schedule_consumption()

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def _poll_rates(self) -> None:
        store = self.get_store()
        region = store.get("region")
        now = datetime.now(tz=timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

        elec_product = store.get("electricity_product_code")
        elec_tariff = store.get("electricity_tariff_code")
        gas_product = store.get("gas_product_code")
        gas_tariff = store.get("gas_tariff_code")

        if elec_product and elec_tariff:
            try:
                slots = await self.client.get_electricity_rates(
                    region, elec_product, elec_tariff, today_start, today_end
                )
                self._electricity_prices = Prices(slots)
                await self._update_electricity_capabilities(now)
                await self._fire_electricity_triggers(now)
            except Exception as exc:
                self.error("Failed to fetch electricity rates: %s", exc)

        if gas_product and gas_tariff:
            try:
                slots = await self.client.get_gas_rates(
                    region, gas_product, gas_tariff, today_start, today_end
                )
                self._gas_prices = Prices(slots)
                await self._update_gas_capabilities(now)
                await self._fire_gas_triggers(now)
            except Exception as exc:
                self.error("Failed to fetch gas rates: %s", exc)

    async def _poll_consumption(self) -> None:
        store = self.get_store()
        homey_id = store.get("homey_id")
        region = store.get("region")
        now = datetime.now(tz=timezone.utc)
        period_to = now.replace(minute=0, second=0, microsecond=0).isoformat()
        period_from = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        mpan = store.get("electricity_mpan")
        elec_serial = store.get("electricity_serial")
        if mpan and elec_serial:
            try:
                entries = await self.client.get_electricity_consumption(
                    homey_id, region, mpan, elec_serial, period_from, period_to
                )
                if entries:
                    total = sum(e["consumption"] for e in entries)
                    await self.set_capability_value("meter_power.electricity", round(total, 4))
            except Exception as exc:
                self.error("Failed to fetch electricity consumption: %s", exc)

        mprn = store.get("gas_mprn")
        gas_serial = store.get("gas_serial")
        if mprn and gas_serial:
            try:
                entries = await self.client.get_gas_consumption(
                    homey_id, region, mprn, gas_serial, period_from, period_to
                )
                if entries:
                    total = sum(e["consumption"] for e in entries)
                    await self.set_capability_value("meter_power.gas", round(total, 4))
            except Exception as exc:
                self.error("Failed to fetch gas consumption: %s", exc)

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
        if self.get_store_value("last_triggered_hour_electricity") == hour_key:
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
        if self.get_store_value("last_triggered_hour_gas") == hour_key:
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
        def current_elec_price() -> float | None:
            return self.get_capability_value("measure_price_current.electricity")

        card = self.homey.flow.get_condition_card("electricity_current_price_below")
        async def elec_below_threshold(args):
            price = current_elec_price()
            return price is not None and price < args.get("price", 0)
        card.register_run_listener(elec_below_threshold)

        card = self.homey.flow.get_condition_card("electricity_cond_price_below_avg")
        async def elec_cond_below_avg(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            window = self._electricity_prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return current is not None and current < window.get_average() * (1 - args.get("percentage", 0) / 100)
        card.register_run_listener(elec_cond_below_avg)

        card = self.homey.flow.get_condition_card("electricity_cond_price_below_avg_today")
        async def elec_cond_below_avg_today(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            return current is not None and current < self._electricity_prices.get_average() * (1 - args.get("percentage", 0) / 100)
        card.register_run_listener(elec_cond_below_avg_today)

        card = self.homey.flow.get_condition_card("electricity_cond_price_at_lowest")
        async def elec_cond_at_lowest(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            window = self._electricity_prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return current is not None and current == window.get_lowest()
        card.register_run_listener(elec_cond_at_lowest)

        card = self.homey.flow.get_condition_card("electricity_cond_price_at_highest")
        async def elec_cond_at_highest(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            window = self._electricity_prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return current is not None and current == window.get_highest()
        card.register_run_listener(elec_cond_at_highest)

        card = self.homey.flow.get_condition_card("electricity_cond_price_at_lowest_today")
        async def elec_cond_at_lowest_today(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            return current is not None and current == self._electricity_prices.get_lowest()
        card.register_run_listener(elec_cond_at_lowest_today)

        card = self.homey.flow.get_condition_card("electricity_cond_price_at_highest_today")
        async def elec_cond_at_highest_today(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            return current is not None and current == self._electricity_prices.get_highest()
        card.register_run_listener(elec_cond_at_highest_today)

        card = self.homey.flow.get_condition_card("electricity_cond_price_among_lowest_today")
        async def elec_cond_among_lowest_today(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            n = args.get("ranked_hours", 1)
            return current is not None and current in sorted(self._electricity_prices.get_sorted_values())[:n]
        card.register_run_listener(elec_cond_among_lowest_today)

        card = self.homey.flow.get_condition_card("electricity_cond_price_among_highest_today")
        async def elec_cond_among_highest_today(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            n = args.get("ranked_hours", 1)
            return current is not None and current in sorted(self._electricity_prices.get_sorted_values())[-n:]
        card.register_run_listener(elec_cond_among_highest_today)

        card = self.homey.flow.get_condition_card("electricity_cond_price_among_lowest_during_time")
        async def elec_cond_among_lowest_during_time(args):
            if not self._electricity_prices:
                return False
            current = current_elec_price()
            window = self._electricity_prices.get_for_time_window(args.get("time_from", "00:00"), args.get("time_to", "23:59"))
            n = args.get("ranked_hours", 1)
            return current is not None and current in sorted(window.get_sorted_values())[:n]
        card.register_run_listener(elec_cond_among_lowest_during_time)

    def _register_gas_conditions(self) -> None:
        def current_gas_price() -> float | None:
            return self.get_capability_value("measure_price_current.gas")

        card = self.homey.flow.get_condition_card("gas_current_price_below")
        async def gas_below_threshold(args):
            price = current_gas_price()
            return price is not None and price < args.get("price", 0)
        card.register_run_listener(gas_below_threshold)

        card = self.homey.flow.get_condition_card("gas_cond_price_below_avg")
        async def gas_cond_below_avg(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            window = self._gas_prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return current is not None and current < window.get_average() * (1 - args.get("percentage", 0) / 100)
        card.register_run_listener(gas_cond_below_avg)

        card = self.homey.flow.get_condition_card("gas_cond_price_below_avg_today")
        async def gas_cond_below_avg_today(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            return current is not None and current < self._gas_prices.get_average() * (1 - args.get("percentage", 0) / 100)
        card.register_run_listener(gas_cond_below_avg_today)

        card = self.homey.flow.get_condition_card("gas_cond_price_at_lowest")
        async def gas_cond_at_lowest(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            window = self._gas_prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return current is not None and current == window.get_lowest()
        card.register_run_listener(gas_cond_at_lowest)

        card = self.homey.flow.get_condition_card("gas_cond_price_at_highest")
        async def gas_cond_at_highest(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            window = self._gas_prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))
            return current is not None and current == window.get_highest()
        card.register_run_listener(gas_cond_at_highest)

        card = self.homey.flow.get_condition_card("gas_cond_price_at_lowest_today")
        async def gas_cond_at_lowest_today(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            return current is not None and current == self._gas_prices.get_lowest()
        card.register_run_listener(gas_cond_at_lowest_today)

        card = self.homey.flow.get_condition_card("gas_cond_price_at_highest_today")
        async def gas_cond_at_highest_today(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            return current is not None and current == self._gas_prices.get_highest()
        card.register_run_listener(gas_cond_at_highest_today)

        card = self.homey.flow.get_condition_card("gas_cond_price_among_lowest_today")
        async def gas_cond_among_lowest_today(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            n = args.get("ranked_hours", 1)
            return current is not None and current in sorted(self._gas_prices.get_sorted_values())[:n]
        card.register_run_listener(gas_cond_among_lowest_today)

        card = self.homey.flow.get_condition_card("gas_cond_price_among_highest_today")
        async def gas_cond_among_highest_today(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            n = args.get("ranked_hours", 1)
            return current is not None and current in sorted(self._gas_prices.get_sorted_values())[-n:]
        card.register_run_listener(gas_cond_among_highest_today)

        card = self.homey.flow.get_condition_card("gas_cond_price_among_lowest_during_time")
        async def gas_cond_among_lowest_during_time(args):
            if not self._gas_prices:
                return False
            current = current_gas_price()
            window = self._gas_prices.get_for_time_window(args.get("time_from", "00:00"), args.get("time_to", "23:59"))
            n = args.get("ranked_hours", 1)
            return current is not None and current in sorted(window.get_sorted_values())[:n]
        card.register_run_listener(gas_cond_among_lowest_during_time)


homey_export = OctopusEnergyDevice
