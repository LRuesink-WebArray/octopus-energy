# Octopus Energy Homey App

Homey app (Python) + backend server integrating with the Octopus Energy Kraken GraphQL API across all regions.

## Repository structure

```
octopus-energy/
  app/          Homey Python app
  server/       FastAPI backend server
  docker-compose.yml  Local development environment
  .env.example        Required environment variables
```

## Local development

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Start the server and Redis:

```bash
docker compose up --build
```

The server will be available at `http://localhost:3000`.

## Server environment variables

| Variable | Description |
|----------|-------------|
| `API_KEY` | Kraken organization API key (for `obtainKrakenToken`) |
| `OAUTH_CLIENT_ID` | OAuth2 application client ID |
| `OAUTH_CLIENT_SECRET` | OAuth2 application client secret |
| `SERVER_URL` | Public URL of this server (used as OAuth2 redirect URI base) |
| `REDIS_URL` | Redis connection string, e.g. `redis://localhost:6379/0` |
| `PORT` | Port to listen on (default: `3000`) |

## Deployment (Northflank)

Build and deploy `server/Dockerfile`. Configure the environment variables above via Northflank secrets. Attach a managed Redis addon and set `REDIS_URL` accordingly. Run 2 replicas — Redis persistence (AOF) ensures tokens survive pod restarts.

## Supported regions

| Code | Country |
|------|---------|
| GB | United Kingdom |
| DE | Germany |
| ES | Spain |
| JP | Japan |
| IT | Italy |
| US | United States |
