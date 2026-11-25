"""Load demo parts, files and jobs through the backend API, the same way the office web app does.

    .venv/bin/python demo/load.py                    # reads demo/jobs.json
    .venv/bin/python demo/load.py other/jobs.json --api http://localhost:8000

File paths in jobs.json are relative to the json file. Missing files are skipped with a warning.
Run it once after a fresh `docker compose down -v && docker compose up`; running it again adds the jobs again.
"""

import argparse
import json
import mimetypes
from datetime import date, timedelta
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec", nargs="?", default=Path(__file__).with_name("jobs.json"), type=Path)
    parser.add_argument("--api", default="http://localhost:8000")
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())

    with httpx.Client(base_url=args.api, timeout=60) as api:
        for part in spec["parts"]:
            files = []
            for rel in part.get("files", []):
                path = args.spec.parent / rel
                if not path.exists():
                    print(f"  ! missing {rel}, skipped")
                    continue
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                files.append(("files", (path.name, path.read_bytes(), content_type)))
            form = {"material": part["material"], "tools": ", ".join(f"T{t}" for t in part["tools"])}
            if "cycle_s" in part:
                form["cycle_s"] = str(part["cycle_s"])
            r = api.put(f"/parts/{part['part_number']}", data=form, files=files or None)
            r.raise_for_status()
            print(f"part {part['part_number']}: {len(r.json()['files'])} file(s) in S3")

        for job in spec["jobs"]:
            body = {k: job[k] for k in ("order_number", "line_number", "part_number", "quantity", "machinist")}
            body["due_date"] = (date.today() + timedelta(days=job.get("due_in_days", 3))).isoformat()
            machine_id = job.get("machine_id", spec["machine_id"])
            r = api.post(f"/machines/{machine_id}/jobs", json=body)
            r.raise_for_status()
            print(f"job {job['order_number']} - #{job['line_number']} queued on {machine_id}")


if __name__ == "__main__":
    main()
