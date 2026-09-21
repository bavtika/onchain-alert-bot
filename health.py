"""HTTP health and Prometheus metrics for container orchestration."""

from __future__ import annotations

import time
from typing import Any

from aiohttp import web

from utils.logger import log

_start_time = time.time()
_state: dict[str, Any] = {
    "ready": False,
    "strategies": 0,
}


def set_ready(ready: bool, strategies: int = 0) -> None:
    _state["ready"] = ready
    _state["strategies"] = strategies


async def handle_healthz(_request: web.Request) -> web.Response:
    """Liveness: process is up."""
    return web.json_response(
        {
            "status": "ok",
            "uptime_seconds": round(time.time() - _start_time, 1),
        }
    )


async def handle_readyz(_request: web.Request) -> web.Response:
    """Readiness: bot initialized and strategies running."""
    if not _state["ready"]:
        return web.json_response({"status": "not_ready"}, status=503)
    return web.json_response(
        {
            "status": "ready",
            "strategies": _state["strategies"],
            "uptime_seconds": round(time.time() - _start_time, 1),
        }
    )


async def handle_metrics(_request: web.Request) -> web.Response:
    """Minimal Prometheus text exposition (no extra dependency)."""
    uptime = time.time() - _start_time
    ready = 1 if _state["ready"] else 0
    body = "\n".join(
        [
            "# HELP onchain_bot_up 1 if process is running",
            "# TYPE onchain_bot_up gauge",
            "onchain_bot_up 1",
            "# HELP onchain_bot_ready 1 if strategies are initialized",
            "# TYPE onchain_bot_ready gauge",
            f"onchain_bot_ready {ready}",
            "# HELP onchain_bot_strategies Number of active strategies",
            "# TYPE onchain_bot_strategies gauge",
            f"onchain_bot_strategies {_state['strategies']}",
            "# HELP onchain_bot_uptime_seconds Process uptime in seconds",
            "# TYPE onchain_bot_uptime_seconds counter",
            f"onchain_bot_uptime_seconds {uptime:.1f}",
            "",
        ]
    )
    return web.Response(text=body, content_type="text/plain; version=0.0.4")


async def start_health_server(port: int = 8080) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/readyz", handle_readyz)
    app.router.add_get("/metrics", handle_metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health server listening on :{port} (/healthz /readyz /metrics)")
    return runner
