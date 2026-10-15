"""GraphRAG retrieval: vector search + concept expansion + re-ranking, with citations.

1. Embed the query and take the nearest chunks from the vector index (this tenant + global).
2. Find the query's concepts and pull in chunks that MENTION them (one hop in the graph).
3. Re-rank everything by semantic similarity plus concept overlap.
"""

from dataclasses import dataclass, field
from typing import Any

from neo4j import Driver
from neo4j.exceptions import ClientError

from app.knowledge.concepts import match_concepts
from app.knowledge.embeddings import Embedder, cosine
from app.knowledge.store import VECTOR_INDEX

VECTOR_WEIGHT = 0.75
CONCEPT_WEIGHT = 0.25
EXCERPT_CHARS = 700


class IndexNotReady(Exception):
    pass


@dataclass
class Candidate:
    uid: str
    title: str
    heading: str
    path: str
    text: str
    tenant_id: str | None
    concepts: list[str]
    similarity: float  # cosine mapped to 0..1, like Neo4j's vector score
    via: set[str] = field(default_factory=set)  # "vector", "concept"


def rerank(candidates: list[Candidate], query_concepts: list[str], k: int) -> list[tuple[Candidate, float]]:
    """Score = 0.75 * similarity + 0.25 * share of the query's concepts the chunk mentions."""
    wanted = set(query_concepts)
    scored = []
    for c in candidates:
        overlap = len(wanted & set(c.concepts)) / len(wanted) if wanted else 0.0
        scored.append((c, VECTOR_WEIGHT * c.similarity + CONCEPT_WEIGHT * overlap))
    scored.sort(key=lambda cs: (-cs[1], cs[0].uid))
    return scored[:k]


_RETURN = """
    MATCH (d:Document)-[:HAS_CHUNK]->(node)
    OPTIONAL MATCH (node)-[:MENTIONS]->(k:Concept)
    RETURN node.uid AS uid, node.text AS text, node.heading AS heading, node.tenant_id AS tenant_id,
           d.title AS title, d.path AS path, collect(k.name) AS concepts, node.embedding AS embedding
"""


def _candidates(driver: Driver, tid: str, vec: list[float], concepts: list[str], pool: int) -> dict[str, Candidate]:
    found: dict[str, Candidate] = {}

    def add(rec: Any, similarity: float, via: str) -> None:
        cand = found.get(rec["uid"])
        if cand is None:
            cand = found[rec["uid"]] = Candidate(
                uid=rec["uid"],
                title=rec["title"],
                heading=rec["heading"],
                path=rec["path"],
                text=rec["text"],
                tenant_id=rec["tenant_id"],
                concepts=sorted(rec["concepts"]),
                similarity=similarity,
            )
        cand.via.add(via)

    try:
        vector_hits = driver.execute_query(
            f"""
            CALL db.index.vector.queryNodes('{VECTOR_INDEX}', $pool, $vec) YIELD node, score
            WHERE node.tenant_id IS NULL OR node.tenant_id = $tid
            WITH node, score
            {_RETURN}, score
            """,
            pool=pool,
            vec=vec,
            tid=tid,
        ).records
    except ClientError as e:
        raise IndexNotReady("The documentation index isn't ready yet.") from e
    for rec in vector_hits:
        add(rec, rec["score"], "vector")

    if concepts:
        concept_hits = driver.execute_query(
            f"""
            MATCH (node:DocChunk)-[:MENTIONS]->(c:Concept)
            WHERE c.name IN $concepts AND (node.tenant_id IS NULL OR node.tenant_id = $tid)
            WITH DISTINCT node
            {_RETURN}
            LIMIT $pool
            """,
            concepts=concepts,
            tid=tid,
            pool=pool,
        ).records
        for rec in concept_hits:
            add(rec, (1 + cosine(vec, rec["embedding"])) / 2, "concept")
    return found


def search(driver: Driver, embedder: Embedder, tid: str, query: str, k: int = 4) -> list[dict[str, Any]]:
    k = max(1, min(k, 8))
    vec = embedder.embed_query(query)
    concepts = match_concepts(query)
    candidates = _candidates(driver, tid, vec, concepts, pool=max(20, k * 5))
    results = []
    for rank, (c, score) in enumerate(rerank(list(candidates.values()), concepts, k), start=1):
        excerpt = c.text if len(c.text) <= EXCERPT_CHARS else c.text[:EXCERPT_CHARS].rsplit(" ", 1)[0] + " …"
        results.append(
            {
                "rank": rank,
                "citation": f"[{rank}] {c.title} › {c.heading}" if c.heading != c.title else f"[{rank}] {c.title}",
                "title": c.title,
                "heading": c.heading,
                "path": c.path,
                "scope": "tenant" if c.tenant_id else "product",
                "excerpt": excerpt,
                "score": round(score, 4),
                "concepts": c.concepts,
                "matched_concepts": sorted(set(concepts) & set(c.concepts)),
                "via": sorted(c.via),
            }
        )
    return results
