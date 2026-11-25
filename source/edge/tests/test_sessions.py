"""Part sessions on the Pi: lifecycle, session_id on every event, machine state, and the simulator."""

import asyncio
import json
import random
from datetime import date

import pytest

from agent.agent import SCREENSHOT_TABLE, Agent, Config, ImageCache, StubHaasSource, Store
from agent.sim import MachineSim
from forge_core import Bundle, Job, MachineState


class FakeMqtt:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, topic, payload, qos=0, retain=False):
        if topic.endswith("/events"):
            self.events.append(json.loads(payload))

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


def job(job_id: int, quantity: int = 3, cycle_s: int | None = 240) -> Job:
    return Job(
        id=job_id, order_number=str(1600 + job_id), line_number=1, part_number=f"P-{job_id}", quantity=quantity,
        material="6061", machinist="Miraj", due_date=date(2026, 12, 1), required_tools=[99], cycle_s=cycle_s,
    )


def bundle(*jobs: Job) -> Bundle:
    return Bundle(machine_id="m1", version=1, core_tools={}, measured_tools=[], jobs=list(jobs))


def make_agent(tmp_path) -> Agent:
    agent = Agent(Config(machine_id="m1"), Store(tmp_path / "agent.db"), StubHaasSource(), ImageCache(tmp_path / "img"))
    agent.mqtt = FakeMqtt()
    return agent


def set_bundle(agent: Agent, b: Bundle) -> None:
    agent.store.save_bundle(b)
    agent.sync_session()


@pytest.fixture
def agent(tmp_path):
    a = make_agent(tmp_path)
    a.store.record_tool_table(list(SCREENSHOT_TABLE))
    return a


def test_no_bundle_yet_means_no_session(agent):
    agent.sync_session()
    assert agent.store.open_session() is None
    assert agent.mqtt.events == []


def test_session_opens_for_the_current_job_and_stamps_every_event(agent):
    set_bundle(agent, bundle(job(1), job(2)))
    session = agent.store.open_session()
    assert session.job_id == 1
    start = agent.mqtt.events[-1]
    assert start["type"] == "session_start" and start["session_id"] == session.session_id
    assert start["data"]["quantity"] == 3 and start["data"]["expected_cycle_s"] == 240
    assert start["data"]["tools"] == SCREENSHOT_TABLE

    agent.record_tool_table([29] + SCREENSHOT_TABLE[1:])
    assert agent.mqtt.events[-1]["type"] == "tool_change"
    assert agent.mqtt.events[-1]["session_id"] == session.session_id
    assert agent.page().session.state is MachineState.SETUP


def test_restart_resumes_the_open_session(agent, tmp_path):
    set_bundle(agent, bundle(job(1)))
    first = agent.store.open_session().session_id

    restarted = make_agent(tmp_path)  # same SQLite file
    restarted.sync_session()
    assert restarted.store.open_session().session_id == first
    assert restarted.mqtt.events == []  # no second session_start


def test_job_complete_closes_the_session_and_opens_the_next(agent):
    set_bundle(agent, bundle(job(1), job(2)))
    first = agent.store.open_session().session_id
    agent.mqtt.events.clear()

    agent.complete_job(1)
    assert agent.mqtt.types() == ["job_complete", "session_end", "session_start"]
    complete, end, start = agent.mqtt.events
    assert complete["session_id"] == end["session_id"] == first
    assert end["data"]["reason"] == "completed"
    assert start["session_id"] != first and start["data"]["job_id"] == 2
    assert agent.alerts[-1].text.startswith("Session closed: 0 made")


def test_a_job_that_leaves_the_bundle_was_canceled(agent):
    set_bundle(agent, bundle(job(1), job(2)))
    agent.mqtt.events.clear()
    set_bundle(agent, bundle(job(2)))  # the office canceled job 1
    end, start = agent.mqtt.events
    assert end["type"] == "session_end" and end["data"]["reason"] == "canceled"
    assert start["type"] == "session_start" and start["data"]["job_id"] == 2


def test_empty_queue_tool_changes_have_no_session(agent):
    set_bundle(agent, bundle())
    agent.record_tool_table([29] + SCREENSHOT_TABLE[1:])
    assert agent.mqtt.events[-1]["type"] == "tool_change"
    assert agent.mqtt.events[-1]["session_id"] is None
    with pytest.raises(ValueError, match="no job"):
        agent.cycle_start()


