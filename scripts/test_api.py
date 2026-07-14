"""
Test script for verifying the German Kraken API integration.

Runs against the real OEG Kraken endpoints to validate:
1. Client credentials token acquisition
2. Account query (properties → electricityMalos/gasMalos)
3. Electricity rates query
4. Consumption query (measurements API)

Usage:
    # Set credentials in .env or export them:
    export OAUTH_CLIENT_ID=...
    export OAUTH_CLIENT_SECRET=...

    # Optionally set a customer token for account/consumption tests:
    export CUSTOMER_ACCESS_TOKEN=...

    python scripts/test_api.py
"""

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

GRAPHQL_URL = "https://api.oeg-kraken.energy/v1/graphql/"
IDENTITY_URL = "https://auth.oeg-kraken.energy"

# Load .env file if present
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
CUSTOMER_TOKEN = os.environ.get("CUSTOMER_ACCESS_TOKEN", "")


def pp(label: str, data):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(json.dumps(data, indent=2, default=str))


async def get_client_credentials_token() -> str:
    """Test client credentials flow — will fail for Authorization Code-only apps."""
    print("\n[1] Testing client credentials token (expected to fail for auth-code apps)...")

    if not CLIENT_ID or not CLIENT_SECRET:
        print("   SKIP: OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET not set")
        return ""

    credentials = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{IDENTITY_URL}/token/",
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    if resp.status_code != 200:
        print(f"   Expected: {resp.status_code} — {resp.json().get('error', resp.text[:100])}")
        print("   (This app uses Authorization Code grant only — use CUSTOMER_ACCESS_TOKEN instead)")
        return ""

    payload = resp.json()
    token = payload["access_token"]
    expires_in = payload.get("expires_in", "?")
    print(f"   OK: got token (expires_in={expires_in}s, length={len(token)})")
    return token


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
        print(f"   FAIL: GraphQL errors:")
        for e in body["errors"]:
            print(f"     - {e.get('message', e)}")
        return None

    return body.get("data")


async def test_viewer_query(token: str) -> list[str]:
    """Test viewer query to list account numbers."""
    print("\n[2] Testing viewer query (list accounts)...")

    if not token:
        print("   SKIP: no customer token")
        return []

    query = """
    query Viewer {
      viewer {
        accounts {
          number
        }
      }
    }
    """
    data = await graphql(token, query, {})
    if data:
        accounts = [a["number"] for a in data["viewer"]["accounts"]]
        print(f"   OK: found {len(accounts)} account(s): {accounts}")
        return accounts
    return []


async def test_account_query(token: str, account_number: str):
    """Test German account query with properties → electricityMalos/gasMalos."""
    print(f"\n[3] Testing account query for {account_number}...")

    if not token:
        print("   SKIP: no customer token")
        return None

    query = """
    query GetAccount($accountNumber: String!) {
      account(accountNumber: $accountNumber) {
        number
        properties {
          electricityMalos {
            malo
            meters {
              serialNumber
              hasSmartMeterGateway
            }
            agreements(active: true) {
              tariff {
                ... on TariffType {
                  productCode
                  tariffCode
                }
              }
              validFrom
              validTo
            }
          }
          gasMalos {
            malo
            meters {
              serialNumber
              hasSmartMeterGateway
            }
            agreements(active: true) {
              tariff {
                ... on TariffType {
                  productCode
                  tariffCode
                }
              }
              validFrom
              validTo
            }
          }
        }
      }
    }
    """
    data = await graphql(token, query, {"accountNumber": account_number})
    if data:
        pp("Account response", data["account"])
        return data["account"]
    return None


