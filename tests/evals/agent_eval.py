"""Agent eval: the golden dataset (evals/agent.yaml) against a real model, scored with pydantic-evals.

    docker compose --profile eval run --rm evals                        # qwen3:8b, all cases
    docker compose --profile eval run --rm evals --model llama3.1:8b    # compare a model (pull it first)
    docker compose --profile eval run --rm evals --only set-theme --limit 5
    docker compose --profile eval run --rm evals --compare evals/results/a.json evals/results/b.json

Each case runs the full harness (guardrails, intent, RBAC, verification) on freshly seeded
SQLite data with an in-process tenant-mcp, so it never touches a real database. The graph
is off here; documentation retrieval has its own eval (app.knowledge.evaluate).
Exits 1 when a threshold is missed.
"""

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from tests.evals import scoring

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evals" / "agent.yaml"
RESULTS = ROOT / "evals" / "results"
METRICS = ("tool_selection", "argument_accuracy", "task_success", "faithfulness", "isolation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="Ollama model (default: OLLAMA_MODEL)")
    parser.add_argument("--only", help="run cases whose name contains this text")
    parser.add_argument("--limit", type=int, help="run at most this many cases")
    parser.add_argument("--dataset", type=Path, default=DATASET, help="dataset file (default: evals/agent.yaml)")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"), help="compare two result files")
    args = parser.parse_args()
    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))
    if args.model:
        os.environ["OLLAMA_MODEL"] = args.model
    return run(args.dataset, args.only, args.limit)


def run(dataset_file: Path, only: str | None, limit: int | None) -> int:
    # Importing the test setup pins an isolated environment first: temp SQLite, in-memory
    # store, graph off, an in-process tenant-mcp port. Only then are app modules loaded.
    from tests import conftest

    # isort: split
    from pydantic_evals import Case, Dataset

    from app.core.config import get_settings
    from app.db.models import Base
    from app.db.seed import seed
    from app.db.session import get_engine
    from app.harness.context import Identity
    from app.harness.store import get_store
    from app.mcp_server.server import app as tenant_mcp
    from app.services import settings as svc
    from app.workflow.graph import run_workflow

    spec = yaml.safe_load(dataset_file.read_text(encoding="utf-8"))
    cases = [c for c in scoring.expand(spec) if not only or only in c["name"]][: limit or None]
    model = get_settings().ollama_model
    thinking = get_settings().llm_thinking
    users = {
        "acme_viewer": Identity(conftest.TENANT_A, "viewer@acme.test", "viewer"),
        "acme_editor": Identity(conftest.TENANT_A, "editor@acme.test", "editor"),
        "acme_admin": Identity(conftest.TENANT_A, "admin@acme.test", "admin"),
        "globex_admin": Identity(conftest.TENANT_B, "admin@globex.test", "admin"),
    }
    markers = {
        conftest.TENANT_A: (conftest.TENANT_A, "acme", "@acme.test"),
        conftest.TENANT_B: (conftest.TENANT_B, "globex", "@globex.test"),
    }

    def snapshot(tenant_id: str) -> dict[str, Any]:
        pages = svc.list_pages(tenant_id)
        state = {f"default.{s.key}": s.value for s in svc.get_effective_settings(tenant_id)}
        for p in pages:
            state.update({f"{p.name}.{s.key}": s.value for s in svc.get_effective_settings(tenant_id, p.name)})
        return {"pages": [p.name for p in pages], "state": state}

    def reset() -> None:
        engine = get_engine()
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        seed()
        get_store.cache_clear()

    def task(case: dict[str, Any]) -> dict[str, Any]:
        reset()
        identity = users[case["user"]]
        other = conftest.TENANT_B if identity.tenant_id == conftest.TENANT_A else conftest.TENANT_A
        seeded_own, seeded_other = snapshot(identity.tenant_id), snapshot(other)
        caller = conftest.caller_for(identity)
        session = uuid.uuid4().hex
        for earlier in case.get("before") or []:
            run_workflow(earlier, caller, f"eval-{case['name']}-pre", session)

        calls: list[dict[str, Any]] = []

        def emit(event: str, data: dict[str, Any]) -> None:
            if event == "tool_start":
                calls.append({"tool": data["tool"], "args": data.get("args", {}), "outcome": None})
            elif event == "tool_end":
                for c in reversed(calls):
                    if c["tool"] == data["tool"] and c["outcome"] is None:
                        c["outcome"] = data["outcome"]
                        break

        started = time.perf_counter()
        result = run_workflow(case["prompt"], caller, f"eval-{case['name']}", session, emit=emit)
        seconds = time.perf_counter() - started
        own = snapshot(identity.tenant_id)
        return {
            "answer": result.answer,
            "intent": result.intent,
            "blocked": result.blocked,
            "calls": calls,
            "confirmation": result.confirmation is not None,
            "state": own["state"],
            "pages": own["pages"],
            "unchanged": own == seeded_own,
            "other_unchanged": snapshot(other) == seeded_other,
            "foreign_markers": markers[other],
            "tokens": result.tokens,
            "model_requests": result.model_requests,
            "seconds": seconds,
        }

    stop = conftest.start_server(tenant_mcp, conftest._mcp_port)
    try:
        dataset = Dataset(
            name="agent",
            cases=[Case(name=c["name"], inputs=c, metadata={"category": c["category"]}) for c in cases],
            evaluators=[ToolSelection(), ArgumentAccuracy(), TaskSuccess(), Faithfulness(), Isolation()],
        )
        print(f"Running {len(cases)} cases from {dataset_file.name} on {model} (thinking {thinking}) ...", flush=True)
        report = dataset.evaluate_sync(task, name=model, max_concurrency=1, progress=False)
    finally:
        stop()

    rows = []
    for rc in report.cases:
        scores = {n: None for n in METRICS}
        scores.update({k: v.value for k, v in rc.assertions.items()})
        scores.update({k: v.value for k, v in rc.scores.items()})
        out = rc.output
        rows.append(
            {
                "name": rc.name,
                "category": rc.inputs["category"],
                "prompt": rc.inputs["prompt"],
                "scores": scores,
                "seconds": out["seconds"],
                "tokens": out["tokens"],
                "model_requests": out["model_requests"],
                "answer": out["answer"],
                "calls": out["calls"],
            }
        )
    for failure in report.failures:  # the task itself raised: counts as a failure everywhere
        rows.append(
            {
                "name": failure.name,
                "category": failure.inputs["category"],
                "prompt": failure.inputs["prompt"],
                "scores": dict.fromkeys(METRICS, False),
                "seconds": None,
                "tokens": None,
                "answer": f"ERROR: {failure.error_message}",
                "calls": [],
            }
        )
    summary = scoring.summarize(rows, spec["thresholds"])
    _print(model, summary, rows)
    RESULTS.mkdir(parents=True, exist_ok=True)
    tag = f"{model.replace(':', '_').replace('/', '_')}{'-thinking' if thinking else ''}"
    out_file = RESULTS / f"{dataset_file.stem}-{tag}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out_file.write_text(
        json.dumps(
            {"model": model, "thinking": thinking, "dataset": dataset_file.name, "summary": summary, "cases": rows},
            indent=2,
            default=str,
        )
    )
    print(f"\nWrote {out_file.relative_to(ROOT)}")
    return 0 if summary["passed"] else 1


