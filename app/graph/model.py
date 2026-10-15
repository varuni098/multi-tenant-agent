"""In-memory property graph used for the expected projection, dumps of Neo4j, validation and drift."""

from dataclasses import dataclass, field
from typing import Any

from app.graph.ontology import Ontology, load_ontology

NodeRef = tuple[str, Any]  # (label, key value)


@dataclass(frozen=True)
class Rel:
    type: str
    start: NodeRef
    end: NodeRef


@dataclass
class Graph:
    nodes: dict[NodeRef, dict[str, Any]] = field(default_factory=dict)
    rels: set[Rel] = field(default_factory=set)

    def add_node(self, label: str, props: dict[str, Any], ontology: Ontology | None = None) -> NodeRef:
        key = (ontology or load_ontology()).classes[label].key
        ref = (label, props[key])
        self.nodes[ref] = {k: v for k, v in props.items() if v is not None}  # Neo4j has no null properties
        return ref

    def add_rel(self, rel_type: str, start: NodeRef, end: NodeRef) -> None:
        self.rels.add(Rel(rel_type, start, end))

    def merge(self, other: "Graph") -> "Graph":
        self.nodes.update(other.nodes)
        self.rels |= other.rels
        return self

    def tenant_of(self, ref: NodeRef) -> str | None:
        props = self.nodes.get(ref)
        return props.get("tenant_id") if props else None
