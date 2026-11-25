"""Telemetry consumer: reads the machine-event stream into Postgres (events + part sessions).

    python -m backend.telemetry

How events get here:
  Pi --MQTT forge/machines/<id>/events--> RabbitMQ MQTT plugin --> amq.topic exchange
     (routing key forge.machines.<id>.events) --binding--> stream "forge.machine-events"

A stream is an append-only log. Reading doesn't remove messages, so any number of consumers
(this feed, alerting, an archiver, OEE analytics) can each read it from their own position, and a
new one can replay history. This consumer stores the stream offset with every event and, on start,
resumes right after the last one it stored, so it catches up on whatever arrived while it was down.

Each event is stored and applied to its part session (backend/sessions.py) in one transaction, so a
crash between the two can't leave them out of step. Triggers on both tables NOTIFY the backend, which
pushes the change to the office (backend/live.py).

Scaling later: make it a super stream partitioned by machine_id (already in the routing key), with
one consumer per partition. Machine type or cell belong in topics or stream filters, not partitions.
"""

import json
import logging
import os
import time
from datetime import datetime

import pika
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from backend.db import MachineEvent, SessionLocal, migrate
from backend.sessions import project

STREAM = "forge.machine-events"
BINDING = "forge.machines.*.events"  # MQTT topic levels become dot-separated routing keys
AMQP_URL = os.environ.get("AMQP_URL", "amqp://forge:forge@localhost:5672/%2F")

log = logging.getLogger("telemetry")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def on_message(channel, method, properties, body: bytes) -> None:
    offset = properties.headers["x-stream-offset"]
    try:
        event = json.loads(body)
        values = dict(
            event_id=event["event_id"], machine_id=event["machine_id"], type=event["type"],
            at=datetime.fromisoformat(event["at"]), data=event.get("data", {}), stream_offset=offset,
            session_id=event.get("session_id"),
        )
    except (ValueError, KeyError, TypeError):
        log.warning("skipping malformed event at offset %d", offset)
    else:
        with SessionLocal() as session:
            stored = session.execute(
                insert(MachineEvent).values(**values).on_conflict_do_nothing().returning(MachineEvent.id)
            ).first()
            if stored:  # a redelivered event was stored (and projected) the first time
                project(session, MachineEvent(**values))
            session.commit()
        log.info("offset %d: %s %s", offset, values["machine_id"], values["type"])
    channel.basic_ack(method.delivery_tag)


def run() -> None:
    connection = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
    channel = connection.channel()
    channel.queue_declare(
        STREAM, durable=True,
        arguments={"x-queue-type": "stream", "x-max-age": "7D", "x-max-length-bytes": 1_000_000_000},
    )
    channel.queue_bind(STREAM, "amq.topic", routing_key=BINDING)
    channel.basic_qos(prefetch_count=200)  # streams require a prefetch limit and manual acks

    with SessionLocal() as session:
        last = session.scalar(select(func.max(MachineEvent.stream_offset)))
    start = "first" if last is None else last + 1
    log.info("reading %s from offset %s", STREAM, start)
    channel.basic_consume(STREAM, on_message, arguments={"x-stream-offset": start})
    channel.start_consuming()


def main() -> None:
    migrate()
    while True:
        try:
            run()
        except (pika.exceptions.AMQPError, OSError) as e:
            log.warning("broker unavailable (%s); retrying", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