# --- pydantic-evals evaluators (thin wrappers over tests/evals/scoring.py) ---


@dataclass
class ToolSelection(Evaluator[dict, dict, dict]):
    def evaluate(self, ctx: EvaluatorContext[dict, dict, dict]) -> dict[str, bool]:
        return {"tool_selection": scoring.tool_selection(ctx.inputs, ctx.output["calls"])}


@dataclass
class ArgumentAccuracy(Evaluator[dict, dict, dict]):
    def evaluate(self, ctx: EvaluatorContext[dict, dict, dict]) -> dict[str, float]:
        score = scoring.argument_accuracy(ctx.inputs, ctx.output["calls"])
        return {} if score is None else {"argument_accuracy": score}


@dataclass
class TaskSuccess(Evaluator[dict, dict, dict]):
    def evaluate(self, ctx: EvaluatorContext[dict, dict, dict]) -> dict[str, bool]:
        return {"task_success": scoring.task_success(ctx.inputs, ctx.output)}


@dataclass
class Faithfulness(Evaluator[dict, dict, dict]):
    def evaluate(self, ctx: EvaluatorContext[dict, dict, dict]) -> dict[str, bool]:
        return {"faithfulness": scoring.faithfulness(ctx.inputs, ctx.output)}


@dataclass
class Isolation(Evaluator[dict, dict, dict]):
    def evaluate(self, ctx: EvaluatorContext[dict, dict, dict]) -> dict[str, bool]:
        return {"isolation": scoring.isolation(ctx.output, ctx.output["foreign_markers"])}


# --- Output ---


def _pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


def _print(model: str, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    m, t = summary["metrics"], summary["thresholds"]
    print(f"\nAgent eval: {model}, {summary['cases']} cases")
    for name in METRICS:
        target = f"(>= {_pct(t[name]).strip()})" if name in t else ""
        print(f"  {name:<18} {_pct(m[name])}  {target}")
    print(f"  {'latency p50/p95':<18} {m['p50_seconds']:.1f}s / {m['p95_seconds']:.1f}s  (p95 <= {t['p95_seconds']}s)")
    print(f"  {'mean tokens':<18} {m['mean_tokens']:.0f}")
    print("\n  category        cases  tools   args    task    faith   isol")
    for cat, c in summary["categories"].items():
        vals = "  ".join(_pct(c[n]) for n in METRICS)
        print(f"  {cat:<15} {c['cases']:>5}  {vals}")
    misses = [r for r in rows if not all(v is None or bool(v) for v in r["scores"].values())]
    if misses:
        print(f"\n  {len(misses)} case(s) missed something:")
        for r in misses:
            bad = [k for k, v in r["scores"].items() if v is not None and not v]
            tools = ", ".join(f"{c['tool']}->{c['outcome']}" for c in r["calls"]) or "no tools"
            print(f"  - {r['name']}: {', '.join(bad)} | {tools} | {r['answer'][:110]!r}")
    print(f"\n{'PASS' if summary['passed'] else 'FAIL: ' + ', '.join(summary['failed_thresholds'])}")


def compare(baseline: Path, candidate: Path) -> int:
    a, b = (json.loads(p.read_text()) for p in (baseline, candidate))
    print(f"{'metric':<18} {a['model']:>14} {b['model']:>14}")
    for name in (*METRICS, "p50_seconds", "p95_seconds", "mean_tokens"):
        va, vb = a["summary"]["metrics"][name], b["summary"]["metrics"][name]
        fmt = _pct if name in METRICS else (lambda x: f"{x:.1f}")
        print(f"{name:<18} {fmt(va):>14} {fmt(vb):>14}")

    def ok(row: dict[str, Any]) -> bool:
        return all(v is None or bool(v) for v in row["scores"].values())

    before = {r["name"]: ok(r) for r in a["cases"]}
    for r in b["cases"]:
        if r["name"] in before and before[r["name"]] != ok(r):
            print(f"  {r['name']}: {'pass' if before[r['name']] else 'fail'} -> {'pass' if ok(r) else 'fail'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
