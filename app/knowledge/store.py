"""Neo4j storage for documents, chunks, embeddings and concepts."""

from typing import Any

from neo4j import Driver, ManagedTransaction

from app.knowledge.chunker import Chunk
from app.knowledge.concepts import vocabulary
from app.knowledge.documents import SourceDoc

VECTOR_INDEX = "doc_chunk_embedding"


def ensure_schema(driver: Driver, dimensions: int) -> None:
    driver.execute_query(
        f"""
        CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS
        FOR (c:DocChunk) ON c.embedding
        OPTIONS {{indexConfig: {{`vector.dimensions`: {int(dimensions)}, `vector.similarity_function`: 'cosine'}}}}
        """
    )


def sync_concepts(driver: Driver) -> None:
    """Upsert the vocabulary as Concept nodes and link entity concepts to their graph nodes (ABOUT)."""
    rows = [{"name": c.name, "kind": c.kind, "terms": list(c.terms)} for c in vocabulary().values()]

    def work(tx: ManagedTransaction) -> None:
        tx.run("MATCH (c:Concept) WHERE NOT c.name IN $names DETACH DELETE c", names=[r["name"] for r in rows])
        tx.run("UNWIND $rows AS r MERGE (c:Concept {name: r.name}) SET c.kind = r.kind, c.terms = r.terms", rows=rows)
        tx.run("MATCH (:Concept)-[r:ABOUT]->() DELETE r")
        for c in vocabulary().values():
            label = c.about_label
            if label is None:
                continue
            key = "key" if label == "Feature" else "name"
            tx.run(
                f"MATCH (c:Concept {{name: $name}}) MATCH (e:{label} {{{key}: $name}}) MERGE (c)-[:ABOUT]->(e)",
                name=c.name,
            )

    with driver.session() as session:
        session.execute_write(work)


def stored_hashes(driver: Driver) -> dict[str, str]:
    """{document uid: content hash + embedder} for change detection."""
    recs = driver.execute_query("MATCH (d:Document) RETURN d.uid AS uid, d.content_hash AS h, d.embedder AS e").records
    return {r["uid"]: f"{r['h']}|{r['e']}" for r in recs}


def write_document(
    driver: Driver,
    doc: SourceDoc,
    chunks: list[Chunk],
    embeddings: list[list[float]],
    concepts: list[list[str]],
    embedder: str,
) -> None:
    """Replace one document and its chunks atomically."""
    rows: list[dict[str, Any]] = [
        {
            "uid": f"{doc.uid}#{c.index}",
            "chunk_index": c.index,
            "heading": c.heading,
            "text": c.text,
            "embedding": emb,
            "concepts": names,
        }
        for c, emb, names in zip(chunks, embeddings, concepts, strict=True)
    ]
    doc_props: dict[str, Any] = {
        "uid": doc.uid,
        "path": doc.path,
        "title": doc.title,
        "content_hash": doc.content_hash,
        "format": doc.format,
        "embedder": embedder,
    }
    if doc.tenant_id:
        doc_props["tenant_id"] = doc.tenant_id

    def work(tx: ManagedTransaction) -> None:
        tx.run("MATCH (d:Document {uid: $uid}) OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c) DETACH DELETE d, c", uid=doc.uid)
        tx.run("CREATE (d:Document) SET d = $props", props=doc_props)
        tx.run(
            """
            MATCH (d:Document {uid: $uid})
            UNWIND $rows AS r
            CREATE (d)-[:HAS_CHUNK]->(c:DocChunk)
            SET c.uid = r.uid, c.chunk_index = r.chunk_index, c.heading = r.heading, c.text = r.text,
                c.embedding = r.embedding, c.title = d.title, c.path = d.path
            FOREACH (_ IN CASE WHEN d.tenant_id IS NULL THEN [] ELSE [1] END | SET c.tenant_id = d.tenant_id)
            WITH c, r
            UNWIND r.concepts AS name
            MATCH (k:Concept {name: name})
            MERGE (c)-[:MENTIONS]->(k)
            """,
            uid=doc.uid,
            rows=rows,
        )

    with driver.session() as session:
        session.execute_write(work)


def remove_documents(driver: Driver, keep: set[str]) -> int:
    recs = driver.execute_query(
        """
        MATCH (d:Document) WHERE NOT d.uid IN $keep
        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c)
        WITH d, collect(c) AS chunks
        FOREACH (c IN chunks | DETACH DELETE c)
        DETACH DELETE d
        RETURN count(*) AS removed
        """,
        keep=sorted(keep),
    ).records
    return recs[0]["removed"] if recs else 0