def test_cycles_count_parts_until_the_quantity_is_made(agent):
    set_bundle(agent, bundle(job(1, quantity=2)))
    agent.mqtt.events.clear()

    agent.cycle_start()
    assert agent.page().session.state is MachineState.CUTTING
    assert agent.cycle_stop(machine_s=230) == 1
    assert agent.page().session.state is MachineState.IDLE
    agent.cycle_start()
    assert agent.cycle_stop(machine_s=250) == 2

    assert agent.mqtt.types() == [
        "cycle_start", "cycle_stop", "part_complete", "cycle_start", "cycle_stop", "part_complete", "quantity_reached",
    ]
    session = agent.page().session
    assert (session.made, session.last_cycle_s, session.avg_cycle_s) == (2, 250, 240)
    assert agent.alerts[-1].text == "All 2 made: tap Job complete"


def test_an_alarm_loses_the_running_cycle(agent):
    set_bundle(agent, bundle(job(1)))
    agent.cycle_start()
    agent.raise_alarm("1010", "Door open")
    assert agent.mqtt.events[-2]["type"] == "cycle_stop" and agent.mqtt.events[-2]["data"]["aborted"] == "alarm"
    assert agent.page().session.state is MachineState.ALARM
    assert agent.store.open_session().made == 0
    with pytest.raises(ValueError, match="alarm"):
        agent.cycle_start()
    agent.clear_alarm()
    agent.cycle_start()  # allowed again


def test_feed_hold_and_resume(agent):
    set_bundle(agent, bundle(job(1)))
    agent.cycle_start()
    agent.feed_hold()
    assert agent.page().session.state is MachineState.FEED_HOLD
    agent.resume()
    assert agent.page().session.state is MachineState.CUTTING
    assert agent.mqtt.types()[-2:] == ["feed_hold", "resume"]


def test_simulator_runs_the_job_to_its_quantity_then_stops(agent):
    set_bundle(agent, bundle(job(1, quantity=3, cycle_s=240)))

    async def scenario():
        sim = MachineSim(agent, random.Random(1))
        sim.run(speed=100_000)  # 240 s cycles in ~2 ms
        for _ in range(500):
            await asyncio.sleep(0.005)
            if not sim.running:
                break
        return sim

    sim = asyncio.run(scenario())
    assert not sim.running
    session = agent.page().session
    assert session.made == 3
    assert 240 * 0.85 <= session.avg_cycle_s <= 240 * 1.15  # reported in machine time, not wall time
    assert agent.mqtt.types().count("part_complete") == 3 and agent.mqtt.types()[-1] == "quantity_reached"
    with pytest.raises(ValueError, match="all 3 are made"):
        sim.finish_part()


def test_simulator_finish_part_runs_one_whole_cycle(agent):
    set_bundle(agent, bundle(job(1, quantity=5)))

    async def scenario():
        sim = MachineSim(agent, random.Random(1))
        return sim.finish_part()

    assert asyncio.run(scenario()) == 1
    assert agent.mqtt.types()[-3:] == ["cycle_start", "cycle_stop", "part_complete"]


def test_simulator_stops_on_alarm_and_when_the_session_ends(agent):
    set_bundle(agent, bundle(job(1, quantity=50), job(2, quantity=50)))

    async def scenario():
        sim = MachineSim(agent, random.Random(1))
        sim.run(speed=1)  # slow: a cycle is in progress when we act
        await asyncio.sleep(0.01)
        assert agent.cycle_started_at is not None
        sim.alarm("1010")
        await asyncio.sleep(0.01)
        assert not sim.running and agent.cycle_started_at is None

        sim.clear_alarm()
        sim.run(speed=1)
        await asyncio.sleep(0.01)
        agent.complete_job(1)  # the machinist taps Job complete mid-cycle
        await asyncio.sleep(0.01)
        return sim

    sim = asyncio.run(scenario())
    assert not sim.running  # the next job needs its setup first
    assert agent.page().session.job_id == 2 and agent.page().session.state is MachineState.SETUP
