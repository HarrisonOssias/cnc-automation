"""Drive every simulated Pi at once, from the host. Each Pi's agent answers on its kiosk port.

    .venv/bin/python demo/fleet.py                   # status of every machine
    .venv/bin/python demo/fleet.py run --speed 20    # start the simulator on every machine with a job
    .venv/bin/python demo/fleet.py stop              # stop each one after its current part
    .venv/bin/python demo/fleet.py run --only 8081   # just one kiosk

The kiosk ports match docker-compose.yml (agent, agent-2, agent-3). A machine without a job, or in
alarm, is reported and skipped; the others carry on.
"""

import argparse

import httpx

KIOSKS = [8080, 8081, 8082]


def status(api: httpx.Client, port: int) -> str:
    page = api.get(f"http://localhost:{port}/api/page").raise_for_status().json()
    job, session, sim = page["job"], page.get("session"), page.get("sim") or {}
    if not job:
        return f"{page['machine_id']:<12} :{port}  no job"
    line = f"{page['machine_id']:<12} :{port}  {job['order_number']} - #{job['line_number']}"
    if session:
        line += f"  {session['state']:<9} {session['made']} of {session['quantity']} made"
    if sim.get("running"):
        line += f"  sim {sim['speed']:g}x"
    return line


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", default="status", choices=["status", "run", "stop"])
    parser.add_argument("--speed", type=float, default=20)
    parser.add_argument("--only", type=int, action="append", help="kiosk port; repeat for several")
    args = parser.parse_args()

    with httpx.Client(timeout=5) as api:
        for port in args.only or KIOSKS:
            try:
                if args.action != "status":
                    body = {"speed": args.speed} if args.action == "run" else None
                    r = api.post(f"http://localhost:{port}/api/sim/{args.action}", json=body)
                    if r.status_code == 409:
                        print(f":{port}  skipped: {r.json()['detail']}")
                        continue
                    r.raise_for_status()
                print(status(api, port))
            except httpx.HTTPError as e:
                print(f":{port}  unreachable ({e.__class__.__name__}); is that agent running?")


if __name__ == "__main__":
    main()
