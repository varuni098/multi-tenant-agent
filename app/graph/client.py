"""Neo4j access: constraints, writing projections, dumping the graph."""

import logging
from collections import defaultdict
from functools import lru_cache
from typing import Any

from neo4j import Driver, GraphDatabase, ManagedTransaction

from app.core.config import get_settings
from app.graph.model import Graph, Rel
from app.graph.ontology import load_ontology

log = logging.getLogger(__name__)


@lru_cache
def get_driver() -> Driver:
    s = get_settings()
    return GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))


def ensure_constraints(driver: Driver) -> None:
    with driver.session() as session:
        for statement in load_ontology().constraint_statements():
            session.run(statement)


def _write_nodes(tx: ManagedTransaction, graph: Graph, refs: list[tuple[str, Any]]) -> None:
    classes = load_ontology().classes
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ref in refs:
        by_label[ref[0]].append(graph.nodes[ref])
    for label, rows in by_label.items():
        key = classes[label].key  # identifiers validated when the ontology was loaded
        tx.run(f"UNWIND $rows AS r MERGE (n:{label} {{{key}: r.{key}}}) SET n = r", rows=rows)


def _write_rels(tx: ManagedTransaction, rels: list[Rel]) -> None:
    classes = load_ontology().classes
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rels:
        groups[(r.type, r.start[0], r.end[0])].append({"a": r.start[1], "b": r.end[1]})
    for (rel_type, sl, el), rows in groups.items():
        sk, ek = classes[sl].key, classes[el].key
        tx.run(
            f"UNWIND $rows AS r MATCH (a:{sl} {{{sk}: r.a}}) MATCH (b:{el} {{{ek}: r.b}}) MERGE (a)-[:{rel_type}]->(b)",
            rows=rows,
        )


def write_reference(driver: Driver, graph: Graph) -> None:
    """Upsert shared nodes and replace edges among them, in one transaction."""
    classes = load_ontology().classes
    shared = [
        ref for ref in graph.nodes if classes[ref[0]].tenant_scoped is not True and "tenant_id" not in graph.nodes[ref]
    ]

    def work(tx: ManagedTransaction) -> None:
        # Edges owned by reference data are rebuilt; edges from tenant nodes are left alone
        tx.run("MATCH (:Role)-[r:MAY]->(:Action) DELETE r")
        tx.run("MATCH (:Plan)-[r:INCLUDES]->(:Feature) DELETE r")
        tx.run("MATCH (:SettingKey)-[r:REQUIRES]->(:Feature) DELETE r")
        tx.run("MATCH (:Template)-[r:DEFINES]->(s:Setting) DETACH DELETE s")
        for label in ("Role", "Action", "Plan", "Feature", "SettingKey", "Template"):
            key = classes[label].key
            keep = [ref[1] for ref in shared if ref[0] == label]
            tx.run(f"MATCH (n:{label}) WHERE NOT n.{key} IN $keep DETACH DELETE n", keep=keep)
        _write_nodes(tx, graph, shared)
        shared_set = set(shared)
        _write_rels(tx, [r for r in graph.rels if r.start in shared_set and r.end in shared_set])

    with driver.session() as session:
        session.execute_write(work)


def write_tenant(driver: Driver, tenant_id: str, graph: Graph | None) -> None:
    """Replace everything belonging to one tenant atomically. graph=None deletes the tenant."""
    owned = [] if graph is None else [ref for ref, p in graph.nodes.items() if p.get("tenant_id") == tenant_id]
    owned_set = set(owned)

    def work(tx: ManagedTransaction) -> None:
        tx.run("MATCH (n) WHERE n.tenant_id = $tid DETACH DELETE n", tid=tenant_id)
        if graph is not None:
            _write_nodes(tx, graph, owned)
            _write_rels(tx, [r for r in graph.rels if r.start in owned_set or r.end in owned_set])

    with driver.session() as session:
        session.execute_write(work)


def dump_graph(driver: Driver) -> Graph:
    """Read the whole graph into memory (fine at this scale; used by validation and drift checks)."""
    classes = load_ontology().classes
    g = Graph()
    ids: dict[str, tuple[str, Any]] = {}
    with driver.session() as session:
        for rec in session.run("MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props"):
            labels = rec["labels"]
            label = labels[0] if len(labels) == 1 else "|".join(sorted(labels))
            cls = classes.get(label)
            key = rec["props"].get(cls.key) if cls else rec["id"]
            ref = (label, key)
            g.nodes[ref] = dict(rec["props"])
            ids[rec["id"]] = ref
        for rec in session.run("MATCH (a)-[r]->(b) RETURN elementId(a) AS a, type(r) AS t, elementId(b) AS b"):
            g.rels.add(Rel(rec["t"], ids[rec["a"]], ids[rec["b"]]))
    return g


def ping(driver: Driver) -> None:
    driver.verify_connectivity()
