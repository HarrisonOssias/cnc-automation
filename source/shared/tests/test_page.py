from datetime import date

import pytest

from forge_core import EMPTY, Bundle, Job, SlotState, compute_page, compute_slots

# The production screenshot from the spec, slot by slot (index 1..31).
SCREENSHOT_TABLE = [
    37, 27, EMPTY, 77, 108, 312, 120, 130, EMPTY,  # 1-9
    99,  # 10 (core)
    327, 350, EMPTY, 150, 321, 325, 366, 58, 295, EMPTY, EMPTY, EMPTY,  # 11-22
    100, 159, 15, 16, 17, 18, 19, 20, 90,  # 23-31 (core)
]
CORE = {10: 99, 23: 100, 24: 159, 25: 15, 26: 16, 27: 17, 28: 18, 29: 19, 30: 20, 31: 90}
REQUIRED = [99, 120, 130, 150, 20, 29, 113, 283]
MEASURED = [t for t in SCREENSHOT_TABLE if t != EMPTY]


def by_index(slots):
    return {s.index: s for s in slots}


def test_reproduces_production_screenshot():
    slots, unplaced = compute_slots(SCREENSHOT_TABLE, CORE, REQUIRED, MEASURED)
    s = by_index(slots)

    # Orange pills: missing tools go to the first non-empty, unneeded indexes (3 is empty, skipped).
    assert [(i, s[i].swap_to) for i in (1, 2, 4)] == [(1, 29), (2, 113), (4, 283)]
    assert s[1].state is SlotState.IDLE  # the slot stays blue; only the pill is orange
    assert s[3].state is SlotState.EMPTY and s[3].swap_to is None

    # Green IN USE in the other columns.
    assert {i for i, x in s.items() if x.state is SlotState.IN_USE} == {7, 8, 14}

    # Core column: all correct, IN USE on 10 (T99) and 30 (T20).
    assert all(s[i].state is SlotState.CORE_OK for i in CORE)
    assert {i for i in CORE if s[i].in_use} == {10, 30}

    assert {i for i, x in s.items() if x.state is SlotState.EMPTY} == {3, 9, 13, 20, 21, 22}
    assert unplaced == []
    assert s[1].measured and not s[3].measured


def test_nothing_to_swap_when_all_tools_loaded():
    slots, unplaced = compute_slots(SCREENSHOT_TABLE, CORE, [120, 99], MEASURED)
    assert all(s.swap_to is None for s in slots)
    assert unplaced == []


def test_core_index_missing_its_tool_is_red_and_names_it():
    table = list(SCREENSHOT_TABLE)
    table[9] = EMPTY  # index 10 should hold T99
    s = by_index(compute_slots(table, CORE, REQUIRED, MEASURED)[0])
    assert s[10].state is SlotState.CORE_WRONG
    assert s[10].swap_to == 99
    assert not s[10].in_use


def test_core_index_with_wrong_tool_is_red():
    table = list(SCREENSHOT_TABLE)
    table[30] = 555  # index 31 should hold T90
    s = by_index(compute_slots(table, CORE, [], MEASURED)[0])
    assert s[31].state is SlotState.CORE_WRONG and s[31].swap_to == 90


def test_needed_tool_in_wrong_core_index_still_needs_a_slot():
    table = list(SCREENSHOT_TABLE)
    table[30] = 29  # T29 is needed but sits in core index 31, which gets cleared
    slots, _ = compute_slots(table, CORE, [29], MEASURED)
    s = by_index(slots)
    assert s[31].state is SlotState.CORE_WRONG
    assert s[1].swap_to == 29


def test_falls_back_to_empty_slots_when_no_unneeded_tools():
    table = [EMPTY] * 31
    table[0], table[1] = 1, 2  # both needed
    slots, unplaced = compute_slots(table, {}, [1, 2, 3, 4], [])
    s = by_index(slots)
    assert (s[3].swap_to, s[4].swap_to) == (3, 4)
    assert unplaced == []


def test_overflow_reports_unplaced_tools():
    table = list(range(1, 32))  # every slot holds a needed tool
    slots, unplaced = compute_slots(table, {}, list(range(1, 32)) + [40, 41], [])
    assert all(s.swap_to is None for s in slots)
    assert unplaced == [40, 41]


def test_empty_machine():
    slots, unplaced = compute_slots([EMPTY] * 31, {}, [5], [])
    assert slots[0].swap_to == 5
    assert all(s.state is SlotState.EMPTY for s in slots)
    assert unplaced == []


@pytest.mark.parametrize(
    "table",
    [[EMPTY] * 30, [EMPTY] * 32, [7, 7] + [EMPTY] * 29, [0] + [EMPTY] * 30],
    ids=["too-short", "too-long", "duplicate-tool", "zero-tool"],
)
def test_rejects_invalid_tool_tables(table):
    with pytest.raises(ValueError):
        compute_slots(table, {}, [], [])


def test_compute_page_uses_first_job_and_queues_the_rest():
    job = Job(
        id=1, order_number="1616", line_number=1, part_number="BRKT-303", quantity=30,
        material="303 SUS", machinist="Miraj", due_date=date(2026, 11, 23), required_tools=REQUIRED,
    )
    later = job.model_copy(update={"id": 2, "line_number": 2, "required_tools": [1]})
    bundle = Bundle(machine_id="m1", version=1, core_tools=CORE, measured_tools=MEASURED, jobs=[job, later])
    page = compute_page(SCREENSHOT_TABLE, bundle)
    assert page.job == job and page.up_next == [later]
    assert by_index(page.slots)[1].swap_to == 29


def test_compute_page_without_jobs_shows_no_swaps():
    bundle = Bundle(machine_id="m1", version=1, core_tools=CORE, measured_tools=[], jobs=[])
    page = compute_page(SCREENSHOT_TABLE, bundle)
    assert page.job is None
    assert all(s.swap_to is None for s in page.slots)
