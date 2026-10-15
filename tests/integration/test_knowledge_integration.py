"""Knowledge ingestion and GraphRAG retrieval against real Neo4j (hash embedder, no GPU).

    docker compose --profile test run --rm tests
"""

import os
import shutil
from pathlib import Path

import pytest
import yaml

from app.graph.client import dump_graph, ensure_constraints, get_driver
from app.graph.reconcile import compute_drift, project_all
from app.graph.validate import validate
from app.knowledge import retrieval
from app.knowledge.embeddings import HashEmbedder
from app.knowledge.evaluate import DEFAULT_FILE, evaluate
from app.knowledge.ingest import ingest
from app.workflow.graph import run_workflow
from tests.conftest import GRAPH_PORT, TENANT_A, TENANT_B, VIEWER_A, caller_for, start_server

pytestmark = pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="needs Neo4j")

DOCS = Path(__file__).resolve().parents[2] / "docs"
STYLE_GUIDE = f"tenants/{TENANT_A}/acme-style-guide.md"
EMBEDDER = HashEmbedder(768)


@pytest.fixture
def kg(tmp_path):
    """Fresh graph with the MySQL projection plus the shipped docs ingested from a scratch copy."""
    driver = get_driver()
    driver.execute_query("MATCH (n) DETACH DELETE n")
    driver.execute_query("DROP INDEX doc_chunk_embedding IF EXISTS")
    ensure_constraints(driver)
    project_all(driver)
    root = tmp_path / "docs"
    shutil.copytree(DOCS, root)
    stats = ingest(root, driver, EMBEDDER)
    return driver, root, stats


def _search(tid, q, k=4):
    return retrieval.search(get_driver(), EMBEDDER, tid, q, k)


def test_ingest_everything_then_nothing_twice(kg):
    driver, root, stats = kg
    assert (stats.seen, stats.ingested, stats.unchanged, stats.removed) == (10, 10, 0, 0)
    assert stats.chunks >= 30
    again = ingest(root, driver, EMBEDDER)
    assert (again.ingested, again.unchanged) == (0, 10)


def test_only_changed_documents_are_reingested(kg):
    driver, root, _ = kg
    templates = root / "product" / "templates.md"
    templates.write_text(templates.read_text() + "\n\n## Retired templates\n\nThe legacy_blue template was retired.\n")
    (root / "product" / "history-and-undo.md").unlink()
    stats = ingest(root, driver, EMBEDDER)
    assert (stats.ingested, stats.unchanged, stats.removed) == (1, 8, 1)
    assert _search(TENANT_A, "legacy_blue retired template")[0]["path"] == "product/templates.md"
    assert not any(r["path"] == "product/history-and-undo.md" for r in _search(TENANT_A, "undo the last change", 8))


def test_graph_with_knowledge_conforms_and_projection_has_no_drift(kg):
    driver, _, _ = kg
    assert validate(dump_graph(driver)) == []
    assert compute_drift(driver).total == 0  # knowledge nodes don't disturb the MySQL projection


def test_concepts_link_chunks_to_ontology_entities(kg):
    driver, _, _ = kg
    rec = driver.execute_query(
        """
        MATCH (d:Document {path: 'product/plans-and-features.md'})-[:HAS_CHUNK]->(c)-[:MENTIONS]->(k:Concept)
        OPTIONAL MATCH (k)-[:ABOUT]->(e)
        RETURN collect(DISTINCT k.name) AS concepts, collect(DISTINCT labels(e)[0] + ':' + coalesce(e.name, e.key)) AS entities
        """
    ).records[0]
    assert {"brand_color", "custom_branding", "free", "pro"} <= set(rec["concepts"])
    assert {"SettingKey:brand_color", "Feature:custom_branding", "Plan:pro"} <= set(rec["entities"])


def test_search_returns_cited_relevant_sections(kg):
    results = _search(TENANT_A, "How long is a confirmation token valid?")
    top = results[0]
    assert top["path"] == "product/confirmations.md"
    assert top["citation"].startswith("[1] Confirming destructive actions")
    assert "confirmation" in top["matched_concepts"]
    assert [r["rank"] for r in results] == list(range(1, len(results) + 1))


def test_concept_expansion_contributes_candidates(kg):
    results = _search(TENANT_A, "custom branding on the free plan", k=8)
    assert any("concept" in r["via"] for r in results)
    assert results[0]["path"] == "product/plans-and-features.md"


def test_tenant_docs_are_private(kg):
    question = "Which theme should the Acme dashboard use?"
    assert STYLE_GUIDE in [r["path"] for r in _search(TENANT_A, question)]
    assert STYLE_GUIDE not in [r["path"] for r in _search(TENANT_B, question, k=8)]
    assert all(r["scope"] == "product" for r in _search(TENANT_B, "Acme style guide marketing review", k=8))


def test_retrieval_eval_has_no_leaks(kg):
    report = evaluate(yaml.safe_load(DEFAULT_FILE.read_text()), EMBEDDER)
    print(report.render())
    assert report.leaks == 0
    # A lexical hash embedder is a weak baseline; the live eval with nomic-embed-text uses the real thresholds
    assert report.recall_at_k >= 0.7


def test_agent_answers_with_citations(kg, script, settings):
    from app.graph_mcp.server import app

    settings.graph_mode = "enforce"
    settings.embedding_provider = "hash"
    stop = start_server(app, GRAPH_PORT)
    try:
        s = script([("search_docs", {"query": "how long is a confirmation token valid"})], "Five minutes [1].")
        result = run_workflow("How long is a confirmation token valid?", caller_for(VIEWER_A), "req", "s")
    finally:
        stop()
    assert "search_docs" in s.offered[0]
    assert result.answer == "Five minutes [1]."
    assert result.citations and result.citations[0].path == "product/confirmations.md"
