"""Graph sync worker: consumes tenant events and re-projects the affected tenant into Neo4j.

    python -m app.graph.sync

Each event is a notification: the worker re-reads that tenant from MySQL and replaces its
subgraph in one transaction. That makes processing idempotent and safe if events are
redelivered or reordered. A message that can't be processed goes to the dead-letter queue,
and the periodic reconcile repairs whatever it missed.
"""

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.graph import broker
from app.graph.client import ensure_constraints, get_driver
from app.graph.reconcile import project_tenant, reconcile

log = logging.getLogger(__name__)


class BadMessage(Exception):
    pass


def parse(body: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(body)
    except ValueError as e:
        raise BadMessage("not JSON") from e
    if msg.get("aggregate_type") != "tenant" or not isinstance(msg.get("aggregate_id"), str):
        raise BadMessage("not a tenant event")
    return msg


def lag_seconds(msg: dict[str, Any]) -> float | None:
    created = msg.get("created_at")
    if not created:
        return None
    return (datetime.now(UTC) - datetime.fromisoformat(created)).total_seconds()


def handle(body: bytes) -> float | None:
    """Apply one event. Returns the end-to-end lag (change committed -> graph updated)."""
    msg = parse(body)
    project_tenant(get_driver(), msg["aggregate_id"])
    lag = lag_seconds(msg)
    log.info("Projected tenant %s for event %s (lag %.2fs)", msg["aggregate_id"], msg.get("event_id"), lag or 0)
    return lag


def _on_message(channel: BlockingChannel, method: Basic.Deliver, _props: BasicProperties, body: bytes) -> None:
    try:
        handle(body)
    except Exception:
        log.exception("Event failed; dead-lettering it (reconcile will repair the graph)")
        channel.basic_nack(method.delivery_tag, requeue=False)
        return
    channel.basic_ack(method.delivery_tag)


def _reconcile_forever(interval: int) -> None:
    while True:
        time.sleep(interval)
        try:
            reconcile(get_driver(), fix=True)
        except Exception:
            log.exception("Periodic reconcile failed")


def main() -> None:
    setup_logging()
    settings = get_settings()
    driver = get_driver()
    for attempt in range(30):
        try:
            driver.verify_connectivity()
            break
        except Exception:
            log.warning("Neo4j not reachable yet (attempt %d)", attempt + 1)
            time.sleep(3)
    ensure_constraints(driver)
    reconcile(driver, fix=True)  # bootstrap: the graph starts as an exact copy of MySQL
    threading.Thread(target=_reconcile_forever, args=(settings.reconcile_interval_seconds,), daemon=True).start()

    while True:
        connection = broker.connect()
        try:
            channel = connection.channel()
            broker.declare(channel)
            channel.basic_qos(prefetch_count=10)
            channel.basic_consume(broker.QUEUE, _on_message)
            log.info("Graph sync consuming %s", broker.QUEUE)
            channel.start_consuming()
        except (pika.exceptions.AMQPError, OSError) as e:
            log.warning("Sync connection lost: %s; reconnecting", e)
            time.sleep(2)
        finally:
            if connection.is_open:
                connection.close()


if __name__ == "__main__":
    main()
