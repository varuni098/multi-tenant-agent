"""Load ontology/ontology.yaml into typed definitions."""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ONTOLOGY_PATH = Path(__file__).resolve().parents[2] / "ontology" / "ontology.yaml"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ClassDef:
    name: str
    key: str
    tenant_scoped: bool | str  # True, False or "optional"
    required: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class RelDef:
    type: str
    from_label: str
    to_label: str
    out: str  # "1", "0..1", "*", "1..*"
    in_: str


@dataclass(frozen=True)
class Rule:
    id: str
    description: str


@dataclass(frozen=True)
class Ontology:
    version: int
    classes: dict[str, ClassDef]
    relationships: tuple[RelDef, ...]
    rules: tuple[Rule, ...]
    inheritance: tuple[str, ...]
    aliases: dict[str, tuple[str, ...]]
    concept_terms: dict[str, dict[str, tuple[str, ...]]]  # kind -> name -> terms
    topics: dict[str, tuple[str, ...]]

    def rel_def(self, rel_type: str, from_label: str, to_label: str) -> RelDef | None:
        for r in self.relationships:
            if (r.type, r.from_label, r.to_label) == (rel_type, from_label, to_label):
                return r
        return None

    def rule(self, rule_id: str) -> Rule:
        return next(r for r in self.rules if r.id == rule_id)

    def constraint_statements(self) -> list[str]:
        return [
            f"CREATE CONSTRAINT {name.lower()}_{c.key} IF NOT EXISTS FOR (n:{name}) REQUIRE n.{c.key} IS UNIQUE"
            for name, c in self.classes.items()
        ]


def _identifier(value: str) -> str:
    """Labels, keys and relationship types are interpolated into Cypher, so they must be plain identifiers."""
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Invalid ontology identifier: {value!r}")
    return value


def parse_ontology(data: dict[str, Any]) -> Ontology:
    classes = {
        _identifier(name): ClassDef(
            name=name,
            key=_identifier(spec["key"]),
            tenant_scoped=spec.get("tenant_scoped", False),
            required=tuple(spec.get("required", [])),
            description=spec.get("description", ""),
        )
        for name, spec in data["classes"].items()
    }
    relationships = []
    for r in data["relationships"]:
        if r["from"] not in classes or r["to"] not in classes:
            raise ValueError(f"Relationship {r['type']} references an unknown class")
        relationships.append(RelDef(_identifier(r["type"]), r["from"], r["to"], str(r["out"]), str(r["in"])))
    return Ontology(
        version=int(data["version"]),
        classes=classes,
        relationships=tuple(relationships),
        rules=tuple(Rule(r["id"], r["description"]) for r in data["rules"]),
        inheritance=tuple(data["inheritance"]),
        aliases={k: tuple(v) for k, v in (data.get("aliases") or {}).items()},
        concept_terms={
            kind: {name: tuple(terms) for name, terms in names.items()}
            for kind, names in (data.get("concept_terms") or {}).items()
        },
        topics={k: tuple(v) for k, v in (data.get("topics") or {}).items()},
    )


@lru_cache
def load_ontology(path: Path = ONTOLOGY_PATH) -> Ontology:
    return parse_ontology(yaml.safe_load(path.read_text(encoding="utf-8")))
