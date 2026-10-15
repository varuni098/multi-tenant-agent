from pydantic_ai.exceptions import ModelAPIError

from app.db.models import Tenant
from app.db.session import session_scope
from app.harness.confirmations import confirm
from app.harness.intent import OFF_TOPIC_REPLY
from app.services import settings as svc
from app.workflow.graph import SAFE_FAILURE_REPLY, run_workflow
from tests.conftest import ADMIN_A, EDITOR_A, TENANT_A


def _run(prompt, identity=EDITOR_A, session="s"):
    return run_workflow(prompt, identity, "req-test", session)


def test_off_topic_skips_the_model(script):
    s = script("should not run")
    result = _run("hello there")
    assert (result.answer, result.intent, s.calls) == (OFF_TOPIC_REPLY, "off_topic", 0)


def test_read_request_gets_no_write_tools(script):
    s = script("it is light")
    _run("what theme is the login page?", ADMIN_A)
    assert s.offered[0] == ["get_change_history", "get_settings_for_page", "get_theme", "list_pages", "search_pages"]


def test_write_records_actions(script):
    script([("get_theme", {"page_name": "login"})], [("set_theme", {"page_name": "login", "theme": "dark"})], "Done.")
    result = _run("toggle the login page")
    assert [a.tool for a in result.actions] == ["set_theme"]
    assert result.answer == "Done."
    assert svc.get_setting(TENANT_A, "theme", "login").value == "dark"
    assert svc.get_change_history(TENANT_A)[0].actor == "editor@acme.test"


def test_wrong_page_name_is_retried_with_options(script):
    s = script([("get_theme", {"page_name": "login page"})], [("get_theme", {"page_name": "login"})], "light")
    assert _run("theme of the login page?").answer == "light"
    assert s.calls == 3


def test_false_change_claim_is_retried_then_replaced(script):
    script("I have changed the theme to dark.", "The theme has been changed.")
    result = _run("make login dark")
    assert result.answer == SAFE_FAILURE_REPLY
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"


def test_false_claim_corrected_on_retry(script):
    script(
        "I have changed it.",
        [("set_theme", {"page_name": "login", "theme": "dark"})],
        "The login page has been changed to dark.",
    )
    result = _run("make login dark")
    assert result.answer == "The login page has been changed to dark."
    assert len(result.actions) == 1


def test_destructive_admin_action_waits_for_confirmation(script):
    script([("delete_page", {"page_name": "Login"})], "I deleted the login page.")
    result = _run("delete the login page", ADMIN_A)
    assert result.confirmation is not None
    assert result.answer == "Please confirm: Delete page 'login' and its 1 setting override(s)."
    assert result.actions == []
    assert "login" in {p.name for p in svc.list_pages(TENANT_A)}

    action = confirm(ADMIN_A, result.confirmation.token, "req-confirm")
    assert action.message == "Deleted page 'login'."
    assert "login" not in {p.name for p in svc.list_pages(TENANT_A)}


def test_bulk_change_is_validated_before_confirmation(script):
    script(
        [("bulk_set_setting", {"page_names": ["login", "billing"], "key": "theme", "value": "dark"})],
        [("bulk_set_setting", {"page_names": ["login", "dashboard"], "key": "theme", "value": "Dark"})],
        "ok",
    )
    result = _run("set login and dashboard to dark", ADMIN_A)
    assert result.confirmation is not None
    assert "2 page(s)" in result.confirmation.summary and "1 will change" in result.confirmation.summary
    confirm(ADMIN_A, result.confirmation.token, "req")
    assert {p.theme for p in svc.list_pages(TENANT_A) if p.name in ("login", "dashboard")} == {"dark"}


def test_create_page_runs_without_confirmation(script):
    script([("create_page", {"page_name": "billing", "theme": "dark"})], "Created.")
    result = _run("create a billing page", ADMIN_A)
    assert [a.tool for a in result.actions] == ["create_page"]
    assert svc.get_setting(TENANT_A, "theme", "billing").value == "dark"


def test_inactive_tenant_is_blocked(script):
    s = script("no")
    with session_scope() as session:
        tenant = session.get(Tenant, TENANT_A)
        assert tenant is not None
        tenant.status = "suspended"
    result = _run("what theme is login")
    assert result.blocked and s.calls == 0


def test_runaway_tool_loop_hits_usage_limit(script, settings):
    settings.max_requests_per_run = 3
    script(*[[("get_theme", {"page_name": "login"})]] * 10)
    assert _run("what theme is login").answer == SAFE_FAILURE_REPLY


def test_model_outage_opens_circuit(script, settings):
    settings.breaker_failure_threshold = 2
    from pydantic_ai.models.function import FunctionModel

    from app.agent.agent import agent

    def down(messages, info):
        raise ModelAPIError("qwen3:8b", "connection refused")

    with agent.override(model=FunctionModel(down)):
        answers = [_run("what theme is login").answer for _ in range(3)]
    assert all("temporarily unavailable" in a for a in answers)
    from app.harness.reliability import _candidates

    assert not _candidates()[0].breaker.allow()


def test_conversation_history_is_kept_per_session(script):
    s = script("first", "second", "third")
    _run("what theme is login", session="a")
    _run("and the dashboard?", session="a")
    _run("what theme is login", session="b")
    assert s.message_counts == [1, 3, 1]