async def test_electricity_rates(token: str, account_number: str):
    """Test unitRateForecast query."""
    print(f"\n[4] Testing electricity unitRateForecast (account={account_number})...")

    if not token:
        print("   SKIP: no token")
        return

    query = """
    query ElectricityRateForecast($accountNumber: String!) {
      account(accountNumber: $accountNumber) {
        properties {
          electricityMalos {
            agreements(active: true) {
              unitRateForecast {
                validFrom
                validTo
                valueIncVat
              }
            }
          }
        }
      }
    }
    """
    data = await graphql(token, query, {"accountNumber": account_number})
    if data:
        props = data["account"]["properties"]
        if not props:
            print("   WARN: no properties returned")
            return
        malos = props[0].get("electricityMalos") or []
        if not malos:
            print("   WARN: no electricityMalos returned")
            return
        agreements = malos[0].get("agreements") or []
        if not agreements:
            print("   WARN: no active agreements")
            return
        forecast = agreements[0].get("unitRateForecast") or []
        print(f"   OK: got {len(forecast)} rate forecast slot(s)")
        if forecast:
            print(f"   First: {forecast[0]}")
            print(f"   Last:  {forecast[-1]}")


async def test_consumption(token: str, account_number: str, malo_id: str):
    """Test measurements/consumption query."""
    print(f"\n[5] Testing electricity consumption (malo={malo_id})...")

    if not token or not malo_id:
        print("   SKIP: no token or malo_id")
        return

    now = datetime.now(tz=timezone.utc)
    period_from = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    period_to = now.isoformat()

    query = """
    query ElectricityConsumption($accountNumber: String!, $malo: String!, $periodFrom: DateTime!, $periodTo: DateTime!) {
      account(accountNumber: $accountNumber) {
        properties {
          electricityMalos(malo: $malo) {
            measurements(startAt: $periodFrom, endAt: $periodTo, readingType: INTERVAL, timeGranularity: THIRTY_MIN_INTERVAL) {
              edges {
                node {
                  startAt
                  endAt
                  value
                }
              }
            }
          }
        }
      }
    }
    """
    data = await graphql(token, query, {
        "accountNumber": account_number,
        "malo": malo_id,
        "periodFrom": period_from,
        "periodTo": period_to,
    })
    if data:
        props = data["account"]["properties"]
        if props:
            malos = props[0].get("electricityMalos", [])
            if malos:
                edges = malos[0].get("measurements", {}).get("edges", [])
                print(f"   OK: got {len(edges)} measurement(s)")
                if edges:
                    print(f"   Sample: {edges[0]['node']}")
            else:
                print("   WARN: no electricityMalos returned")
        else:
            print("   WARN: no properties returned")


async def main():
    print("Octopus Energy Germany (OEG) API Test")
    print(f"GraphQL: {GRAPHQL_URL}")
    print(f"Identity: {IDENTITY_URL}")
    print(f"Client ID: {CLIENT_ID[:8]}..." if CLIENT_ID else "Client ID: NOT SET")
    print(f"Customer token: {'SET' if CUSTOMER_TOKEN else 'NOT SET'}")

    # 1. Get partner token via client credentials
    partner_token = await get_client_credentials_token()

    # Use customer token if provided, otherwise skip account-level tests
    customer_token = CUSTOMER_TOKEN

    # 2. List accounts
    accounts = await test_viewer_query(customer_token)

    # 3. Get account details (German schema)
    account_data = None
    if accounts:
        account_data = await test_account_query(customer_token, accounts[0])

    # 4. Test rates via unitRateForecast (customer-scoped)
    elec_malo = ""
    if account_data:
        props = account_data.get("properties") or []
        if props:
            elec_malos = props[0].get("electricityMalos") or []
            if elec_malos:
                elec_malo = elec_malos[0].get("malo", "")

    if accounts and customer_token:
        await test_electricity_rates(customer_token, accounts[0])
    else:
        print("\n[4] SKIP: no account/customer token for rate forecast test")

    # 5. Test consumption
    if accounts and elec_malo:
        await test_consumption(customer_token, accounts[0], elec_malo)
    else:
        print("\n[5] SKIP: no account/malo_id available for consumption test")

    print("\n" + "="*60)
    print("  DONE")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
