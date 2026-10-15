from pydantic_ai.models.test import TestModel

from app.agent.agent import agent
from app.workflow.graph import run_workflow
from app.workflow.utils import extract_uuid
from tests.conftest import TENANT_A

UNKNOWN_TENANT = "00000000-0000-0000-0000-000000000000"


def test_extract_uuid_from_prompt():
    assert extract_uuid(f"toggle login for {TENANT_A} please") == TENANT_A


def test_extract_uuid_falls_back_to_default():
    assert extract_uuid("toggle login") == TENANT_A


def test_valid_tenant_runs_agent():
    with agent.override(model=TestModel(custom_output_text="done")):
        state = run_workflow(f"theme of login for {TENANT_A}?", TENANT_A)
    assert state["is_valid"] is True
    assert state["agent_response"] == "done"


def test_unknown_tenant_is_blocked_without_calling_model():
    model = TestModel()
    with agent.override(model=model):
        state = run_workflow("toggle login", UNKNOWN_TENANT)
    assert state["is_valid"] is False
    assert state["agent_response"].startswith("Operation blocked")
    assert model.last_model_request_parameters is None
