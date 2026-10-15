"""Outbox relay: publishes unpublished outbox rows to RabbitMQ, then marks them published.

    python -m app.graph.relay

At-least-once: a crash between publish and commit republishes the batch, and the
consumer is idempotent. SKIP LOCKED lets several relays run side by side.
"""

import json
import logging
import time
from datetime import UTC, datetime

import pika
from pika.adapters.blocking_connection import BlockingChannel
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.models import OutboxEvent
from app.db.session import session_scope
from app.graph import broker

log = logging.getLogger(__name__)


def message_for(event: OutboxEvent) -> dict:
    return {
        "event_id": event.id,
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "payload": event.payload,
        "created_at": event.created_at.replace(tzinfo=UTC).isoformat() if event.created_at else None,
    }


def publish_pending(channel: BlockingChannel, batch: int = 100) -> int:
    with session_scope() as session:
        events = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        ).all()
        for event in events:
            channel.basic_publish(
                broker.EXCHANGE,
                routing_key=event.event_type,
                body=json.dumps(message_for(event)),
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent,
                    content_type="application/json",
                    message_id=str(event.id),
                ),
                mandatory=True,
            )
            event.published_at = datetime.now(UTC).replace(tzinfo=None)
        return len(events)


def main() -> None:
    setup_logging()
    poll = get_settings().relay_poll_seconds
    while True:
        connection = broker.connect()
        try:
            channel = connection.channel()
            channel.confirm_delivery()  # basic_publish raises unless the broker confirms
            broker.declare(channel)
            log.info("Outbox relay started")
            while True:
                published = publish_pending(channel)
                if published:
                    log.info("Published %d event(s)", published)
                else:
                    connection.sleep(poll)  # keeps heartbeats flowing while idle
        except (pika.exceptions.AMQPError, OSError) as e:
            log.warning("Relay connection lost: %s; reconnecting", e)
            time.sleep(2)
        finally:
            if connection.is_open:
                connection.close()


if __name__ == "__main__":
    main()
