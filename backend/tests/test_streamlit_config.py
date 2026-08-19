"""Security contract for the local-only Streamlit server."""

from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_streamlit_is_bound_to_loopback_with_web_protections() -> None:
    config_path = PROJECT_ROOT / ".streamlit" / "config.toml"

    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)

    server = config["server"]
    assert server["address"] == "127.0.0.1"
    assert server["enableCORS"] is True
    assert server["enableXsrfProtection"] is True


def test_frontend_keeps_sidebar_reopen_control_and_centers_workspace_tabs() -> None:
    """Guard the two navigation regressions reported from the real UI."""
    styles_path = PROJECT_ROOT / "frontend" / "ui_styles.py"
    styles = styles_path.read_text(encoding="utf-8")

    assert '[data-testid="stSidebarCollapsedControl"]' in styles
    assert "visibility: visible !important;" in styles
    assert "pointer-events: auto !important;" in styles
    assert 'div[role="radiogroup"] > label' in styles
    assert "align-items: center !important;" in styles
    assert 'label[data-testid="stRadioOption"]' in styles
    assert '> div:not([data-testid="stMarkdownContainer"])' in styles


def test_frontend_styles_scope_tooltips_and_disabled_primary_buttons() -> None:
    """Prevent broad button rules from regressing compact help controls."""
    styles_path = PROJECT_ROOT / "frontend" / "ui_styles.py"
    styles = styles_path.read_text(encoding="utf-8")

    scoped_tooltip_button = (
        '[data-testid="stWidgetLabel"] [data-testid="stTooltipIcon"] button'
    )
    assert scoped_tooltip_button in styles
    assert '\n[data-testid="stTooltipIcon"] button {' not in styles
    assert "background: transparent !important;" in styles
    assert 'section button[data-testid="stBaseButton-secondary"]' in styles
    assert 'button[data-testid="stBaseButton-primary"]:disabled' in styles
    assert "background: #e2e5e9 !important;" in styles
    assert 'div[data-testid="stMetric"]' in styles
    assert 'div[data-testid="stColumn"]' in styles
    assert "@media (max-width: 1100px)" in styles
    assert ".st-key-nm_two_pane_summary" in styles
    assert ".st-key-nm_two_pane_rag" in styles


def test_frontend_uses_readable_narrow_pane_metric_grids() -> None:
    app_path = PROJECT_ROOT / "frontend" / "streamlit_app.py"
    source = app_path.read_text(encoding="utf-8")

    assert "def render_metric_grid" in source
    assert source.count("render_metric_grid(") >= 5
    assert 'key="settings_generation_card"' in source
    assert 'key="settings_embedding_card"' in source


def test_frontend_has_non_secret_model_management_page() -> None:
    app_path = PROJECT_ROOT / "frontend" / "streamlit_app.py"
    source = app_path.read_text(encoding="utf-8")

    assert '"模型与密钥"' in source
    assert "def render_model_settings_page" in source
    assert 'type="password"' in source
    assert "留空，保留现有密钥" in source
    assert "清除现有生成模型密钥" in source
    assert "清除现有向量模型密钥" in source
    assert "摘要并发" in source
    assert '"自动调节"' in source
    assert '"最大并发请求数"' in source
    assert '"summary_concurrency_mode"' in source
    assert '"summary_max_concurrency"' in source
    assert "恢复 .env 配置" in source
