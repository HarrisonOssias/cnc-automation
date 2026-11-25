"""Agent entry point and the "handraulic" tool-change CLI.

    python -m agent serve               # run the agent (what systemd starts)
    python -m agent show                # print the current tool table and swaps
    python -m agent set-slot 4 283      # load T283 into index 4
    python -m agent clear-slot 3        # empty index 3
    python -m agent complete            # the machinist finishes the current job (same as the kiosk button)
    python -m agent cycle start         # the control starts / stops a cycle; a stop counts one part

Demo machine simulator (the same controls as the kiosk's ?demo=1 drawer):

    python -m agent sim run --speed 20  # run the current job, 20x faster than real time
    python -m agent sim stop            # stop after the current part
    python -m agent sim part            # finish one part now
    python -m agent sim hold | resume   # feed hold
    python -m agent sim alarm 1010      # raise an alarm (1010 door open, 108 servo overload, 1068 low coolant)
    python -m agent sim clear           # clear the alarm
"""

import argparse
import os

import httpx

AGENT_URL = os.environ.get("AGENT_URL", "http://127.0.0.1:8080")


def show() -> None:
    page = httpx.get(f"{AGENT_URL}/api/page").raise_for_status().json()
    job = page["job"]
    sync = page["sync"]
    print(f"machine {page['machine_id']}  backend {'online' if sync['online'] else 'OFFLINE'}"
          f"  pending uploads {sync['pending_uploads']}")
    session = page.get("session")
    if session:
        avg = f"  avg {session['avg_cycle_s']} s" if session["avg_cycle_s"] else ""
        alarm = f"  ({session['alarm']})" if session["alarm"] else ""
        print(f"session {session['id'][:8]}  {session['state']}{alarm}  {session['made']} of {session['quantity']} made{avg}")
    if job:
        print(f"job {job['order_number']} - #{job['line_number']}  {job['quantity']} x {job['material']}"
              f"  needs {', '.join(f'T{t}' for t in job['required_tools'])}")
    for s in page["slots"]:
        tool = f"T{s['tool']}" if s["tool"] else "--"
        note = f"-> T{s['swap_to']}" if s["swap_to"] else ("IN USE" if s["in_use"] else "")
        print(f"  {s['index']:>2} {'core' if s['core'] else '    '} {tool:>5}  {s['state']:<10} {note}")
    if page["unplaced_tools"]:
        print("no slot for:", ", ".join(f"T{t}" for t in page["unplaced_tools"]))


def set_slot(index: int, tool: int) -> None:
    r = httpx.put(f"{AGENT_URL}/api/slots/{index}", json={"tool": tool})
    if r.is_error:
        raise SystemExit(f"error: {r.json().get('detail', r.text)}")
    print(f"index {index} -> {'empty' if tool == -1 else f'T{tool}'} (seq {r.json()['seq']})")


def complete() -> None:
    job = httpx.get(f"{AGENT_URL}/api/page").raise_for_status().json()["job"]
    if not job:
        raise SystemExit("no current job")
    r = httpx.post(f"{AGENT_URL}/api/jobs/{job['id']}/complete")
    if r.is_error:
        raise SystemExit(f"error: {r.json().get('detail', r.text)}")
    print(f"job {job['order_number']} - #{job['line_number']} completed")


def post(path: str, body: dict | None = None) -> dict:
    r = httpx.post(f"{AGENT_URL}{path}", json=body)
    if r.is_error:
        raise SystemExit(f"error: {r.json().get('detail', r.text)}")
    return r.json()


def sim(action: str, arg: str | None, speed: float) -> None:
    if action == "run":
        post("/api/sim/run", {"speed": speed})
        print(f"simulator running at {speed:g}x")
    elif action == "stop":
        post("/api/sim/stop")
        print("simulator stops after this part")
    elif action == "part":
        print(f"part done ({post('/api/sim/part')['made']} made)")
    elif action in ("hold", "resume"):
        post(f"/api/sim/{action}")
        print("feed hold" if action == "hold" else "resumed")
    elif action == "alarm":
        print(f"alarm {post('/api/sim/alarm', {'code': arg or '1010'})['alarm']}")
    elif action == "clear":
        post("/api/sim/alarm/clear")
        print("alarm cleared")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the agent")
    sub.add_parser("show", help="print the current page")
    p = sub.add_parser("set-slot", help="put a tool in an index")
    p.add_argument("index", type=int)
    p.add_argument("tool", type=int)
    p = sub.add_parser("clear-slot", help="empty an index")
    p.add_argument("index", type=int)
    sub.add_parser("complete", help="complete the current job, like the kiosk button")
    p = sub.add_parser("cycle", help="cycle start/stop from the control (stubbed)")
    p.add_argument("state", choices=["start", "stop"])
    p = sub.add_parser("sim", help="demo machine simulator")
    p.add_argument("action", choices=["run", "stop", "part", "hold", "resume", "alarm", "clear"])
    p.add_argument("arg", nargs="?", help="alarm code, for `sim alarm`")
    p.add_argument("--speed", type=float, default=20, help="time compression for `sim run` (default 20x)")
    args = parser.parse_args()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "agent.agent:app",
            host=os.environ.get("AGENT_HOST", "127.0.0.1"),
            port=int(os.environ.get("AGENT_PORT", "8080")),
        )
    elif args.cmd == "show":
        show()
    elif args.cmd == "set-slot":
        set_slot(args.index, args.tool)
    elif args.cmd == "clear-slot":
        set_slot(args.index, -1)
    elif args.cmd == "complete":
        complete()
    elif args.cmd == "cycle":
        result = post(f"/api/cycle/{args.state}")
        print(f"cycle {args.state}" + (f" ({result['made']} made)" if "made" in result else ""))
    elif args.cmd == "sim":
        sim(args.action, args.arg, args.speed)


if __name__ == "__main__":
    main()
