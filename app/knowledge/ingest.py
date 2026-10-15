"""Ingest the docs folder into the knowledge graph.

    python -m app.knowledge.ingest              # once
    python -m app.knowledge.ingest --watch 30   # keep re-scanning; only changed files are re-embedded

A document is re-embedded only when its content hash (or the embedding model) changes.
Files that disappear are removed from the graph.
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from neo4j import Driver

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.graph.client import ensure_constraints, get_driver
from app.knowledge import store
from app.knowledge.chunker import chunk
from app.knowledge.concepts import extract_concepts
from app.knowledge.documents import discover
from app.knowledge.embeddings import Embedder, get_embedder

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    seen: int = 0
    ingested: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0

    def __str__(self) -> str:
        return (
            f"{self.seen} document(s): {self.ingested} ingested ({self.chunks} chunks), "
            f"{self.unchanged} unchanged, {self.removed} removed"
        )


def ingest(root: Path, driver: Driver, embedder: Embedder, use_model_concepts: bool = False) -> IngestStats:
    s = get_settings()
    store.ensure_schema(driver, embedder.dimensions)
    store.sync_concepts(driver)
    known = store.stored_hashes(driver)
    stats = IngestStats()
    docs = discover(root)
    for doc in docs:
        stats.seen += 1
        if known.get(doc.uid) == f"{doc.content_hash}|{embedder.name}":
            stats.unchanged += 1
            continue
        chunks = chunk(doc.text, s.chunk_target_chars, s.chunk_overlap_chars)
        # Embed with the heading in front, so a chunk's section title contributes to its meaning
        embeddings = embedder.embed_documents([f"{doc.title} - {c.heading}\n\n{c.text}" for c in chunks])
        concepts = [extract_concepts(f"{c.heading}\n{c.text}", use_model_concepts) for c in chunks]
        store.write_document(driver, doc, chunks, embeddings, concepts, embedder.name)
        stats.ingested += 1
        stats.chunks += len(chunks)
        log.info("Ingested %s (%d chunks)", doc.uid, len(chunks))
    stats.removed = store.remove_documents(driver, {d.uid for d in docs})
    return stats


def main() -> int:
    setup_logging()
    s = get_settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=s.docs_path)
    parser.add_argument("--watch", type=int, default=s.knowledge_watch_seconds, help="re-scan interval in seconds")
    args = parser.parse_args()

    driver = get_driver()
    for attempt in range(30):
        try:
            driver.verify_connectivity()
            break
        except Exception:
            log.warning("Neo4j not reachable yet (attempt %d)", attempt + 1)
            time.sleep(3)
    ensure_constraints(driver)
    while True:
        try:
            stats = ingest(Path(args.path), driver, get_embedder(), s.knowledge_model_concepts)
            log.info("Knowledge ingest: %s", stats)
        except Exception:
            log.exception("Knowledge ingest failed")
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
