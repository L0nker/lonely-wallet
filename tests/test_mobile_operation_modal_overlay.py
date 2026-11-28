from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
STYLE_START = HTML.index('<style id="v5-1-mobile-responsive">')
STYLE_END = HTML.index("</style>", STYLE_START)
MOBILE_CSS = HTML[STYLE_START:STYLE_END]


def test_mobile_modal_backdrop_sits_above_sticky_navigation_and_wallet_picker():
    assert ".nav {" in MOBILE_CSS
    assert "z-index: 40 !important" in MOBILE_CSS
    assert "z-index: 10060 !important" in MOBILE_CSS
    assert ".modalback, .utility-modal-back" in MOBILE_CSS
    assert "position: fixed !important" in MOBILE_CSS


def test_mobile_modal_follows_visual_viewport_when_keyboard_opens():
    assert "window.visualViewport" in HTML
    assert "--lw-mobile-visual-height" in HTML
    assert "--lw-mobile-visual-top" in HTML
    assert "visualViewport.addEventListener('resize',syncMobileModalViewport" in HTML
    assert "visualViewport.addEventListener('scroll',syncMobileModalViewport" in HTML
    assert "max-height: min(calc(var(--lw-mobile-visual-height, 100dvh) - 16px), 900px) !important" in MOBILE_CSS


def test_main_modal_locks_background_without_disabling_modal_scroll():
    assert "html.lw-main-modal-open,body.lw-main-modal-open" in HTML
    assert "overflow:hidden!important" in HTML
    assert "overflow-y: auto !important" in MOBILE_CSS
    assert "touch-action: pan-y" in MOBILE_CSS
    assert "overscroll-behavior: contain" in MOBILE_CSS
    for function in ("openGoalWizard", "openModal", "closeModal", "editOperation"):
        assert f"function {function}" in HTML or f"async function {function}" in HTML
    assert "setMainModalViewportLock(true)" in HTML
    assert "setMainModalViewportLock(false)" in HTML


def test_transaction_modal_stays_in_shared_overlay_and_keeps_form_actions():
    assert 'id="modalback"' in HTML
    assert 'id="opForm" class="formgrid operation-form"' in HTML
    assert "＋ Adicionar lançamento" in HTML
    assert 'onclick="closeModal()"' in HTML


def test_release_version_and_compose_metadata_match():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "6.1.18"
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'version: "6.1.18"' in compose
    assert compose.count("v=6.1.18") == 2
