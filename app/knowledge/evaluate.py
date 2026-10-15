"""Retrieval eval for search_docs: recall@k, MRR and tenant leakage against evals/retrieval.yaml.

python -m app.knowledge.evaluate [--file evals/retrieval.yaml]   # exit 1 below the thresholds
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.logging import setup_logging
from app.graph.client import get_driver
from app.knowledge.embeddings import Embedder, get_embedder
from app.knowledge.retrieval import search

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "evals" / "retrieval.yaml"


@dataclass
class EvalReport:
    k: int
    recall_at_k: float
    mrr: float
    leaks: int
    rows: list[dict[str, Any]]
    thresholds: dict[str, float]

    @property
    def passed(self) -> bool:
        return (
            self.recall_at_k >= self.thresholds["recall_at_k"]
            and self.mrr >= self.thresholds["mrr"]
            and self.leaks == 0
        )

    def render(self) -> str:
        lines = [f"{'rank':>4}  {'tenant':<7} question"]
        for r in self.rows:
            mark = (
                "LEAK" if r["leaked"] else (str(r["rank"]) if r["rank"] else ("ok" if r["expect"] is None else "miss"))
            )
            lines.append(f"{mark:>4}  {r['tenant']:<7} {r['q']}")
        lines.append(
            f"recall@{self.k}={self.recall_at_k:.2f} (>= {self.thresholds['recall_at_k']}), "
            f"MRR={self.mrr:.2f} (>= {self.thresholds['mrr']}), leaks={self.leaks} -> "
            f"{'PASS' if self.passed else 'FAIL'}"
        )
        return "\n".join(lines)


def evaluate(spec: dict[str, Any], embedder: Embedder) -> EvalReport:
    driver = get_driver()
    k = int(spec["thresholds"]["k"])
    tenants = spec["tenants"]
    rows = []
    for case in spec["cases"]:
        tenant = case.get("tenant", "acme")
        paths = [r["path"] for r in search(driver, embedder, tenants[tenant], case["q"], k)]
        expect, absent = case.get("expect"), case.get("absent")
        rank = paths.index(expect) + 1 if expect in paths else None
        rows.append({"q": case["q"], "tenant": tenant, "expect": expect, "rank": rank, "leaked": absent in paths})
    scored = [r for r in rows if r["expect"]]
    recall = sum(1 for r in scored if r["rank"]) / len(scored) if scored else 1.0
    mrr = sum(1 / r["rank"] for r in scored if r["rank"]) / len(scored) if scored else 1.0
    return EvalReport(k, recall, mrr, sum(r["leaked"] for r in rows), rows, spec["thresholds"])


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = parser.parse_args()
    report = evaluate(yaml.safe_load(args.file.read_text(encoding="utf-8")), get_embedder())
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
