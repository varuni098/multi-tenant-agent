"""Live bridge: forwards committed tenant events from RabbitMQ to the per-tenant live channel (Redis).

    python -m app.live.bridge

It binds its own exclusive, non-durable queue to the tenant events exchange: live updates are
only useful while someone is watching, so nothing queues up while the bridge is down. It never
touches MySQL; the event already says what changed, and clients re-read through the API.
"""

import json
import logging
import time
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties

from app.core.logging import setup_logging
from app.graph import broker
from app.live.bus import get_bus

log = logging.getLogger(__name__)

# Fields a UI may see. Anything else in the payload (e.g. request ids) stays server-side.
_FIELDS = ("page_name", "key", "after", "actor")


def to_live_event(body: bytes) -> tuple[str, dict[str, Any]] | None:
    """(tenant_id, event) for a tenant event, or None for anything else."""
    try:
        msg = json.loads(body)
    except ValueError:
        return None
    tenant_id = msg.get("aggregate_id")
    if msg.get("aggregate_type") != "tenant" or not isinstance(tenant_id, str):
        return None
    payload = msg.get("payload") or {}
    event = {"type": msg.get("event_type"), "event_id": msg.get("event_id"), "at": msg.get("created_at")}
    event.update({k: payload.get(k) for k in _FIELDS})
    return tenant_id, event


def _on_message(channel: BlockingChannel, method: Basic.Deliver, _: BasicProperties, body: bytes) -> None:
    parsed = to_live_event(body)
    if parsed:
        tenant_id, event = parsed
        try:
            get_bus().publish(tenant_id, event)
            log.info("Live %s for tenant %s", event["type"], tenant_id)
        except Exception:  # a missed live update is harmless; clients also refresh on reconnect
            log.exception("Failed to publish live event")
    channel.basic_ack(method.delivery_tag)


def main() -> None:
    setup_logging()
    while True:
        try:
            connection = broker.connect()
            channel = connection.channel()
            channel.exchange_declare(broker.EXCHANGE, exchange_type="topic", durable=True)
            queue = channel.queue_declare("", exclusive=True, auto_delete=True).method.queue
            channel.queue_bind(queue, broker.EXCHANGE, routing_key="#")
            channel.basic_consume(queue, _on_message)
            log.info("Live bridge consuming %s", broker.EXCHANGE)
            channel.start_consuming()
        except (pika.exceptions.AMQPError, OSError) as e:
            log.warning("Live bridge connection lost: %s; reconnecting", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
