"""Knowledge pipeline pieces that need no Neo4j or model: loaders, chunker, concepts, embedder, ranking, eval."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.graph.model import Graph
from app.graph.validate import validate
from app.harness import tenant_client
from app.harness.context import Citation, RunDeps
from app.harness.intent import classify
from app.harness.policy import allowed_tools
from app.knowledge import concepts, evaluate
from app.knowledge.chunker import chunk
from app.knowledge.documents import discover, html_to_text, load, pdf_to_text
from app.knowledge.embeddings import HashEmbedder, cosine
from app.knowledge.retrieval import Candidate, rerank
from app.workflow.graph import _unique
from tests.conftest import EDITOR_A, TENANT_A

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _minimal_pdf(text: str) -> bytes:
    """A valid one-page PDF (with a correct xref table) that draws `text`."""
    stream = f"BT /F1 18 Tf 20 40 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 100]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + body + b"endobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


# --- Loaders ---


def test_discover_scopes_documents_by_folder(tmp_path):
    (tmp_path / "product").mkdir()
    (tmp_path / "product" / "a.md").write_text("# Alpha\n\ntext")
    (tmp_path / "tenants" / TENANT_A).mkdir(parents=True)
    (tmp_path / "tenants" / TENANT_A / "b.md").write_text("# Beta\n\nprivate")
    (tmp_path / "tenants" / "not-a-uuid").mkdir()
    (tmp_path / "tenants" / "not-a-uuid" / "c.md").write_text("# Ignored")
    (tmp_path / "product" / "image.png").write_bytes(b"\x89PNG")
    docs = {d.path: d for d in discover(tmp_path)}
    assert set(docs) == {"product/a.md", f"tenants/{TENANT_A}/b.md"}
    assert docs["product/a.md"].uid == "global:product/a.md" and docs["product/a.md"].tenant_id is None
    assert docs[f"tenants/{TENANT_A}/b.md"].tenant_id == TENANT_A
    assert docs["product/a.md"].title == "Alpha"


def test_content_hash_tracks_changes(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# X\n\none")
    first = load(tmp_path, f)
    f.write_text("# X\n\ntwo")
    second = load(tmp_path, f)
    assert first and second and first.content_hash != second.content_hash


def test_html_keeps_headings_and_drops_markup():
    title, text = html_to_text(
        "<html><head><title>FAQ</title><style>p{}</style></head><body><h1>FAQ</h1><h2>Q one</h2>"
        "<p>Answer <b>bold</b>.</p><script>alert(1)</script></body></html>"
    )
    assert title == "FAQ"
    assert "## Q one" in text and "Answer bold." in text
    assert "alert" not in text and "p{}" not in text


def test_pdf_text_extraction():
    assert "Hello PDF docs" in pdf_to_text(_minimal_pdf("Hello PDF docs"))


def test_shipped_docs_load():
    docs = discover(DOCS)
    assert len(docs) == 10
    assert {d.format for d in docs} == {"md", "html"}
    assert sum(1 for d in docs if d.tenant_id) == 1


# --- Chunker ---


def test_chunks_follow_headings():
    chunks = chunk("# Doc\n\nintro\n\n## Setup\n\nsetup text\n\n### Details\n\nmore\n\n## Usage\n\nuse it")
    assert [(c.index, c.heading) for c in chunks] == [
        (0, "Doc"),
        (1, "Setup"),
        (2, "Setup › Details"),
        (3, "Usage"),
    ]


def test_long_sections_split_with_overlap():
    body = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20))
    chunks = chunk(f"# T\n\n## Long\n\n{body}", target_chars=800, overlap_chars=100)
    assert len(chunks) > 3
    assert all(len(c.text) <= 800 + 100 + 2 for c in chunks)
    assert chunks[1].text.split("\n\n")[0] in chunks[0].text  # overlap carries the previous tail


def test_shipped_docs_chunk_sensibly():
    for doc in discover(DOCS):
        chunks = chunk(doc.text)
        assert chunks and all(0 < len(c.text) <= 2200 for c in chunks), doc.path


# --- Concepts ---


def test_vocabulary_covers_ontology_entities_and_topics():
    vocab = concepts.vocabulary()
    assert {"theme", "brand_color", "custom_branding", "dark_first", "pro", "admin", "inheritance"} <= set(vocab)
    assert vocab["brand_color"].about_label == "SettingKey"
    assert vocab["custom_branding"].about_label == "Feature"
    assert vocab["inheritance"].about_label is None


@pytest.mark.parametrize(
    ("text", "expected", "not_expected"),
    [
        ("Change the accent colour on the login screen", {"brand_color"}, set()),
        ("Feel free to ask", set(), {"free"}),
        ("On the free plan you can't brand", {"free"}, set()),
        ("Switch to the standard template", {"standard"}, set()),
        ("The page inherits the tenant default", {"inheritance"}, set()),
        ("Approve it with the confirmation token", {"confirmation"}, set()),
        ("undo, revert, roll back", {"undo"}, set()),
    ],
)
def test_lexical_concept_matching(text, expected, not_expected):
    found = set(concepts.match_concepts(text))
    assert expected <= found and not (not_expected & found)


def test_model_concepts_are_restricted_to_the_vocabulary(monkeypatch):
    def fake_post(url, json, timeout):
        assert json["format"]["properties"]["concepts"]["items"]["enum"]  # constrained output
        return httpx.Response(
            200,
            json={"message": {"content": '{"concepts": ["theme", "quantum_flux", "undo"]}'}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(concepts.httpx, "post", fake_post)
    assert concepts.model_concepts("anything") == ["theme", "undo"]  # invented concept dropped
    assert concepts.extract_concepts("tenant default", use_model=True) == ["inheritance", "theme", "undo"]


def test_model_concept_failure_falls_back_to_lexical(monkeypatch):
    def broken(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(concepts.httpx, "post", broken)
    assert concepts.extract_concepts("roll back the change", use_model=True) == ["undo"]


# --- Embeddings and ranking ---


def test_hash_embedder_is_deterministic_and_normalised():
    e = HashEmbedder(768)
    a, b = e.embed_query("dark theme for the dashboard"), e.embed_query("dark theme for the dashboard")
    assert a == b and abs(sum(x * x for x in a) - 1) < 1e-9
    related = cosine(a, e.embed_query("set the dashboard theme to dark"))
    unrelated = cosine(a, e.embed_query("confirmation tokens expire after five minutes"))
    assert related > unrelated


def _cand(uid, similarity, concepts_):
    return Candidate(uid, "T", "H", "p", "text", None, concepts_, similarity)


def test_rerank_rewards_concept_overlap():
    ranked = rerank([_cand("a", 0.80, []), _cand("b", 0.74, ["confirmation"])], ["confirmation"], k=2)
    assert [c.uid for c, _ in ranked] == ["b", "a"]
    assert rerank([_cand("a", 0.8, []), _cand("b", 0.7, [])], [], k=1)[0][0].uid == "a"


# --- Harness: citations, intent, policy ---


def test_search_docs_results_become_citations():
    deps = RunDeps(identity=EDITOR_A, request_id="r", allowed_tools=frozenset({"search_docs"}))
    hits = [{"rank": 1, "title": "Templates", "heading": "Switching templates", "path": "product/templates.md"}]

    async def call_tool(name, args, metadata=None):
        return {"result": hits}

    asyncio.run(tenant_client._log_graph_call(SimpleNamespace(deps=deps), call_tool, "search_docs", {"query": "x"}))
    assert deps.citations == [Citation(1, "Templates", "Switching templates", "product/templates.md")]


def test_citations_are_deduplicated_per_section():
    c = Citation(1, "T", "H", "p")
    assert _unique([c, Citation(2, "T", "H", "p"), Citation(3, "T", "Other", "p")]) == [
        c,
        Citation(3, "T", "Other", "p"),
    ]


def test_doc_questions_reach_the_agent():
    assert classify("How long is a confirmation token valid?", []) == "read"
    assert classify("What do I get when upgrading?", []) == "read"
    assert "search_docs" in allowed_tools("viewer", "read", graph=True)


# --- Ontology v2 accepts knowledge nodes ---


def test_knowledge_nodes_conform_to_ontology():
    g = Graph()
    g.add_node("SettingKey", {"name": "theme", "value_type": "enum", "default_json": '"light"'})
    concept = g.add_node("Concept", {"name": "theme", "kind": "setting", "terms": ["theme"]})
    g.add_rel("ABOUT", concept, ("SettingKey", "theme"))
    doc = g.add_node("Document", {"uid": "global:a.md", "path": "a.md", "title": "A", "content_hash": "h"})
    ch = g.add_node("DocChunk", {"uid": "global:a.md#0", "text": "t", "chunk_index": 0, "embedding": [0.1]})
    g.add_rel("HAS_CHUNK", doc, ch)
    g.add_rel("MENTIONS", ch, concept)
    assert validate(g) == []
    g.add_rel("HAS_CHUNK", ("Document", "global:a.md"), ("Concept", "theme"))
    assert any("not allowed between Document and Concept" in e for e in validate(g))


# --- Eval scoring ---


def test_eval_metrics(monkeypatch):
    results = {
        "q1": ["a.md", "b.md"],
        "q2": ["c.md", "b.md"],
        "q3": ["x.md"],
        "q4": ["secret.md"],
    }
    monkeypatch.setattr(evaluate, "search", lambda d, e, tid, q, k: [{"path": p} for p in results[q]])
    monkeypatch.setattr(evaluate, "get_driver", lambda: None)
    spec = {
        "thresholds": {"k": 4, "recall_at_k": 0.6, "mrr": 0.5},
        "tenants": {"acme": "A", "globex": "B"},
        "cases": [
            {"q": "q1", "expect": "a.md"},
            {"q": "q2", "expect": "b.md"},
            {"q": "q3", "expect": "y.md"},
            {"q": "q4", "tenant": "globex", "absent": "secret.md"},
        ],
    }
    report = evaluate.evaluate(spec, HashEmbedder(8))
    assert report.recall_at_k == pytest.approx(2 / 3)
    assert report.mrr == pytest.approx((1 + 0.5) / 3)
    assert report.leaks == 1 and not report.passed
