"""The concept vocabulary (from the ontology) and concept extraction for documentation.

Extraction always runs a whole-word lexical match. An optional model pass can suggest more
concepts, but only names already in the vocabulary are accepted, so the model can't invent
concepts.
"""

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.core.config import get_settings
from app.graph.ontology import load_ontology

log = logging.getLogger(__name__)

# Concept kind -> the graph label its ABOUT edge points to
KIND_LABELS = {"setting": "SettingKey", "feature": "Feature", "template": "Template", "plan": "Plan", "role": "Role"}


@dataclass(frozen=True)
class ConceptDef:
    name: str
    kind: str  # setting | feature | template | plan | role | topic
    terms: tuple[str, ...]

    @property
    def about_label(self) -> str | None:
        return KIND_LABELS.get(self.kind)


@lru_cache
def vocabulary() -> dict[str, ConceptDef]:
    o = load_ontology()
    vocab: dict[str, ConceptDef] = {}
    for key, aliases in o.aliases.items():  # setting keys
        vocab[key] = ConceptDef(key, "setting", tuple(sorted({key, key.replace("_", " "), *aliases})))
    kinds = {"features": "feature", "templates": "template", "plans": "plan", "roles": "role"}
    for group, names in o.concept_terms.items():
        for name, terms in names.items():
            vocab[name] = ConceptDef(name, kinds[group], tuple(sorted(set(terms))))
    for name, terms in o.topics.items():
        vocab[name] = ConceptDef(name, "topic", tuple(sorted(set(terms))))
    return vocab


@lru_cache
def _patterns() -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for c in vocabulary().values():
        alternatives = "|".join(re.escape(t.lower()) for t in sorted(c.terms, key=len, reverse=True))
        out.append((c.name, re.compile(rf"(?<![a-z0-9_])(?:{alternatives})(?![a-z0-9_])")))
    return out


def match_concepts(text: str) -> list[str]:
    lowered = text.lower()
    return sorted(name for name, pattern in _patterns() if pattern.search(lowered))


def model_concepts(text: str) -> list[str]:
    """Ask the local model which vocabulary concepts a chunk is about; keep only known names."""
    vocab = vocabulary()
    s = get_settings()
    prompt = (
        "Which of these concepts does the text discuss? Answer with names from the list only.\n"
        f"Concepts: {', '.join(sorted(vocab))}\n\nText:\n{text[:2500]}"
    )
    schema = {
        "type": "object",
        "properties": {"concepts": {"type": "array", "items": {"type": "string", "enum": sorted(vocab)}}},
        "required": ["concepts"],
    }
    try:
        resp = httpx.post(
            f"{s.ollama_native_url}/api/chat",
            json={
                "model": s.ollama_model,
                "messages": [{"role": "user", "content": prompt}],
                "format": schema,
                "stream": False,
                "think": False,
                "options": {"temperature": 0},
            },
            timeout=120,
        )
        resp.raise_for_status()
        proposed = json.loads(resp.json()["message"]["content"]).get("concepts", [])
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("Model concept pass failed, using lexical matches only: %s", e)
        return []
    return sorted({c for c in proposed if c in vocab})


def extract_concepts(text: str, use_model: bool = False) -> list[str]:
    found = set(match_concepts(text))
    if use_model:
        found |= set(model_concepts(text))
    return sorted(found)
