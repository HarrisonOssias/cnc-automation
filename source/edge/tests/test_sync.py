"""The Pi's sync loop: a bundle_changed notice must never wait for the idle timeout."""

import asyncio

import httpx

from agent.agent import SCREENSHOT_TABLE, Agent, Config, ImageCache, StubHaasSource, Store
from forge_core import Bundle

from test_sessions import FakeMqtt


def test_notice_during_a_bundle_fetch_triggers_another_fetch_at_once(tmp_path):
    """Seen in a rehearsal: a reorder's notice landed mid-pass and the Pi showed the old order for 15 s."""
    agent = Agent(Config(machine_id="m1"), Store(tmp_path / "agent.db"), StubHaasSource(), ImageCache(tmp_path / "i"))
    agent.mqtt = FakeMqtt()
    agent.store.record_tool_table(list(SCREENSHOT_TABLE))
    fetches = 0

    def backend(request: httpx.Request) -> httpx.Response:
        nonlocal fetches
        if request.url.path.endswith("/bundle"):
            fetches += 1
            if fetches == 1:
                agent.mark_bundle_stale()  # the office reorders while this response is in flight
            bundle = Bundle(machine_id="m1", version=fetches, core_tools={}, measured_tools=[], jobs=[])
            return httpx.Response(200, json=bundle.model_dump(mode="json"))
        return httpx.Response(200, json={})

    async def run() -> None:
        agent.loop = asyncio.get_running_loop()
        agent.http = httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(backend))
        task = asyncio.create_task(agent.sync_loop())
        await asyncio.sleep(0.5)  # far less than the 15 s idle timeout
        task.cancel()

    asyncio.run(run())
    assert fetches == 2
