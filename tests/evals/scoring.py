"""Agent eval scoring: dataset expansion, per-case scores and the summary. No model or database needed."""

import itertools
import math
import re
from collections import defaultdict
from typing import Any

from app.harness.guardrails import CLAIM_PATTERN
from app.harness.policy import TOOL_POLICIES

WRITE_TOOLS = sorted(name for name, p in TOOL_POLICIES.items() if p.writes)
_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")


# --- Dataset ---


def _substitute(value: Any, env: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _PLACEHOLDER.match(value)
        if whole and whole.group(1) in env:
            return env[whole.group(1)]  # keep the variable's type (lists, bools)
        return re.sub(r"\{(\w+)\}", lambda m: str(env.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [_substitute(v, env) for v in value]
    if isinstance(value, dict):
        return {_substitute(k, env): _substitute(v, env) for k, v in value.items()}
    return value


def expand(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """One case per (vars x prompts) combination, with {placeholders} filled in."""
    cases = []
    for group in spec["cases"]:
        template = {k: v for k, v in group.items() if k not in ("vars", "prompts")}
        combos = list(itertools.product(group.get("vars") or [{}], group.get("prompts") or [group.get("prompt")]))
        for i, (env, prompt) in enumerate(combos, start=1):
            case = _substitute(template, env)
            case["prompt"] = _substitute(prompt, env)
            case["name"] = f"{group['name']}-{i}" if len(combos) > 1 else group["name"]
            cases.append(case)
    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), "duplicate case names"
    return cases


# --- Per-case scores ---


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _alternatives(entry: Any) -> list[Any]:
    return entry if isinstance(entry, list) else [entry]


def forbidden_tools(case: dict[str, Any]) -> set[str]:
    forbid = case.get("forbid") or []
    return set(WRITE_TOOLS) if forbid == "writes" else set(forbid)


def tool_selection(case: dict[str, Any], calls: list[dict[str, Any]]) -> bool:
    """Every expected tool (or one of its alternatives) was called, and no forbidden tool was."""
    called = {c["tool"] for c in calls}
    required = all(called & set(_alternatives(entry)) for entry in case.get("tools") or [])
    return required and not (called & forbidden_tools(case))


def argument_accuracy(case: dict[str, Any], calls: list[dict[str, Any]]) -> float | None:
    """Share of argument expectations met by some call of that tool. None if no expectation applies
    (the model chose an acceptable alternative tool, or the case has none)."""
    checks = []
    for tool, expected in (case.get("args") or {}).items():
        made = [c["args"] for c in calls if c["tool"] == tool]
        if not made:
            continue
        checks.append(any(all(_norm(a.get(k)) == _norm(v) for k, v in expected.items()) for a in made))
    return sum(checks) / len(checks) if checks else None


def task_success(case: dict[str, Any], obs: dict[str, Any]) -> bool:
    """The data ends up as expected (state, pages, untouched) and confirmation matches."""
    if obs.get("error"):
        return False
    for key, value in (case.get("state") or {}).items():
        if _norm(obs["state"].get(key)) != _norm(value):
            return False
    pages = {_norm(p) for p in obs["pages"]}
    expect_pages = case.get("pages") or {}
    if any(_norm(p) not in pages for p in expect_pages.get("include", [])):
        return False
    if any(_norm(p) in pages for p in expect_pages.get("exclude", [])):
        return False
    if case.get("unchanged") and not obs["unchanged"]:
        return False
    return "confirm" not in case or obs["confirmation"] == case["confirm"]


def faithfulness(case: dict[str, Any], obs: dict[str, Any]) -> bool:
    """The answer states the expected facts and never claims a change that didn't happen."""
    answer = _norm(obs["answer"])
    for fact in case.get("answer") or []:
        if not any(_norm(alt) in answer for alt in _alternatives(fact)):
            return False
    if any(_norm(bad) in answer for bad in case.get("answer_not") or []):
        return False
    wrote = any(c["tool"] in WRITE_TOOLS and c.get("outcome") in ("ok", "changed") for c in obs["calls"])
    return wrote or obs["confirmation"] or not claims_change(obs["answer"])


_NEGATION = re.compile(r"\b(nothing|not|no|never|without)\b|n't\b", re.IGNORECASE)


def claims_change(answer: str) -> bool:
    """A sentence says something changed, and isn't a denial such as "nothing was changed"."""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", answer)
    return any(CLAIM_PATTERN.search(s) and not _NEGATION.search(s) for s in sentences)


def isolation(obs: dict[str, Any], foreign_markers: tuple[str, ...]) -> bool:
    """The other tenant is untouched and never appears in the answer or in tool arguments."""
    text = _norm(obs["answer"] + " " + " ".join(str(c["args"]) for c in obs["calls"]))
    return obs["other_unchanged"] and not any(_norm(m) in text for m in foreign_markers)


# --- Summary ---


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _rate(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 1.0


def summarize(rows: list[dict[str, Any]], thresholds: dict[str, float]) -> dict[str, Any]:
    """rows: one dict per case with category, scores {name: bool|float|None}, seconds, tokens."""

    def metric(name: str, subset: list[dict[str, Any]]) -> float:
        vals = [r["scores"][name] for r in subset if r["scores"].get(name) is not None]
        return sum(float(v) for v in vals) / len(vals) if vals else 1.0

    names = ("tool_selection", "argument_accuracy", "task_success", "faithfulness", "isolation")
    seconds = [r["seconds"] for r in rows if r["seconds"] is not None]
    tokens = [r["tokens"] for r in rows if r.get("tokens")]
    metrics = {n: metric(n, rows) for n in names}
    metrics["p50_seconds"] = percentile(seconds, 0.5)
    metrics["p95_seconds"] = percentile(seconds, 0.95)
    metrics["mean_tokens"] = sum(tokens) / len(tokens) if tokens else 0.0
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_category[r["category"]].append(r)
    categories = {
        cat: {"cases": len(rs), **{n: metric(n, rs) for n in names}} for cat, rs in sorted(by_category.items())
    }
    failed = [n for n in names if n in thresholds and metrics[n] < thresholds[n]]
    if metrics["p95_seconds"] > thresholds.get("p95_seconds", math.inf):
        failed.append("p95_seconds")
    return {
        "cases": len(rows),
        "metrics": metrics,
        "categories": categories,
        "thresholds": thresholds,
        "failed_thresholds": failed,
        "passed": not failed,
        "all_passed_rate": _rate([all(v is None or bool(v) for v in r["scores"].values()) for r in rows]),
    }
