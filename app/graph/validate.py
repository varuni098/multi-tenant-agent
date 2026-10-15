"""Check a graph against the ontology (SHACL-style shape checks).

python -m app.graph.validate      # validates the live Neo4j graph; exit 1 on violations
"""

import json
import re
import sys
from collections import Counter

from app.graph.model import Graph, NodeRef
from app.graph.ontology import Ontology, load_ontology


def _within(count: int, spec: str) -> bool:
    if spec == "*":
        return True
    if spec == "1":
        return count == 1
    if spec == "0..1":
        return count <= 1
    if spec == "1..*":
        return count >= 1
    raise ValueError(f"Unknown cardinality {spec!r}")


def _value_ok(props: dict, key_def: dict) -> bool:
    try:
        value = json.loads(props["value_json"])
    except (KeyError, ValueError):
        return False
    vtype = key_def.get("value_type")
    if vtype == "bool":
        return isinstance(value, bool)
    if vtype == "enum":
        return value in json.loads(key_def.get("allowed_json", "[]"))
    if not isinstance(value, str):
        return False
    pattern = key_def.get("pattern")
    return not pattern or re.fullmatch(pattern, value) is not None


def validate(graph: Graph, ontology: Ontology | None = None) -> list[str]:
    """Return human-readable violations; an empty list means the graph conforms."""
    o = ontology or load_ontology()
    errors: list[str] = []

    for (label, key), props in graph.nodes.items():
        cls = o.classes.get(label)
        if cls is None:
            errors.append(f"{label}({key}): label is not in the ontology")
            continue
        missing = [p for p in cls.required if p not in props]
        if missing:
            errors.append(f"{label}({key}): missing required properties {missing}")
        if cls.tenant_scoped is True and "tenant_id" not in props:
            errors.append(f"{label}({key}): tenant-scoped node without tenant_id")
        if cls.tenant_scoped is False and "tenant_id" in props:
            errors.append(f"{label}({key}): shared node must not carry tenant_id")

    out_counts: Counter[tuple[NodeRef, str, str]] = Counter()
    in_counts: Counter[tuple[NodeRef, str, str]] = Counter()
    for rel in graph.rels:
        (sl, _), (el, _) = rel.start, rel.end
        if rel.start not in graph.nodes or rel.end not in graph.nodes:
            errors.append(f"{rel.type} {rel.start}->{rel.end}: dangling relationship")
            continue
        if o.rel_def(rel.type, sl, el) is None:
            errors.append(f"{rel.type} {rel.start}->{rel.end}: not allowed between {sl} and {el}")
            continue
        out_counts[(rel.start, rel.type, el)] += 1
        in_counts[(rel.end, rel.type, sl)] += 1
        # No edge may join two different tenants
        st, et = graph.tenant_of(rel.start), graph.tenant_of(rel.end)
        if st and et and st != et:
            errors.append(f"{rel.type} {rel.start}->{rel.end}: crosses tenants {st} -> {et}")

    for rd in o.relationships:
        for ref in graph.nodes:
            if ref[0] == rd.from_label and not _within(out_counts[(ref, rd.type, rd.to_label)], rd.out):
                errors.append(
                    f"{ref[0]}({ref[1]}): has {out_counts[(ref, rd.type, rd.to_label)]} outgoing "
                    f"{rd.type}->{rd.to_label}, expected {rd.out}"
                )
            if ref[0] == rd.to_label and not _within(in_counts[(ref, rd.type, rd.from_label)], rd.in_):
                errors.append(
                    f"{ref[0]}({ref[1]}): has {in_counts[(ref, rd.type, rd.from_label)]} incoming "
                    f"{rd.from_label}-{rd.type}, expected {rd.in_}"
                )

    # Domain checks the shapes above can't express
    for ref, props in graph.nodes.items():
        if ref[0] == "Setting":
            owners = [r for r in graph.rels if r.end == ref and r.type in ("HAS_SETTING", "DEFINES")]
            if len(owners) != 1:
                errors.append(f"Setting({ref[1]}): expected exactly one owner (page, tenant or template)")
            key_ref = ("SettingKey", props.get("key"))
            if key_ref in graph.nodes and not _value_ok(props, graph.nodes[key_ref]):
                errors.append(f"Setting({ref[1]}): value {props.get('value_json')} is invalid for {props.get('key')}")
        if ref[0] == "Page":
            owner = [r.start for r in graph.rels if r.end == ref and r.type == "OWNS"]
            if owner and graph.tenant_of(owner[0]) != props.get("tenant_id"):
                errors.append(f"Page({ref[1]}): tenant_id does not match its owning tenant")
    return errors


def main() -> int:
    from app.graph.client import dump_graph, get_driver

    errors = validate(dump_graph(get_driver()))
    for e in errors:
        print(e)
    print(f"{len(errors)} violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
