import logging
import traceback

from homey.driver import Driver
from homey.pair_session import PairSession

from ...lib import resolve_server_url
from ...lib.server_client import ServerClient

logger = logging.getLogger(__name__)


class OctopusEnergyDriver(Driver):
    async def on_init(self) -> None:
        server_url, source = resolve_server_url(self.homey)
        self.client = ServerClient(server_url)
        self.log(f"Driver initialized, server_url={server_url} (source={source})")

    async def on_pair(self, session: PairSession) -> None:
        self.log(f"on_pair: session started, server_url={self.client.base_url}")

        try:
            homey_id = await self.homey.cloud.get_homey_id()
        except Exception as exc:
            self.error(f"on_pair: failed to resolve homey_id: {exc!r}\n{traceback.format_exc()}")
            raise

        self.log(f"on_pair: homey_id={homey_id}")
        region = "DE"
        # auth["open_url"]: the Homey-hosted URL the account_link view must open.
        # auth["callback"]: keep a reference to the CloudOAuth2Callback alive.
        auth: dict = {"open_url": None, "callback": None}

        async def handle_region_selected(data: str) -> None:
            nonlocal region
            region = data
            self.log(f"on_pair: region selected -> {region}")

        async def handle_show_view(view_id: str) -> None:
            self.log(f"on_pair: showView -> {view_id}")
            if view_id != "loading":
                return

            self.log(
                f"on_pair: requesting account link "
                f"(homey_id={homey_id} region={region} server={self.client.base_url})"
            )
            try:
                authorize_url = await self.client.create_account_link(homey_id, region)
            except Exception as exc:
                self.error(f"on_pair: create_account_link failed: {exc!r}\n{traceback.format_exc()}")
                raise

            # Route the OAuth2 redirect through Homey's cloud callback. Homey relays
            # the auth code back to us; we forward it to the server to finish the
            # PKCE token exchange.
            callback = await self.homey.cloud.create_oauth2_callback(authorize_url)
            auth["callback"] = callback

            def on_url(url: str) -> None:
                auth["open_url"] = url
                self.log("on_pair: oauth2 callback url ready")

            async def on_code(code) -> None:
                if isinstance(code, Exception):
                    self.error(f"on_pair: oauth2 callback returned error: {code!r}")
                    await session.emit("auth_failed", str(code))
                    return
                self.log("on_pair: received auth code, exchanging via server")
                try:
                    await self.client.complete_account_link(homey_id, region, code)
                except Exception as exc:
                    self.error(f"on_pair: token exchange failed: {exc!r}\n{traceback.format_exc()}")
                    await session.emit("auth_failed", str(exc))
                    return
                # Drive the advance from the backend: events emitted to the front-end
                # can be missed while the OAuth popup is open/closing, leaving the
                # Continue button disabled. Advancing here is reliable.
                self.log("on_pair: account linked, advancing to device list")
                await session.emit("authorized")
                await session.next_view()
                self.log("on_pair: advanced past account_link to list_devices")

            callback.on_url(on_url)
            callback.on_code(on_code)

            await session.next_view()
            self.log("on_pair: advanced past loading view")

        async def handle_get_account_link(_data=None) -> str | None:
            self.log(f"on_pair: get_account_link -> {auth['open_url']}")
            return auth["open_url"]

        async def handle_list_devices(_data=None) -> list[dict]:
            self.log(f"on_pair: list_devices (homey_id={homey_id} region={region})")
            try:
                account_numbers = await self.client.get_accounts(homey_id, region)
            except Exception as exc:
                self.error(f"on_pair: failed to list accounts: {exc!r}\n{traceback.format_exc()}")
                return []

            self.log(f"on_pair: found {len(account_numbers)} account(s): {account_numbers}")
            devices = []
            for account_number in account_numbers:
                try:
                    contract = await self.client.get_contract(homey_id, region, account_number)
                    devices.append({
                        "name": f"Octopus Energy — {account_number}",
                        "data": {"id": f"{homey_id}:{region}:{account_number}"},
                        "store": {
                            "homey_id": homey_id,
                            "region": region,
                            "account_number": account_number,
                            "electricity_malo_id": contract.get("electricity", {}).get("malo_id") if contract.get("electricity") else None,
                            "gas_malo_id": contract.get("gas", {}).get("malo_id") if contract.get("gas") else None,
                        },
                    })
                except Exception as exc:
                    self.error(f"on_pair: failed to get contract {account_number}: {exc!r}\n{traceback.format_exc()}")

            self.log(f"on_pair: returning {len(devices)} device(s)")
            return devices

        session.set_handler("region_selected", handle_region_selected)
        session.set_handler("showView", handle_show_view)
        session.set_handler("get_account_link", handle_get_account_link)
        session.set_handler("list_devices", handle_list_devices)

    async def on_repair(self, session: PairSession, device=None) -> None:
        self.log(f"on_repair: session started, server_url={self.client.base_url}")
        homey_id = await self.homey.cloud.get_homey_id()
        region = device.get_store().get("region") if device else None
        self.log(f"on_repair: homey_id={homey_id} region={region}")
        auth: dict = {"open_url": None, "callback": None}

        async def handle_show_view(view_id: str) -> None:
            self.log(f"on_repair: showView -> {view_id}")
            if view_id != "loading":
                return

            self.log(
                f"on_repair: requesting account link "
                f"(homey_id={homey_id} region={region} server={self.client.base_url})"
            )
            try:
                authorize_url = await self.client.create_account_link(homey_id, region)
            except Exception as exc:
                self.error(f"on_repair: create_account_link failed: {exc!r}\n{traceback.format_exc()}")
                raise

            callback = await self.homey.cloud.create_oauth2_callback(authorize_url)
            auth["callback"] = callback

            def on_url(url: str) -> None:
                auth["open_url"] = url
                self.log("on_repair: oauth2 callback url ready")

            async def on_code(code) -> None:
                if isinstance(code, Exception):
                    self.error(f"on_repair: oauth2 callback returned error: {code!r}")
                    await session.emit("auth_failed", str(code))
                    return
                self.log("on_repair: received auth code, exchanging via server")
                try:
                    await self.client.complete_account_link(homey_id, region, code)
                except Exception as exc:
                    self.error(f"on_repair: token exchange failed: {exc!r}\n{traceback.format_exc()}")
                    await session.emit("auth_failed", str(exc))
                    return
                if device:
                    await device.set_available()
                self.log("on_repair: account re-linked, finishing")
                await session.emit("authorized")
                await session.done()

            callback.on_url(on_url)
            callback.on_code(on_code)

            await session.next_view()
            self.log("on_repair: advanced past loading view")

        async def handle_get_account_link(_data=None) -> str | None:
            self.log(f"on_repair: get_account_link -> {auth['open_url']}")
            return auth["open_url"]

        async def handle_repair_complete(_data=None) -> None:
            self.log("on_repair: repair complete")
            await session.done()

        session.set_handler("showView", handle_show_view)
        session.set_handler("get_account_link", handle_get_account_link)
        session.set_handler("repairComplete", handle_repair_complete)


homey_export = OctopusEnergyDriver
