"""
ASGI entry point for Fly.io: wraps server.py's MCP app with an api_key auth
gate, mirroring the proxy.cjs pattern used by garmin-connect-mcp. server.py
itself is not modified for this purpose (it exposes `mcp` and `ingest_app`).

Also exposes /admin/upload/{filename} and /admin/reload (both gated by the
same api_key) so sync_to_fly.sh can seed/update the Parquet files on the Fly
volume over plain HTTPS — SSH/SFTP tunnels to Fly are blocked on this network.

/ingest (Health Auto Export webhook, defined in server.py) is intentionally
NOT behind this Worker-facing api_key gate: it has its own auth (x-api-key
header or api_key query param, checked against INTERNAL_SECRET) since the
iOS app calls Fly directly and can only set a header, not our query param.
"""

import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from server import _cache, ingest_app, mcp

API_KEY = os.environ.get("API_KEY")
if not API_KEY:
    print("ERROR: API_KEY env var is required", file=sys.stderr)
    sys.exit(1)

DATA_DIR = Path(os.environ.get("APPLE_HEALTH_DATA_DIR", "/data"))


async def upload_file(request: Request) -> PlainTextResponse:
    if request.query_params.get("api_key") != API_KEY:
        return PlainTextResponse("Unauthorized", status_code=401)
    filename = request.path_params["filename"]
    if "/" in filename or "\\" in filename or filename.startswith("."):
        return PlainTextResponse("Invalid filename", status_code=400)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / filename
    size = 0
    with open(dest, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
    return PlainTextResponse(f"Saved {filename} ({size} bytes)")


async def reload_data(request: Request) -> JSONResponse:
    if request.query_params.get("api_key") != API_KEY:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _cache.clear()
    return JSONResponse({"ok": True, "reloaded": True})


mcp_app = mcp.streamable_http_app()
admin_app = Starlette(routes=[
    Route("/admin/upload/{filename}", upload_file, methods=["PUT"]),
    Route("/admin/reload", reload_data, methods=["POST"]),
])


class RootApp:
    """Manual ASGI router (not Starlette Mount, which breaks the mcp app's
    lifespan/task-group init). Routes:
      /ingest*  -> server.py's ingest_app, own auth, no outer api_key gate
      /admin/*  -> admin_app, requires ?api_key=
      everything else (incl. /mcp) -> mcp_app, requires ?api_key=
    """

    def __init__(self, mcp_app: object, admin_app: object, ingest_app: object, api_key: str) -> None:
        self.mcp_app = mcp_app
        self.admin_app = admin_app
        self.ingest_app = ingest_app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # lifespan and any other scope type: forward to mcp_app so its startup runs
            await self.mcp_app(scope, receive, send)
            return

        if scope["path"].startswith("/ingest"):
            await self.ingest_app(scope, receive, send)
            return

        params = dict(parse_qsl(scope.get("query_string", b"").decode()))
        if params.get("api_key") != self.api_key:
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        if scope["path"].startswith("/admin/"):
            await self.admin_app(scope, receive, send)
            return

        await self.mcp_app(scope, receive, send)


app = RootApp(mcp_app, admin_app, ingest_app, API_KEY)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
