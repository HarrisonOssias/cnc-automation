"""Part sessions as a projection of machine events (backend/sessions.py, the pure part)."""

from datetime import UTC, datetime, timedelta

from backend.sessions import apply_event, close, new_session

T0 = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def started(quantity: int = 3):
    return new_session("s1", "m1", T0, {
        "job_id": 7, "order": "1616 - #1", "part_number": "BRKT-303", "quantity": quantity,
        "expected_cycle_s": 240, "tool_seq": 4, "tools": [1, 2, 3],
    })


def run(row, *events):
    for seconds, event_type, data in events:
        apply_event(row, event_type, at(seconds), data)
    return row


def part(seconds, made, cycle_s):
    return [(seconds - cycle_s, "cycle_start", {}), (seconds, "cycle_stop", {}),
            (seconds, "part_complete", {"made": made, "quantity": 3, "cycle_s": cycle_s})]


def test_a_session_starts_in_setup():
    row = started()
    assert (row.state, row.made, row.order_ref, row.expected_cycle_s) == ("setup", 0, "1616 - #1", 240)


def test_time_is_credited_to_each_state():
    # 10 min of setup, two parts with a 30 s load in between, then 60 s idle before Job complete.
    row = run(started(), *part(600 + 230, 1, 230), *part(830 + 30 + 250, 2, 250),
              (1110 + 60, "session_end", {"reason": "completed", "made": 2}))
    assert row.first_cycle_at == at(600)
    assert (row.setup_s, row.cut_s, row.idle_s) == (600, 480, 90)
    assert (row.made, row.cycles, row.avg_cycle_s, row.min_cycle_s, row.max_cycle_s) == (2, 2, 240, 230, 250)
    assert (row.state, row.end_reason, row.ended_at) == ("ended", "completed", at(1170))


def test_feed_hold_and_alarm_time():
    row = run(
        started(),
        (60, "cycle_start", {}), (100, "feed_hold", {}), (130, "resume", {}),
        (160, "cycle_stop", {"aborted": "alarm"}), (160, "alarm", {"code": "1010", "message": "Door open"}),
    )
    assert (row.state, row.alarm, row.alarms) == ("alarm", "1010 Door open", 1)
    run(row, (400, "alarm_clear", {}))
    assert (row.state, row.alarm) == ("idle", None)
    assert (row.cut_s, row.hold_s, row.alarm_s) == (70, 30, 240)


def test_an_alarm_during_setup_returns_to_setup():
    row = run(started(), (30, "alarm", {"code": "108", "message": "Servo overload"}), (90, "alarm_clear", {}))
    assert row.state == "setup" and (row.setup_s, row.alarm_s) == (30, 60)


def test_rest_close_then_late_events():
    """The completion upload can close the session before the consumer reads the last events."""
    row = run(started(), *part(300, 1, 240))
    assert close(row, at(500), "completed")
    run(row, *part(450, 2, 140), (500, "session_end", {"reason": "completed", "made": 2}), (900, "cycle_start", {}))
    assert (row.made, row.cycles, row.end_reason, row.state) == (2, 2, "completed", "ended")
    assert row.first_cycle_at == at(60)  # nothing after the close changed its timings
    assert not close(row, at(600), "canceled")  # closing twice changes nothing
    assert row.end_reason == "completed"


def test_cancel_closes_from_the_current_state():
    row = run(started(), (100, "cycle_start", {}))
    close(row, at(160), "canceled")
    assert (row.cut_s, row.end_reason, row.alarm) == (60, "canceled", None)
