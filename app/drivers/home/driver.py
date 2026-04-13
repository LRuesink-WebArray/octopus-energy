import logging

import homey
from homey import PairSession

from ...lib.server_client import ServerClient

logger = logging.getLogger(__name__)


class OctopusEnergyDriver(homey.Driver):
    async def on_init(self) -> None:
        server_url = self.homey.settings.get("server_url") or "https://octopus-energy-server.example.com"
        self.client = ServerClient(server_url)
        self.log("Driver initialized, server_url=%s", server_url)

    async def on_pair(self, session: PairSession) -> None:
        homey_id = await self.homey.cloud.get_homey_id()
        region: str | None = None
        account_link: str | None = None

        @session.set_handler("region_selected")
        async def handle_region_selected(data: str) -> None:
            nonlocal region
            region = data
            self.log("Region selected: %s", region)

        @session.set_handler("showView")
        async def handle_show_view(view_id: str) -> None:
            nonlocal account_link
            if view_id == "loading":
                try:
                    account_link = await self.client.create_account_link(homey_id, region)
                    await session.next_view()
                except Exception as exc:
                    self.error("Failed to create account link: %s", exc)
                    await session.emit("error", str(exc))
            elif view_id == "account_link":
                await session.emit("account_link_init", account_link)

        @session.set_handler("list_devices")
        async def handle_list_devices() -> list[dict]:
            try:
                account_numbers = await self.client.get_accounts(homey_id, region)
            except Exception as exc:
                self.error("Failed to list accounts: %s", exc)
                return []

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
                            "electricity_mpan": contract.get("electricity", {}).get("mpan") if contract.get("electricity") else None,
                            "electricity_serial": contract.get("electricity", {}).get("serial_number") if contract.get("electricity") else None,
                            "electricity_product_code": contract.get("electricity", {}).get("product_code") if contract.get("electricity") else None,
                            "electricity_tariff_code": contract.get("electricity", {}).get("tariff_code") if contract.get("electricity") else None,
                            "gas_mprn": contract.get("gas", {}).get("mprn") if contract.get("gas") else None,
                            "gas_serial": contract.get("gas", {}).get("serial_number") if contract.get("gas") else None,
                            "gas_product_code": contract.get("gas", {}).get("product_code") if contract.get("gas") else None,
                            "gas_tariff_code": contract.get("gas", {}).get("tariff_code") if contract.get("gas") else None,
                        },
                    })
                except Exception as exc:
                    self.error("Failed to get contract %s: %s", account_number, exc)

            return devices

    async def on_repair(self, session: PairSession, device=None) -> None:
        homey_id = await self.homey.cloud.get_homey_id()
        region = device.get_store_value("region") if device else None
        account_link: str | None = None

        @session.set_handler("showView")
        async def handle_show_view(view_id: str) -> None:
            nonlocal account_link
            if view_id == "loading":
                try:
                    account_link = await self.client.create_account_link(homey_id, region)
                    await session.next_view()
                except Exception as exc:
                    self.error("Failed to create account link during repair: %s", exc)
                    await session.emit("error", str(exc))
            elif view_id == "account_link":
                await session.emit("account_link_init", account_link)

        @session.set_handler("repairComplete")
        async def handle_repair_complete() -> None:
            if device:
                await device.set_available()
            await session.done()


homey_export = OctopusEnergyDriver
