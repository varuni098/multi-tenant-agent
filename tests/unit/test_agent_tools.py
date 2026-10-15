import pytest
from pydantic_ai import ModelRetry

from app.agent import agent as tools
from app.services.schemas import ChangeResult, SettingValue
from tests.conftest import TENANT_A

CTX = None  # tools don't use the run context yet


def test_tools_registered():
    names = set(tools.agent._function_toolset.tools)
    assert names == {
        "list_pages",
        "search_pages",
        "get_theme",
        "set_theme",
        "get_settings_for_page",
        "set_setting",
        "get_change_history",
        "undo_last_change",
    }


def test_get_theme_returns_structured_value():
    assert tools.get_theme(CTX, TENANT_A, "login") == SettingValue(key="theme", value="light", source="page")


def test_set_theme():
    result = tools.set_theme(CTX, TENANT_A, "login", "dark")
    assert isinstance(result, ChangeResult) and result.changed


def test_wrong_page_asks_model_to_retry_with_options():
    with pytest.raises(ModelRetry, match="Available pages: dashboard, login, settings"):
        tools.get_theme(CTX, TENANT_A, "login page")


def test_invalid_value_asks_model_to_retry():
    with pytest.raises(ModelRetry, match="light, dark"):
        tools.set_theme(CTX, TENANT_A, "login", "purple")


def test_non_retryable_error_becomes_message():
    result = tools.set_setting(CTX, TENANT_A, "brand_color", "#ff0000", "login")
    assert isinstance(result, str) and "requires the 'custom_branding' feature" in result


def test_undo_with_nothing_to_undo():
    assert tools.undo_last_change(CTX, TENANT_A) == "There is no change to undo."
