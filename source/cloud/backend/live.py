"""Postgres LISTEN -> browsers: the backend's half of the office's live updates.

One connection LISTENs on `forge_live` (the triggers in notify.sql send there) and hands every message
to each open `/live` SSE stream. The office opens one EventSource instead of polling three endpoints.

NOTIFY isn't durable: while this connection is down, messages are lost. So every (re)connect, here
and in the browser, starts from a full snapshot: after reconnecting to Postgres the hub tells every
stream to `resync`, and a browser that reconnects gets a fresh snapshot from `/live`.

One listener per backend replica. Fine for ~150 machines; fan-out to thousands of browsers would move
to Redis or a RabbitMQ stream.
"""

import asyncio
import json
import logging

import psycopg

from backend.db import DATABASE_URL

CHANNEL = "forge_live"
QUEUE_SIZE = 500
RESYNC = json.dumps({"kind": "resync"})

log = logging.getLogger("backend.live")


class Hub:
    """Fans NOTIFY payloads out to SSE subscribers. Each item is (machine_id or None, raw json)."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def publish(self, payload: str) -> None:
        try:
            machine_id = json.loads(payload).get("machine_id")
        except ValueError:
            return
        for queue in self.subscribers:
            if queue.full():  # a browser that can't keep up starts over from a snapshot
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait((None, RESYNC))
            else:
                queue.put_nowait((machine_id, payload))


hub = Hub()


def _dsn() -> str:
    return DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


async def listen() -> None:
    """Runs for the backend's lifetime; reconnects with a short backoff."""
    delay = 1.0
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=True) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")
                log.info("listening on %s", CHANNEL)
                delay = 1.0
                hub.publish(RESYNC)  # anything sent while we were disconnected is gone
                async for note in conn.notifies():
                    hub.publish(note.payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # Postgres restarting, network blip
            log.warning("LISTEN connection lost (%s); retrying in %.0f s", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
