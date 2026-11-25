from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")


def test_wallet_menu_is_portaled_to_body_outside_sticky_header():
    assert "function initWalletPickerPortal()" in HTML
    assert "document.body.appendChild(menu)" in HTML
    assert "initWalletPickerPortal();\nrefreshWalletPicker();" in HTML


def test_wallet_menu_uses_fixed_global_layer_and_internal_scrolling():
    assert ".wallet-picker-menu{position:fixed" in HTML
    assert "z-index:10040" in HTML
    assert "overflow-y:auto" in HTML
    assert "overscroll-behavior:contain" in HTML


def test_wallet_menu_is_positioned_within_visible_viewport():
    assert "getBoundingClientRect()" in HTML
    assert "window.visualViewport" in HTML
    assert "viewportHeight" in HTML
    assert "menu.style.maxHeight=available+'px'" in HTML


def test_wallet_menu_closes_accessibly_and_outside_click_accounts_for_portal():
    assert "!menu?.contains(event.target)" in HTML
    assert "event.key==='Escape'" in HTML
    assert "window.addEventListener('scroll'" in HTML
    assert "!menu.contains(event.target)" in HTML


def test_wallet_selection_and_wallet_actions_remain_present():
    for token in ("selectWallet(wallet.id)", "openWalletEditor('create')", "openWalletEditor('rename')", "deleteSelectedWallet()"):
        assert token in HTML


def test_release_version_is_615():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "6.1.17"
    assert 'version: "6.1.17"' in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
