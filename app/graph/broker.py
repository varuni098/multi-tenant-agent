"""RabbitMQ topology shared by the outbox relay and the graph sync worker."""

import logging
import time

import pika
from pika.adapters.blocking_connection import BlockingChannel

from app.core.config import get_settings

log = logging.getLogger(__name__)

EXCHANGE = "tenant.events"  # topic exchange; routing key = event type
DLX = "tenant.events.dlx"
QUEUE = "graph-sync"
DLQ = "graph-sync.dlq"


def connect(retries: int = 30) -> pika.BlockingConnection:
    params = pika.URLParameters(get_settings().rabbitmq_url)
    params.heartbeat = 30
    for attempt in range(1, retries + 1):
        try:
            return pika.BlockingConnection(params)
        except pika.exceptions.AMQPConnectionError:
            log.warning("RabbitMQ not reachable (attempt %d/%d)", attempt, retries)
            time.sleep(min(2 * attempt, 10))
    raise RuntimeError("RabbitMQ is unreachable")


def declare(channel: BlockingChannel) -> None:
    channel.exchange_declare(EXCHANGE, exchange_type="topic", durable=True)
    channel.exchange_declare(DLX, exchange_type="fanout", durable=True)
    channel.queue_declare(DLQ, durable=True)
    channel.queue_bind(DLQ, DLX)
    channel.queue_declare(QUEUE, durable=True, arguments={"x-dead-letter-exchange": DLX})
    channel.queue_bind(QUEUE, EXCHANGE, routing_key="#")
