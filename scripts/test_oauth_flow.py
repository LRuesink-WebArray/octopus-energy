"""
Test the OEG API using the device code flow (no redirect URI needed).

1. Requests a device code from auth.oeg-kraken.energy
2. You visit the URL and enter the code to authorize
3. Polls for the token
4. Runs API queries to validate our GraphQL works

Usage:
    source .venv/bin/activate
    python scripts/test_oauth_flow.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

GRAPHQL_URL = "https://api.oeg-kraken.energy/v1/graphql/"
IDENTITY_URL = "https://auth.oeg-kraken.energy"

env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")

if not CLIENT_ID:
    print("ERROR: Set OAUTH_CLIENT_ID in .env")
    sys.exit(1)


async def graphql(token: str, query: str, variables: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": token},
        )
    if resp.status_code != 200:
        print(f"   FAIL: HTTP {resp.status_code} — {resp.text[:300]}")
        return None
    body = resp.json()
    if "errors" in body:
        print(f"   GraphQL errors:")
        for e in body["errors"]:
            print(f"     - {e.get('message', e)}")
        return None
    return body.get("data")


async def get_token_via_device_flow() -> str:
    """Use device code flow to get a customer access token."""
    print("[1] Requesting device code...")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{IDENTITY_URL}/device-authorization/",
            data={"client_id": CLIENT_ID},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        print(f"   FAIL: {resp.status_code} — {resp.text}")
        sys.exit(1)

    data = resp.json()
    user_code = data["user_code"]
    device_code = data["device_code"]
    interval = data.get("interval", 5)
    expires_in = data.get("expires_in", 1800)
    verification_url = data.get("verification_uri_complete", data["verification_uri"])

    print(f"\n   ┌─────────────────────────────────────────┐")
    print(f"   │  Visit: {verification_url}")
    print(f"   │  Code:  {user_code}")
    print(f"   └─────────────────────────────────────────┘")
    print(f"\n   Log in with your Octopus Energy Germany account and enter the code above.")
    print(f"   Waiting (expires in {expires_in}s)...", end="", flush=True)

    # Poll for token
    for _ in range(expires_in // interval):
        await asyncio.sleep(interval)
        print(".", end="", flush=True)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{IDENTITY_URL}/token/",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code == 200:
            tokens = resp.json()
            print(" OK!")
            return tokens["access_token"]
        body = resp.json()
        error = body.get("error", "")
        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            await asyncio.sleep(interval)
            continue
        else:
            print(f"\n   FAIL: {error} — {body.get('error_description', '')}")
            sys.exit(1)

    print("\n   FAIL: Timed out waiting for authorization")
    sys.exit(1)


async def main():
    print("=" * 60)
    print("  OEG API Test (device code flow)")
    print("=" * 60)

    access_token = await get_token_via_device_flow()
    print(f"\n[2] Got access token (length={len(access_token)})")

    # Test: Viewer
    print("\n[3] Viewer query...")
    data = await graphql(access_token, "query { viewer { accounts { number } } }", {})
    if not data:
        return
    accounts = [a["number"] for a in data["viewer"]["accounts"]]
    print(f"   Accounts: {accounts}")
    if not accounts:
        return

    account_number = accounts[0]

    # Test: Account (German schema)
    print(f"\n[4] Account query ({account_number})...")
    data = await graphql(access_token, """
    query GetAccount($accountNumber: String!) {
      account(accountNumber: $accountNumber) {
        number
        properties {
          electricityMalos {
            malo
            meters { serialNumber, hasSmartMeterGateway }
            agreements(active: true) {
              tariff { ... on TariffType { productCode, tariffCode } }
              validFrom
              validTo
              unitRateForecast { validFrom, validTo, valueIncVat }
            }
          }
          gasMalos {
            malo
            meters { serialNumber, hasSmartMeterGateway }
            agreements(active: true) {
              tariff { ... on TariffType { productCode, tariffCode } }
              validFrom
              validTo
              unitRateForecast { validFrom, validTo, valueIncVat }
            }
          }
        }
      }
    }
    """, {"accountNumber": account_number})

    if not data:
        return

    account = data["account"]
    print(json.dumps(account, indent=2))

    # Extract MaLo
    elec_malo = ""
    props = account.get("properties") or []
    if props:
        elec_malos = props[0].get("electricityMalos") or []
        if elec_malos:
            elec_malo = elec_malos[0].get("malo", "")
            agreements = elec_malos[0].get("agreements") or []
            if agreements:
                forecast = agreements[0].get("unitRateForecast") or []
                print(f"\n   MaLo: {elec_malo}")
                print(f"   Rate forecast slots: {len(forecast)}")
                if forecast:
                    print(f"   Next: {forecast[0]}")
                    print(f"   Last: {forecast[-1]}")

    # Test: Consumption
    if elec_malo:
        now = datetime.now(tz=timezone.utc)
        period_from = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        period_to = now.isoformat()

        print(f"\n[5] Consumption query (malo={elec_malo})...")
        data = await graphql(access_token, """
        query($accountNumber: String!, $malo: String!, $periodFrom: DateTime!, $periodTo: DateTime!) {
          account(accountNumber: $accountNumber) {
            properties {
              electricityMalos(malo: $malo) {
                measurements(startAt: $periodFrom, endAt: $periodTo, readingType: INTERVAL, timeGranularity: THIRTY_MIN_INTERVAL) {
                  edges { node { startAt, endAt, value } }
                }
              }
            }
          }
        }
        """, {
            "accountNumber": account_number,
            "malo": elec_malo,
            "periodFrom": period_from,
            "periodTo": period_to,
        })
        if data:
            props = data["account"]["properties"]
            if props:
                malos = props[0].get("electricityMalos") or []
                if malos:
                    edges = malos[0].get("measurements", {}).get("edges", [])
                    print(f"   Measurements: {len(edges)}")
                    if edges:
                        print(f"   Latest: {edges[-1]['node']}")

    print("\n" + "=" * 60)
    print("  DONE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
