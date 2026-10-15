"""Compare the expected graph (from MySQL) with the actual one (from Neo4j)."""

from dataclasses import dataclass, field
from typing import Any

from app.graph.model import Graph, NodeRef, Rel

# Labels the projection does not own (Phase 6 documentation nodes)
UNMANAGED_LABELS = {"DocChunk", "Concept"}


@dataclass
class Drift:
    missing_nodes: list[NodeRef] = field(default_factory=list)
    extra_nodes: list[NodeRef] = field(default_factory=list)
    changed_nodes: list[tuple[NodeRef, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    missing_rels: list[Rel] = field(default_factory=list)
    extra_rels: list[Rel] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(
            len(x)
            for x in (self.missing_nodes, self.extra_nodes, self.changed_nodes, self.missing_rels, self.extra_rels)
        )

    def summary(self) -> str:
        return (
            f"nodes: {len(self.missing_nodes)} missing, {len(self.extra_nodes)} extra, "
            f"{len(self.changed_nodes)} changed; rels: {len(self.missing_rels)} missing, {len(self.extra_rels)} extra"
        )


def _managed(ref: NodeRef) -> bool:
    return ref[0] not in UNMANAGED_LABELS


def diff(expected: Graph, actual: Graph) -> Drift:
    d = Drift()
    for ref, props in expected.nodes.items():
        if ref not in actual.nodes:
            d.missing_nodes.append(ref)
        elif actual.nodes[ref] != props:
            d.changed_nodes.append((ref, props, actual.nodes[ref]))
    d.extra_nodes = [ref for ref in actual.nodes if ref not in expected.nodes and _managed(ref)]
    d.missing_rels = sorted(expected.rels - actual.rels, key=repr)
    d.extra_rels = sorted((r for r in actual.rels - expected.rels if _managed(r.start) and _managed(r.end)), key=repr)
    return d
