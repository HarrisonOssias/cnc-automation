"""The page logic: turn a raw tool table plus a bundle into what the operator sees.

Pure functions only (no I/O), so the same code runs on the Pi and the backend
and is easy to test.
"""

from collections.abc import Iterable, Mapping, Sequence

from forge_core.models import Bundle, PageState, Slot, SlotState

SLOT_COUNT = 31
EMPTY = -1


def validate_tool_table(tool_table: Sequence[int]) -> None:
    if len(tool_table) != SLOT_COUNT:
        raise ValueError(f"tool table must have {SLOT_COUNT} slots, got {len(tool_table)}")
    loaded = [t for t in tool_table if t != EMPTY]
    if any(t <= 0 for t in loaded):
        raise ValueError("tool numbers must be positive, or -1 for an empty slot")
    if len(loaded) != len(set(loaded)):
        raise ValueError("a tool can only be in one slot")


def compute_slots(
    tool_table: Sequence[int],
    core_tools: Mapping[int, int],
    required_tools: Iterable[int],
    measured_tools: Iterable[int],
) -> tuple[list[Slot], list[int]]:
    """Return every slot's display state, plus needed tools that have nowhere to go.

    Rules (from the spec):
    - A core index must hold its assigned tool. If not, it's red and names that tool.
    - Other indexes are green if their tool is needed, blue if not, purple if empty.
    - A needed tool that isn't loaded goes to the first non-empty index whose tool
      isn't needed, then to the first empty index. Several swaps can be needed.
    """
    validate_tool_table(tool_table)
    required = list(dict.fromkeys(required_tools))  # unique, order kept
    needed = set(required)
    measured = set(measured_tools)
    core_expected = set(core_tools.values())

    slots: list[Slot] = []
    usable: set[int] = set()  # needed tools that are loaded somewhere they can stay
    for index, raw in enumerate(tool_table, start=1):
        tool = None if raw == EMPTY else raw
        if index in core_tools:
            expected = core_tools[index]
            ok = tool == expected
            slots.append(
                Slot(
                    index=index,
                    tool=tool,
                    core=True,
                    state=SlotState.CORE_OK if ok else SlotState.CORE_WRONG,
                    measured=tool in measured,
                    in_use=ok and expected in needed,
                    swap_to=None if ok else expected,
                )
            )
            if ok:
                usable.add(tool)
            # A tool in the wrong core index is about to be pulled, so it doesn't count.
            continue

        if tool is None:
            state = SlotState.EMPTY
        elif tool in needed:
            state = SlotState.IN_USE
            usable.add(tool)
        else:
            state = SlotState.IDLE
        slots.append(
            Slot(
                index=index,
                tool=tool,
                core=False,
                state=state,
                measured=tool in measured,
                in_use=state is SlotState.IN_USE,
            )
        )

    # Core tools are handled by their own red slot, so they never need a swap slot.
    missing = [t for t in required if t not in usable and t not in core_expected]

    candidates = [s for s in slots if s.state is SlotState.IDLE] + [
        s for s in slots if s.state is SlotState.EMPTY
    ]
    for slot, tool in zip(candidates, missing):
        slot.swap_to = tool
    unplaced = missing[len(candidates) :]
    return slots, unplaced


def compute_page(tool_table: Sequence[int], bundle: Bundle) -> PageState:
    job = bundle.jobs[0] if bundle.jobs else None
    slots, unplaced = compute_slots(
        tool_table,
        bundle.core_tools,
        job.required_tools if job else [],
        bundle.measured_tools,
    )
    return PageState(
        machine_id=bundle.machine_id,
        slots=slots,
        job=job,
        up_next=bundle.jobs[1:],
        unplaced_tools=unplaced,
    )
