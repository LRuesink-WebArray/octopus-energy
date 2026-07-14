# Octopus Energy Homey Integration -- Cascade Notes

## Architecture

Two components:

1. **Server** (`server/`, separate repo: `athombv/athom-cloud-octopus`)
   - FastAPI application deployed on Northflank
   - Redis for token caching, rate limiting, and PKCE state
   - Acts as OAuth2 intermediary between Homey devices and Kraken API
   - Docker image: `python:3.13-slim`

2. **Homey App** (`app/`, main repo: `LRuesink-WebArray/octopus-energy`)
   - Homey SDK 3, Python-based, platforms: local + cloud
   - Single driver `home` with electricity + gas capabilities
   - Polls server for rates (hourly) and consumption (every 30 min)
   - Flow cards for price triggers and conditions with hourly deduplication

### Component Interaction

```
Homey Device
  |
  v
ServerClient (app/lib/server_client.py)
  |  HTTP
  v
FastAPI Server (server/src/main.py)
  |
  +-- PartnerAuthenticator --> client credentials token --> Kraken GraphQL (rates)
  +-- CustomerTokenStore   --> customer OAuth2 token    --> Kraken GraphQL (accounts, consumption)
  +-- Redis (token cache, rate limits, PKCE verifiers)
```

### Key Files

| Component | File | Purpose |
|-----------|------|---------|
| Server entry | `server/src/main.py` | FastAPI app with lifespan, dependency wiring |
| Routes | `server/src/api/routes.py` | REST endpoints for linking, rates, consumption |
| Kraken client | `server/src/lib/kraken_client.py` | GraphQL queries, OAuth2 helpers (PKCE) |
| Partner auth | `server/src/lib/partner_authenticator.py` | Client credentials grant, Redis-cached |
| Customer tokens | `server/src/lib/customer_token_store.py` | Store/refresh customer OAuth2 tokens |
| Rate limiter | `server/src/lib/rate_limiter.py` | Redis sorted-set sliding window per region |
| Regions | `server/src/lib/regions.py` | Per-country GraphQL + identity URLs |
| Settings | `server/src/settings.py` | Pydantic settings, lazy via `get_settings()` |
| App entry | `app/app.py` | Homey app class |
| Driver | `app/drivers/home/driver.py` | Pairing + repair flows |
| Device | `app/drivers/home/device.py` | Polling, capability updates, flow triggers |
| Prices | `app/lib/prices.py` | Price calculations (avg, min, max, windows) |
| Server client | `app/lib/server_client.py` | HTTP client for server API |

---

## Auth Flows

Reference: https://auth.oeg-kraken.systems/

### 1. Client Credentials (server-to-server, for rates)

Used by `PartnerAuthenticator` to get a token for non-user-specific queries (tariff rates).

```
POST {identity_url}/token/
Authorization: Basic BASE64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
```

Response: `{ "access_token": "...", "expires_in": 3600 }`

Token is cached in Redis key `partner_token:{region_code}` with TTL.

### 2. Authorization Code + PKCE (customer login)

Used to link a user's Octopus Energy account.

**Step 1 -- Build auth URL** (`build_authorization_url`):
- Generate `code_verifier` (random), derive `code_challenge` (SHA-256, base64url)
- Store `code_verifier` in Redis key `pkce:{homey_id}:{region}` (TTL 600s)
- Redirect user to:
  ```
  GET {identity_url}/authorize/?
    response_type=code&
    client_id=...&
    redirect_uri={server_url}/oauth/callback&
    state={homey_id}:{region}&
    code_challenge=...&
    code_challenge_method=S256
  ```

**Step 2 -- Callback** (`/oauth/callback`):
- Kraken redirects with `?code=...&state=...`
- Retrieve `code_verifier` from Redis using state
- Exchange code for tokens:
  ```
  POST {identity_url}/token/
  Content-Type: application/x-www-form-urlencoded

  grant_type=authorization_code&
  code=...&
  client_id=...&
  redirect_uri=...&
  code_verifier=...
  ```

**Step 3 -- Token refresh** (`CustomerTokenStore._refresh`):
```
POST {identity_url}/token/
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&
refresh_token=...&
client_id=...&
client_secret=...
```

### 3. GraphQL Requests

All GraphQL calls use `Authorization: Bearer {token}` header against `{region.graphql_url}`.

---

## Environment Variables

| Variable | Required | Default | Used For |
|----------|----------|---------|----------|
| `OAUTH_CLIENT_ID` | Yes | -- | Client credentials + auth code flows |
| `OAUTH_CLIENT_SECRET` | Yes | -- | Client credentials + token refresh |
| `SERVER_URL` | No | `http://localhost:3000` | OAuth redirect URI |
| `REDIS_URL` | No | `redis://localhost:6379/0` | Token cache, rate limits, PKCE |
| `PORT` | No | `3000` | Server listen port |

---

## Implementation Status

### Done

- Server: FastAPI app, all routes, Redis integration, rate limiter
- Server: OAuth2 client credentials (partner token) + PKCE auth code flow
- Server: Kraken GraphQL client (accounts, rates, consumption)
- Server: Customer token store with auto-refresh
- Server: Docker + docker-compose with Redis
- Server: Deployed on Northflank (auto-build on push)
- App: `app.json` seed + `.homeycompose` capabilities and flow cards
- App: Driver with pairing (region select, account link) and repair flows
- App: Device with hourly rate polling + 30-min consumption polling
- App: Flow triggers (20) and conditions (20) for electricity + gas
- App: Flow trigger deduplication (once per hour)
- App: Placeholder assets (icon.svg, images)
- Git: Server as submodule (`athombv/athom-cloud-octopus`)
- Git: Main repo (`LRuesink-WebArray/octopus-energy`)

### Known Issues / Warnings

- `homey app build` warning: `drivers.home has energy.cumulative set to true, but is missing 'cumulativeExportedCapability'` -- harmless, device is import-only (no solar export). Same pattern as Ostrom app.
- Placeholder asset images are 1x1 pink PNGs -- need real artwork.
- Region URLs for ES, JP, IT, US are assumed (`.energy` domain); only DE (`.systems`) and GB are confirmed.

### Not Yet Done / To Verify

- End-to-end test of full OAuth2 flow against live Kraken instance
- Verify GraphQL queries work with OAuth2 Bearer tokens (vs old JWT prefix)
- Verify `TariffType` fragment works across all regions
- Real app icon and driver images
- Homey app store submission
- Production `SERVER_URL` configuration on Northflank
