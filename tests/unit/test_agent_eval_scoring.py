"""The agent eval's dataset and scoring (the eval itself needs a model: tests/evals/agent_eval.py)."""

from pathlib import Path

import pytest
import yaml

from app.harness.policy import TOOL_POLICIES
from tests.evals import scoring

DATASET = Path(__file__).resolve().parents[2] / "evals" / "agent.yaml"
SPEC = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
CASES = scoring.expand(SPEC)


def test_dataset_is_large_and_well_formed():
    assert len(CASES) >= 140
    users = {"acme_viewer", "acme_editor", "acme_admin", "globex_admin"}
    for case in CASES:
        assert case["user"] in users, case["name"]
        assert "{" not in case["prompt"], case["name"]  # every placeholder filled
        named = [t for entry in case.get("tools") or [] for t in scoring._alternatives(entry)]
        named += list(case.get("args") or {}) + sorted(scoring.forbidden_tools(case))
        assert set(named) <= set(TOOL_POLICIES), case["name"]
    assert {"read", "write", "confirm", "permission", "policy", "off_topic", "injection"} <= {
        c["category"] for c in CASES
    }


def test_expansion_fills_placeholders_and_keeps_types():
    spec = {
        "cases": [
            {
                "name": "x",
                "category": "write",
                "vars": [{"page": "login", "value": False}, {"page": "dashboard", "value": ["a", "b"]}],
                "prompts": ["Set {page}", "Change {page}"],
                "state": {"{page}.show_tips": "{value}"},
            }
        ]
    }
    cases = scoring.expand(spec)
    assert [c["name"] for c in cases] == ["x-1", "x-2", "x-3", "x-4"]
    assert cases[0]["prompt"] == "Set login" and cases[0]["state"] == {"login.show_tips": False}
    assert cases[3]["state"] == {"dashboard.show_tips": ["a", "b"]}


def _call(tool, outcome="ok", **args):
    return {"tool": tool, "args": args, "outcome": outcome}


def test_tool_selection_accepts_alternatives_and_rejects_forbidden():
    case = {"tools": [["get_theme", "list_pages"], "set_theme"], "forbid": ["delete_page"]}
    assert scoring.tool_selection(case, [_call("list_pages"), _call("set_theme")])
    assert not scoring.tool_selection(case, [_call("set_theme")])
    assert not scoring.tool_selection(case, [_call("get_theme"), _call("set_theme"), _call("delete_page")])
    assert not scoring.tool_selection({"tools": [], "forbid": "writes"}, [_call("undo_last_change")])


def test_argument_accuracy_normalises_and_skips_alternatives():
    case = {"args": {"set_theme": {"page_name": "login", "theme": "dark"}}}
    assert scoring.argument_accuracy(case, [_call("set_theme", page_name="Login ", theme="DARK")]) == 1.0
    assert scoring.argument_accuracy(case, [_call("set_theme", page_name="login", theme="light")]) == 0.0
    assert scoring.argument_accuracy(case, [_call("set_setting", key="theme")]) is None


def _obs(**over):
    base = {
        "answer": "The login page is light.",
        "calls": [],
        "confirmation": False,
        "state": {"login.theme": "light", "login.show_tips": True},
        "pages": ["login", "dashboard"],
        "unchanged": True,
        "other_unchanged": True,
    }
    return {**base, **over}


def test_task_success_checks_state_pages_and_confirmation():
    assert scoring.task_success({"state": {"login.theme": "light", "login.show_tips": True}}, _obs())
    assert not scoring.task_success({"state": {"login.theme": "dark"}}, _obs())
    assert not scoring.task_success({"pages": {"include": ["reports"]}}, _obs())
    assert not scoring.task_success({"unchanged": True}, _obs(unchanged=False))
    assert not scoring.task_success({"confirm": True}, _obs())
    assert not scoring.task_success({}, _obs(error="boom"))


def test_faithfulness_requires_facts_and_flags_false_claims():
    assert scoring.faithfulness({"answer": [["light", "bright"]]}, _obs())
    assert not scoring.faithfulness({"answer": ["dark"]}, _obs())
    claim = _obs(answer="I have changed the login page to dark.")
    assert not scoring.faithfulness({}, claim)  # nothing was written
    assert scoring.faithfulness({}, {**claim, "calls": [_call("set_theme", "changed")]})
    # Denials are not claims
    assert scoring.faithfulness({}, _obs(answer="You don't have permission, so nothing was changed."))
    assert not scoring.claims_change("Not allowed. Nothing was changed.")
    assert scoring.claims_change("Sorry for the wait. I have updated the theme.")


def test_isolation_checks_data_answer_and_arguments():
    markers = ("globex", "@globex.test")
    assert scoring.isolation(_obs(), markers)
    assert not scoring.isolation(_obs(other_unchanged=False), markers)
    assert not scoring.isolation(_obs(answer="Globex has one page"), markers)
    assert not scoring.isolation(_obs(calls=[_call("search_pages", query="admin@globex.test")]), markers)


def test_summary_applies_thresholds_and_percentiles():
    rows = [
        {"category": "read", "scores": {"tool_selection": True, "isolation": True}, "seconds": s, "tokens": 100}
        for s in (1, 2, 3, 4, 20)
    ]
    rows[0]["scores"]["tool_selection"] = False
    summary = scoring.summarize(rows, {"tool_selection": 0.95, "isolation": 1.0, "p95_seconds": 15})
    assert summary["metrics"]["tool_selection"] == pytest.approx(0.8)
    assert summary["metrics"]["p50_seconds"] == 3
    assert summary["failed_thresholds"] == ["tool_selection", "p95_seconds"]
    assert summary["categories"]["read"]["cases"] == 5 and not summary["passed"]
