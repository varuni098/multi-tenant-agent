import pytest
from sqlalchemy import func, select

from app.db.models import AuditLog, OutboxEvent
from app.db.seed import seed
from app.db.session import session_scope
from app.services import admin
from app.services import settings as svc
from app.services.errors import (
    InvalidSettingValue,
    NotEntitled,
    NothingToUndo,
    PageNotFound,
    TenantNotFound,
    UnknownSettingKey,
)
from tests.conftest import TENANT_A, TENANT_B


def _count(model) -> int:
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


# --- Resolution ---


def test_page_override_wins():
    value = svc.get_setting(TENANT_A, "theme", "dashboard")
    assert (value.value, value.source) == ("dark", "page")


def test_inherits_from_template():
    value = svc.get_setting(TENANT_A, "theme", "settings")
    assert (value.value, value.source) == ("light", "template")


def test_tenant_default_beats_template():
    svc.set_setting(TENANT_A, "theme", "dark")  # tenant-wide
    value = svc.get_setting(TENANT_A, "theme", "settings")
    assert (value.value, value.source) == ("dark", "tenant")
    # ...but the page override still wins
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"


def test_falls_back_to_key_default():
    value = svc.get_setting(TENANT_A, "language", "login")
    assert (value.value, value.source) == ("en", "default")


def test_effective_settings_lists_every_key():
    keys = {s.key for s in svc.get_effective_settings(TENANT_A, "login")}
    assert keys == {"theme", "language", "layout", "brand_color", "show_tips"}


def test_template_change_affects_inherited_pages_only():
    admin.apply_template(TENANT_A, "dark_first")
    assert svc.get_setting(TENANT_A, "theme", "settings").value == "dark"
    assert svc.get_setting(TENANT_A, "layout", "settings").value == "compact"
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"


# --- Lookups ---


def test_page_lookup_is_case_insensitive():
    assert svc.get_setting(TENANT_A, "theme", "  DashBoard ").value == "dark"


def test_missing_page_lists_available_pages():
    with pytest.raises(PageNotFound, match="Available pages: dashboard, login, settings"):
        svc.get_setting(TENANT_A, "theme", "billing")


def test_unknown_key():
    with pytest.raises(UnknownSettingKey, match="Available settings"):
        svc.get_setting(TENANT_A, "font", "login")


def test_unknown_tenant():
    with pytest.raises(TenantNotFound):
        svc.list_pages("00000000-0000-0000-0000-000000000000")


def test_list_pages():
    pages = {p.name: p.theme for p in svc.list_pages(TENANT_A)}
    assert pages == {"dashboard": "dark", "login": "light", "settings": "light"}


@pytest.mark.parametrize(
    ("query", "expected"),
    [("login screen", "login"), ("dash", "dashboard"), ("Settings page", "settings"), ("logn", "login")],
)
def test_search_pages(query, expected):
    assert svc.search_pages(TENANT_A, query)[0].name == expected


def test_search_pages_no_match():
    assert svc.search_pages(TENANT_A, "billing invoices") == []


# --- Writes ---


def test_set_theme_records_audit_and_outbox():
    result = svc.set_theme(TENANT_A, "login", "dark", actor="tester", request_id="req-1")
    assert (result.before, result.after, result.changed) == ("light", "dark", True)
    assert svc.get_setting(TENANT_A, "theme", "login").value == "dark"
    with session_scope() as s:
        entry = s.scalars(select(AuditLog)).one()
        assert (entry.actor, entry.action, entry.before, entry.after, entry.request_id) == (
            "tester",
            "set_setting",
            "light",
            "dark",
            "req-1",
        )
        event = s.scalars(select(OutboxEvent)).one()
        assert event.event_type == "setting.changed"
        assert event.payload["after"] == "dark"
        assert event.published_at is None


def test_set_same_value_is_noop():
    result = svc.set_theme(TENANT_A, "login", "light")
    assert result.changed is False
    assert _count(AuditLog) == 0
    assert _count(OutboxEvent) == 0


def test_enum_value_is_normalised():
    assert svc.set_theme(TENANT_A, "login", " Dark ").after == "dark"


