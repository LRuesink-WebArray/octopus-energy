import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from homey.device import Device

from ...lib import resolve_server_url
from ...lib.capabilities import CAPABILITY_OPTIONS, ELECTRICITY_CAPS, GAS_CAPS
from ...lib.prices import Prices
from ...lib.server_client import ServerClient

logger = logging.getLogger(__name__)

_POLL_RATES_INTERVAL = 3600       # 1 hour — full rate schedule + trigger evaluation
_JITTER_MAX = 60                  # seconds of random jitter to spread server load


class OctopusEnergyDevice(Device):

    async def on_init(self) -> None:
        # Captured here (on the event-loop thread) so the sync set_timeout
        # callback can schedule the async rates tick back onto the loop.
        self._loop = asyncio.get_running_loop()
        self._rates_task: asyncio.Task | None = None

        server_url, source = resolve_server_url(self.homey)
        self.client = ServerClient(server_url)
        account_number = self.get_store().get("account_number")
        self.log(f"Device init: {account_number} server_url={server_url} (source={source})")

        self._electricity_prices: Prices | None = None
        self._gas_prices: Prices | None = None

        # Keep this device's capabilities aligned with the fuel(s) its account
        # actually has, so a single-fuel account doesn't show dead tiles.
        try:
            await self._sync_capabilities()
        except Exception as exc:
            self.error(f"Capability sync failed: {exc!r}")

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
        if getattr(self, "_rates_task", None):
            self._rates_task.cancel()

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    async def _sync_capabilities(self) -> None:
        """Add/remove price capabilities so the device only exposes the fuel(s)
        its account has. Driven by the malo ids stored from the contract at
        pairing — never the (possibly transiently empty) rates response."""
        store = self.get_store()
        want: set[str] = set()
        if store.get("electricity_malo_id"):
            want.update(ELECTRICITY_CAPS)
        if store.get("gas_malo_id"):
            want.update(GAS_CAPS)

        # Neither fuel known (older/incomplete store) — don't strip blindly.
        if not want:
            return

        added, removed = [], []
        for cap in ELECTRICITY_CAPS + GAS_CAPS:
            has = self.has_capability(cap)
            if cap in want and not has:
                await self.add_capability(cap)
                options = CAPABILITY_OPTIONS.get(cap)
                if options:
                    try:
                        await self.set_capability_options(cap, options)
                    except Exception as exc:
                        self.error(f"Failed to set options for {cap}: {exc!r}")
                added.append(cap)
            elif cap not in want and has:
                await self.remove_capability(cap)
                removed.append(cap)

        if added or removed:
            self.log(f"Capabilities synced: +{added} -{removed}")

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _schedule_rates(self) -> None:
        now = datetime.now(tz=timezone.utc)
        next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        jitter = random.randint(0, _JITTER_MAX)
        delay_ms = int((next_hour.timestamp() - now.timestamp() + jitter) * 1000)

        self._rates_timer = self.homey.set_timeout(self._fire_rates_tick, delay_ms)
        self.log(f"Next rates poll in {delay_ms / 1000:.0f}s (+{jitter}s jitter)")

    def _fire_rates_tick(self) -> None:
        # Homey's set_timeout invokes callbacks synchronously and does not await
        # coroutines, so a bare `self._rates_tick` would be created and dropped
        # un-awaited (the poll would silently never run). Schedule it on the loop
        # instead; keep a reference so the task isn't garbage-collected mid-flight.
        self._rates_task = self._loop.create_task(self._rates_tick())

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
        if not self.has_capability("measure_price_current.electricity"):
            return
        current = self._electricity_prices.get_at_instant(now)
        if current is not None:
            await self.set_capability_value("measure_price_current.electricity", round(current, 4))
        await self.set_capability_value("measure_price_highest.electricity", round(self._electricity_prices.get_highest(), 4))
        await self.set_capability_value("measure_price_lowest.electricity", round(self._electricity_prices.get_lowest(), 4))

    async def _update_gas_capabilities(self, now: datetime) -> None:
        if not self._gas_prices:
            return
        if not self.has_capability("measure_price_current.gas"):
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

        state = {"slots": self._electricity_prices.get_slots(), "current": current, "energy_type": "electricity"}

        await self._trigger("electricity_price_below_avg", {}, state)
        await self._trigger("electricity_price_above_avg", {}, state)
        await self._trigger("electricity_price_below_avg_today", {}, state)
        await self._trigger("electricity_price_above_avg_today", {}, state)
        await self._trigger("electricity_price_at_lowest", {}, state)
        await self._trigger("electricity_price_at_highest", {}, state)
        await self._trigger("electricity_price_among_lowest_today", {}, state)
        await self._trigger("electricity_price_among_highest_today", {}, state)

        if self._electricity_prices.is_cheapest(current):
            await self._trigger("electricity_price_at_lowest_today", {}, state)
        if self._electricity_prices.is_priciest(current):
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

        state = {"slots": self._gas_prices.get_slots(), "current": current, "energy_type": "gas"}

        await self._trigger("gas_price_below_avg", {}, state)
        await self._trigger("gas_price_above_avg", {}, state)
        await self._trigger("gas_price_below_avg_today", {}, state)
        await self._trigger("gas_price_above_avg_today", {}, state)
        await self._trigger("gas_price_at_lowest", {}, state)
        await self._trigger("gas_price_at_highest", {}, state)
        await self._trigger("gas_price_among_lowest_today", {}, state)
        await self._trigger("gas_price_among_highest_today", {}, state)

        if self._gas_prices.is_cheapest(current):
            await self._trigger("gas_price_at_lowest_today", {}, state)
        if self._gas_prices.is_priciest(current):
            await self._trigger("gas_price_at_highest_today", {}, state)

    async def _trigger(self, card_id: str, tokens: dict, state: dict) -> None:
        try:
            card = self.homey.flow.get_device_trigger_card(card_id)
            # FlowCardTriggerDevice.trigger(device, tokens={}, **trigger_kwargs):
            # state is not positional — pass it as a keyword so the SDK forwards
            # it to the card's run listener.
            await card.trigger(self, tokens, state=state)
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
        self._register_price_triggers("electricity")

    def _register_price_triggers(self, fuel: str) -> None:
        def make_price_state(state):
            return Prices(state["slots"]), state["current"]

        def window_for(prices, args, use_today):
            if use_today:
                return prices
            return prices.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))

        def register_avg(card_id, use_today, above):
            card = self.homey.flow.get_device_trigger_card(card_id)
            async def run(args, state, _ut=use_today, _ab=above):
                prices, current = make_price_state(state)
                window = window_for(prices, args, _ut)
                pct = args.get("percentage", 0)
                return window.is_above_average(current, pct) if _ab else window.is_below_average(current, pct)
            card.register_run_listener(run)

        register_avg(f"{fuel}_price_below_avg", use_today=False, above=False)
        register_avg(f"{fuel}_price_below_avg_today", use_today=True, above=False)
        register_avg(f"{fuel}_price_above_avg", use_today=False, above=True)
        register_avg(f"{fuel}_price_above_avg_today", use_today=True, above=True)

        def register_extreme(card_id, is_low, use_today):
            card = self.homey.flow.get_device_trigger_card(card_id)
            async def run(args, state, _low=is_low, _ut=use_today):
                prices, current = make_price_state(state)
                window = window_for(prices, args, _ut)
                return window.is_cheapest(current) if _low else window.is_priciest(current)
            card.register_run_listener(run)

        register_extreme(f"{fuel}_price_at_lowest", is_low=True, use_today=False)
        register_extreme(f"{fuel}_price_at_highest", is_low=False, use_today=False)
        register_extreme(f"{fuel}_price_at_lowest_today", is_low=True, use_today=True)
        register_extreme(f"{fuel}_price_at_highest_today", is_low=False, use_today=True)

        def register_among(card_id, is_low):
            card = self.homey.flow.get_device_trigger_card(card_id)
            async def run(args, state, _low=is_low):
                prices, current = make_price_state(state)
                n = args.get("ranked_hours", 1)
                return prices.is_among_cheapest(current, n) if _low else prices.is_among_priciest(current, n)
            card.register_run_listener(run)

        register_among(f"{fuel}_price_among_lowest_today", is_low=True)
        register_among(f"{fuel}_price_among_highest_today", is_low=False)

    def _register_gas_triggers(self) -> None:
        self._register_price_triggers("gas")

    def _register_electricity_conditions(self) -> None:
        self._register_price_conditions("electricity")

    def _register_price_conditions(self, fuel: str) -> None:
        flow = self.homey.flow
        cap = f"measure_price_current.{fuel}"
        prefix = f"{fuel}_cond_price"

        def prices(args):
            device = args.get("device")
            return getattr(device, f"_{fuel}_prices", None) if device else None

        def current(args):
            device = args.get("device")
            if not device or not device.has_capability(cap):
                return None
            return device.get_capability_value(cap)

        def window(args, p):
            return p.get_for_next_n_hours(datetime.now(tz=timezone.utc), args.get("hours", 1))

        async def below_threshold(args):
            price = current(args)
            return price is not None and price < args.get("price", 0)
        flow.get_condition_card(f"{fuel}_current_price_below").register_run_listener(below_threshold)

        async def below_avg(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return window(args, p).is_below_average(c, args.get("percentage", 0))
        flow.get_condition_card(f"{prefix}_below_avg").register_run_listener(below_avg)

        async def below_avg_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return p.is_below_average(c, args.get("percentage", 0))
        flow.get_condition_card(f"{prefix}_below_avg_today").register_run_listener(below_avg_today)

        async def at_lowest(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return window(args, p).is_cheapest(c)
        flow.get_condition_card(f"{prefix}_at_lowest").register_run_listener(at_lowest)

        async def at_highest(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return window(args, p).is_priciest(c)
        flow.get_condition_card(f"{prefix}_at_highest").register_run_listener(at_highest)

        async def at_lowest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return p.is_cheapest(c)
        flow.get_condition_card(f"{prefix}_at_lowest_today").register_run_listener(at_lowest_today)

        async def at_highest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return p.is_priciest(c)
        flow.get_condition_card(f"{prefix}_at_highest_today").register_run_listener(at_highest_today)

        async def among_lowest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return p.is_among_cheapest(c, args.get("ranked_hours", 1))
        flow.get_condition_card(f"{prefix}_among_lowest_today").register_run_listener(among_lowest_today)

        async def among_highest_today(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            return p.is_among_priciest(c, args.get("ranked_hours", 1))
        flow.get_condition_card(f"{prefix}_among_highest_today").register_run_listener(among_highest_today)

        async def among_lowest_during_time(args):
            p, c = prices(args), current(args)
            if not p or c is None:
                return False
            w = p.get_for_time_window(args.get("time_from", "00:00"), args.get("time_to", "23:59"))
            return w.is_among_cheapest(c, args.get("ranked_hours", 1))
        flow.get_condition_card(f"{prefix}_among_lowest_during_time").register_run_listener(among_lowest_during_time)

    def _register_gas_conditions(self) -> None:
        self._register_price_conditions("gas")


homey_export = OctopusEnergyDevice
