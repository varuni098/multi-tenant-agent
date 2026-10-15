from app.db.repository import get_all_tenant_pages, get_tenant_page_theme, toggle_tenant_page_theme
from app.db.seed import seed
from tests.conftest import TENANT_A, TENANT_B


def test_get_theme():
    assert "currently in light mode" in get_tenant_page_theme(TENANT_A, "login")


def test_get_theme_is_case_insensitive():
    assert "currently in dark mode" in get_tenant_page_theme(TENANT_A, "DashBoard")


def test_get_theme_missing_page():
    assert get_tenant_page_theme(TENANT_A, "billing").startswith("No page entry found")


def test_toggle_theme_round_trip():
    assert "from light to dark" in toggle_tenant_page_theme(TENANT_A, "login")
    assert "currently in dark mode" in get_tenant_page_theme(TENANT_A, "login")
    assert "from dark to light" in toggle_tenant_page_theme(TENANT_A, "login")


def test_toggle_only_touches_own_tenant():
    toggle_tenant_page_theme(TENANT_A, "login")
    assert "currently in dark mode" in get_tenant_page_theme(TENANT_B, "login")


def test_toggle_missing_page():
    assert toggle_tenant_page_theme(TENANT_A, "billing").startswith("Error:")


def test_get_all_pages():
    pages = get_all_tenant_pages()
    assert {p["tenant_uuid"] for p in pages} == {TENANT_A, TENANT_B}
    assert len(pages) == 3


def test_seed_is_idempotent():
    assert seed() == 0
