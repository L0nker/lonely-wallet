from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_long_role_explanation_is_removed_from_settings_users_panel():
    start = HTML.index('<div id="securityUsersPanel"')
    end = HTML.index('<div id="securityUsersList"', start)
    panel_header = HTML[start:end]
    assert "Master: cria usuários comuns e Admins" not in panel_header
    assert "Usuário comum: pode somente redefinir o próprio PIN" not in panel_header
    assert '<h2>Gerenciar usuários</h2>' in panel_header
    assert 'onclick="loadManagedUsers()"' in panel_header


def test_mobile_hides_only_duplicate_summary_add_operation_button():
    assert 'class="primary summary-mobile-duplicate-action"' in HTML
    css_start = HTML.index('<style id="v6-1-16-mobile-summary-action">')
    css_end = HTML.index('</style>', css_start)
    mobile_css = HTML[css_start:css_end]
    assert "@media (max-width: 760px)" in mobile_css
    assert "#page-resumo .summary-mobile-duplicate-action { display: none !important; }" in mobile_css
    # Header action and summary refresh control remain present.
    header = HTML[HTML.index('<header class="top"'):HTML.index('</header>')]
    summary = HTML[HTML.index('<section id="page-resumo"'):HTML.index('<section id="page-proventos"')]
    assert 'onclick="openModal(\'op\')"' in header
    assert 'onclick="refresh()"' in summary


def test_permissions_and_backend_are_not_changed_by_copy_removal():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "Somente o Master pode promover ou rebaixar contas existentes." in main
    assert "current_user_role" in HTML
    assert "can_change_role" in HTML


def test_release_version_metadata_is_consistent():
    assert VERSION == "6.1.18"
    assert 'version: "6.1.18"' in COMPOSE
    assert 'v=6.1.18' in COMPOSE
    assert "Versão 6.1.18" in README
    assert "Destaques da versão 6.1.17" in README
