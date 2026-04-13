import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30


class ServerClient:
    def __init__(self, server_url: str) -> None:
        self._base = server_url.rstrip("/")

    async def create_account_link(self, homey_id: str, region: str) -> str:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base}/account/link",
                json={"homey_id": homey_id, "region": region},
            )
            resp.raise_for_status()
            return resp.json()["authorization_url"]

    async def get_accounts(self, homey_id: str, region: str) -> list[str]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base}/users/{homey_id}/accounts",
                params={"region": region},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_contract(self, homey_id: str, region: str, account_number: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base}/users/{homey_id}/contracts/{account_number}",
                params={"region": region},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_electricity_rates(
        self,
        region: str,
        product_code: str,
        tariff_code: str,
        period_from: str,
        period_to: str,
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base}/rates/electricity",
                params={
                    "region": region,
                    "product_code": product_code,
                    "tariff_code": tariff_code,
                    "period_from": period_from,
                    "period_to": period_to,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def get_gas_rates(
        self,
        region: str,
        product_code: str,
        tariff_code: str,
        period_from: str,
        period_to: str,
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base}/rates/gas",
                params={
                    "region": region,
                    "product_code": product_code,
                    "tariff_code": tariff_code,
                    "period_from": period_from,
                    "period_to": period_to,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def get_electricity_consumption(
        self,
        homey_id: str,
        region: str,
        mpan: str,
        serial_number: str,
        period_from: str,
        period_to: str,
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base}/users/{homey_id}/consumption/electricity",
                params={
                    "region": region,
                    "mpan": mpan,
                    "serial_number": serial_number,
                    "period_from": period_from,
                    "period_to": period_to,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def get_gas_consumption(
        self,
        homey_id: str,
        region: str,
        mprn: str,
        serial_number: str,
        period_from: str,
        period_to: str,
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base}/users/{homey_id}/consumption/gas",
                params={
                    "region": region,
                    "mprn": mprn,
                    "serial_number": serial_number,
                    "period_from": period_from,
                    "period_to": period_to,
                },
            )
            resp.raise_for_status()
            return resp.json()
