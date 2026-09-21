import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import health
from notifier import format_alert


def test_format_alert_contains_strategy_and_title():
    msg = format_alert("oi_spike", "BTC OI +30%", "Open interest jumped", urgency="warning")
    assert "OI\\_SPIKE" in msg or "OI_SPIKE" in msg.replace("\\", "")
    assert "BTC OI" in msg.replace("\\", "")


def test_health_state_defaults():
    health.set_ready(False, 0)
    assert health._state["ready"] is False
    health.set_ready(True, 11)
    assert health._state["ready"] is True
    assert health._state["strategies"] == 11


@pytest.mark.asyncio
async def test_healthz_endpoint():
    app = web.Application()
    app.router.add_get("/healthz", health.handle_healthz)
    app.router.add_get("/readyz", health.handle_readyz)
    app.router.add_get("/metrics", health.handle_metrics)

    health.set_ready(False, 0)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

        resp = await client.get("/readyz")
        assert resp.status == 503

        health.set_ready(True, 3)
        resp = await client.get("/readyz")
        assert resp.status == 200
        data = await resp.json()
        assert data["strategies"] == 3

        resp = await client.get("/metrics")
        assert resp.status == 200
        text = await resp.text()
        assert "onchain_bot_up 1" in text
        assert "onchain_bot_ready 1" in text