def test_invalid_enum_value():
    with pytest.raises(InvalidSettingValue, match="one of light, dark"):
        svc.set_theme(TENANT_A, "login", "blue")


def test_bool_coercion():
    assert svc.set_setting(TENANT_A, "show_tips", "no", "login").after is False


def test_feature_gated_setting_requires_plan():
    with pytest.raises(NotEntitled, match="custom_branding"):
        svc.set_setting(TENANT_A, "brand_color", "#ff0000", "login")  # free plan
    assert svc.set_setting(TENANT_B, "brand_color", "#ff0000", "login").after == "#ff0000"  # pro plan


def test_pattern_validation():
    with pytest.raises(InvalidSettingValue):
        svc.set_setting(TENANT_B, "brand_color", "red", "login")


def test_writes_are_isolated_per_tenant():
    svc.set_theme(TENANT_A, "login", "dark")
    svc.set_setting(TENANT_A, "theme", "dark")
    assert svc.get_setting(TENANT_B, "theme", "login").value == "dark"  # unchanged from seed
    assert {p.name for p in svc.list_pages(TENANT_B)} == {"login"}


def test_failed_write_rolls_back():
    with pytest.raises(PageNotFound):
        admin.bulk_set_setting(TENANT_A, ["login", "billing"], "theme", "dark")
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"
    assert _count(AuditLog) == 0


def test_bulk_set_setting():
    results = admin.bulk_set_setting(TENANT_A, ["login", "dashboard", "settings"], "theme", "dark")
    assert [r.changed for r in results] == [True, False, True]
    assert {p.theme for p in svc.list_pages(TENANT_A)} == {"dark"}


# --- History and undo ---


def test_change_history_newest_first():
    svc.set_theme(TENANT_A, "login", "dark")
    svc.set_setting(TENANT_A, "layout", "compact", "dashboard")
    history = svc.get_change_history(TENANT_A)
    assert [(h.page_name, h.key) for h in history] == [("dashboard", "layout"), ("login", "theme")]
    assert [h.key for h in svc.get_change_history(TENANT_A, "LOGIN")] == ["theme"]
    assert svc.get_change_history(TENANT_B) == []


def test_undo_restores_previous_override():
    svc.set_theme(TENANT_A, "login", "dark")
    result = svc.undo_last_change(TENANT_A)
    assert (result.before, result.after, result.changed) == ("dark", "light", True)
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"


def test_undo_of_new_override_returns_to_inherited():
    svc.set_theme(TENANT_A, "settings", "dark")
    svc.undo_last_change(TENANT_A, "settings")
    value = svc.get_setting(TENANT_A, "theme", "settings")
    assert (value.value, value.source) == ("light", "template")


def test_undo_walks_back_through_history():
    svc.set_theme(TENANT_A, "login", "dark")
    svc.set_setting(TENANT_A, "layout", "compact", "login")
    svc.undo_last_change(TENANT_A)  # layout
    assert svc.get_setting(TENANT_A, "layout", "login").value == "comfortable"
    assert svc.get_setting(TENANT_A, "theme", "login").value == "dark"
    svc.undo_last_change(TENANT_A)  # theme
    assert svc.get_setting(TENANT_A, "theme", "login").value == "light"
    with pytest.raises(NothingToUndo):
        svc.undo_last_change(TENANT_A)


def test_undo_is_per_tenant():
    svc.set_theme(TENANT_A, "login", "dark")
    with pytest.raises(NothingToUndo):
        svc.undo_last_change(TENANT_B)


# --- Admin ---


def test_create_and_delete_page():
    created = admin.create_page(TENANT_A, "Billing", theme="dark")
    assert (created.name, created.theme) == ("billing", "dark")
    with pytest.raises(Exception, match="already exists"):
        admin.create_page(TENANT_A, "billing")
    admin.delete_page(TENANT_A, "billing")
    assert "billing" not in {p.name for p in svc.list_pages(TENANT_A)}
    actions = [h.action for h in svc.get_change_history(TENANT_A)]
    assert actions[:3] == ["delete_page", "set_setting", "create_page"]


def test_seed_is_idempotent():
    assert seed() == 0
