import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30


class ServerClient:
    def __init__(self, server_url: str) -> None:
        self._base = server_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base

    async def create_account_link(self, homey_id: str, region: str) -> str:
        url = f"{self._base}/account/link"
        logger.info("POST %s (homey_id=%s region=%s)", url, homey_id, region)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json={"homey_id": homey_id, "region": region},
                )
                resp.raise_for_status()
                return resp.json()["authorization_url"]
        except httpx.HTTPStatusError as exc:
            logger.error("POST %s -> %s: %s", url, exc.response.status_code, exc.response.text)
            raise
        except httpx.HTTPError as exc:
            logger.error("POST %s failed: %r", url, exc)
            raise

    async def complete_account_link(self, homey_id: str, region: str, code: str) -> None:
        url = f"{self._base}/account/exchange"
        logger.info("POST %s (homey_id=%s region=%s)", url, homey_id, region)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json={"homey_id": homey_id, "region": region, "code": code},
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("POST %s -> %s: %s", url, exc.response.status_code, exc.response.text)
            raise
        except httpx.HTTPError as exc:
            logger.error("POST %s failed: %r", url, exc)
            raise

    async def get_accounts(self, homey_id: str, region: str) -> list[str]:
        url = f"{self._base}/users/{homey_id}/accounts"
        logger.info("GET %s (region=%s)", url, region)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, params={"region": region})
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("GET %s -> %s: %s", url, exc.response.status_code, exc.response.text)
            raise
        except httpx.HTTPError as exc:
            logger.error("GET %s failed: %r", url, exc)
            raise

    async def get_contract(self, homey_id: str, region: str, account_number: str) -> dict[str, Any]:
        url = f"{self._base}/users/{homey_id}/contracts/{account_number}"
        logger.info("GET %s (region=%s)", url, region)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, params={"region": region})
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("GET %s -> %s: %s", url, exc.response.status_code, exc.response.text)
            raise
        except httpx.HTTPError as exc:
            logger.error("GET %s failed: %r", url, exc)
            raise

    async def get_electricity_rates(
        self,
        homey_id: str,
        region: str,
        account_number: str,
    ) -> list[dict[str, Any]]:
        url = f"{self._base}/rates/electricity"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    url,
                    params={
                        "homey_id": homey_id,
                        "region": region,
                        "account_number": account_number,
                    },
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("GET %s -> %s: %s", url, exc.response.status_code, exc.response.text)
            raise
        except httpx.HTTPError as exc:
            logger.error("GET %s failed: %r", url, exc)
            raise

    async def get_gas_rates(
        self,
        homey_id: str,
        region: str,
        account_number: str,
    ) -> list[dict[str, Any]]:
        url = f"{self._base}/rates/gas"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    url,
                    params={
                        "homey_id": homey_id,
                        "region": region,
                        "account_number": account_number,
                    },
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("GET %s -> %s: %s", url, exc.response.status_code, exc.response.text)
            raise
        except httpx.HTTPError as exc:
            logger.error("GET %s failed: %r", url, exc)
            raise
