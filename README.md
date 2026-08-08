# Apple Health MCP Server

An [MCP](https://modelcontextprotocol.io/) server that exposes Apple Health data as tools queryable by LLMs (Claude, Copilot, etc.).

## Architecture

```
exportación.xml (Apple Health)
        │
        ▼
  preprocess.py  ──►  data/*.parquet   (run locally on Mac)
                              │
                              ▼  (sync_to_fly.sh, over plain HTTPS)
                     Fly.io: fjcabello-apple-health-mcp
                        ├─ wrapper.py (FastMCP app + api_key ASGI gate, port 8080)
                        └─ /data volume (persistent, 1GB) ─ loaded by server.py
                              │
                              ▼
         apple-health-fly-mcp-worker (Cloudflare Worker, OAuth gateway)
                              │
                    https://apple-health-fly-mcp-worker.fjcabello.workers.dev/mcp
```

The server loads Parquet files at startup (~1-2 s) and caches them in memory. If they are not found, it falls back to parsing the XML directly. `server.py` is never modified for deployment concerns — `wrapper.py` wraps it with the api_key auth gate and the admin sync endpoints (see "Deploy to Fly.io" below).

---

## Requirements

- Python 3.11+
- `pyarrow` (for Parquet support)

```bash
pip install -r requirements.txt
pip install pyarrow
```

### `requirements.txt`
```
mcp[cli]>=1.0.0
lxml>=5.0.0
pandas>=2.0.0
pyarrow>=15.0.0
uvicorn>=0.30.0
```

---

## Data update workflow

### 1. Export from iPhone

`Health → profile → Export All Health Data` → produces a ZIP containing `exportación.xml`. Unzip it so the file lands at `../apple_health_export/exportación.xml` (relative to this repo), or pass a custom path.

### 2. Sync to Fly.io

```bash
cd ~/Personal/apple_health/apple_health_mcp
./sync_to_fly.sh
```

This single script:
1. Runs `preprocess.py` (XML → `data/*.parquet`, ~20-25 files, one per metric type)
2. Uploads every Parquet file to the Fly volume via `PUT /admin/upload/<filename>?api_key=...` (plain HTTPS — SSH/SFTP tunnels to Fly are blocked on this network)
3. Calls `POST /admin/reload?api_key=...` to clear the in-memory cache, so the next tool call picks up the fresh data — no machine restart needed

Both `/admin/*` routes require the `API_KEY` Fly secret as a query param (see `wrapper.py`). The key is read from `.fly_secret_local` (gitignored) or the environment.

### 3. Automated sync via Health Auto Export

Instead of manually exporting the Apple Health ZIP, the [Health Auto Export](https://healthyapps.dev)
iOS app can push data automatically via a webhook, which upserts directly into
the Parquet files on the Fly volume (no XML/preprocess step needed). This is
defined in `server.py` (`ingest_app`) and wired into `wrapper.py`'s router.

**Endpoint (calls Fly directly, not through the Cloudflare Worker):**

```
POST https://fjcabello-apple-health-mcp.fly.dev/ingest
GET  https://fjcabello-apple-health-mcp.fly.dev/ingest/inspect   (last payload received, for debugging)
```

Authentication: header `x-api-key: <secret>` or query param `?api_key=<secret>`, checked against the `INTERNAL_SECRET` Fly secret (kept equal to `API_KEY` for simplicity — set both with the same value). This is intentionally separate from the Cloudflare Worker's OAuth, since the iOS app can only set a header, not go through the OAuth flow.

Configure **one automation per data type** in Health Auto Export (the app only allows one data type per automation): Health Metrics, Workouts, etc. — pick the metrics listed in `config.py` → `HK_TYPE_MAP`. Format JSON, export version v2, incremental date range, header `x-api-key` set to the shared secret.

Each `/ingest` call upserts only the metrics/workouts present in that payload (dedup by `startDate`, or `startDate` + `activityType` for workouts) and clears the in-memory cache so the next MCP tool call reloads fresh data — no restart needed.

## Deploy to Fly.io

The server runs as a Docker container on [Fly.io](https://fly.io) (app `fjcabello-apple-health-mcp`, region `ams`), fronted by `wrapper.py` (an ASGI router that gates `/mcp` and `/admin/*` behind `API_KEY`, and forwards `/ingest*` to `server.py`'s own-auth `ingest_app` — mirrors the `proxy.cjs` pattern in `garmin-connect-mcp`). Parquet files live on a persistent Fly volume mounted at `/data`, not baked into the image.

### First-time setup

```bash
fly auth login
fly apps create fjcabello-apple-health-mcp
fly volumes create apple_health_data --region ams --size 1 --app fjcabello-apple-health-mcp
fly secrets set API_KEY=$(openssl rand -hex 32) --app fjcabello-apple-health-mcp
fly secrets set INTERNAL_SECRET=<same value as API_KEY> --app fjcabello-apple-health-mcp
```

### Continuous deployment

[.github/workflows/deploy.yml](.github/workflows/deploy.yml) auto-deploys to Fly.io on every push to `main`, using a `FLY_API_TOKEN` repo secret (`fly tokens create deploy --app fjcabello-apple-health-mcp`).

> **Why CI instead of `fly deploy` locally:** on this network, Fly's remote "depot" builder and SSH/SFTP tunnels hang indefinitely / fail the WebSocket handshake (corporate SSL/proxy inspection). Deploying from a GitHub-hosted runner avoids this entirely.

### Seeding / updating data

See "Data update workflow" above — use `./sync_to_fly.sh`, not SSH.

---

## Running locally (development)

```bash
python server.py
# Listens on http://0.0.0.0:8001/mcp
```

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `APPLE_HEALTH_DATA_DIR` | `./data` | Directory containing `.parquet` files |
| `APPLE_HEALTH_EXPORT` | `../apple_health_export/exportación.xml` | XML fallback path |

---

## Available MCP tools

| Tool | Description |
|---|---|
| `health_summary` | Overview of all available data types, record counts and date ranges |
| `get_steps` | Daily step counts |
| `get_heart_rate` | Heart rate per day (mean / min / max) |
| `get_resting_heart_rate` | Daily resting heart rate |
| `get_sleep` | Sleep analysis by stage (Core, Deep, REM, Awake) |
| `get_workouts` | Workout sessions, filterable by type and date |
| `get_body_metrics` | Weight (kg), BMI, body fat %, lean body mass |
| `get_activity_energy` | Active/basal energy burned, distance, flights climbed |
| `get_nutrition` | Nutritional intake (calories, protein, carbs, fat) |
| `query_health_data` | Generic query for any available metric |

All tools accept optional `start_date` and `end_date` parameters in `YYYY-MM-DD` format.

### Available metrics for `query_health_data`

`steps`, `heart_rate`, `resting_hr`, `active_energy`, `basal_energy`, `distance_walk`, `distance_cycling`, `flights_climbed`, `sleep`, `body_mass`, `bmi`, `body_fat`, `lean_body_mass`, `walking_speed`, `walking_steadiness`, `dietary_energy`, `dietary_protein`, `dietary_carbs`, `dietary_fat`

---

## Cloud access (OAuth)

The Cloudflare Worker adds OAuth 2.0 authentication for remote access from Claude.ai or VS Code.

**Public URL:** `https://apple-health-fly-mcp-worker.fjcabello.workers.dev/mcp`

### VS Code configuration

```json
// .vscode/mcp.json
{
  "servers": {
    "apple-health-cloud": {
      "type": "http",
      "url": "https://apple-health-fly-mcp-worker.fjcabello.workers.dev/mcp"
    }
  }
}
```

### Worker source

See [fjcabello/apple-health-fly-mcp-worker](https://github.com/fjcabello/apple-health-fly-mcp-worker) (`../apple-health-fly-worker/` in the local workspace). Same multi-MCP OAuth gateway pattern as `garmin-connect-mcp`'s `mcp-oauth-gateway`.
