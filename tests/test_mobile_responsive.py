from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
STYLE_START = HTML.index('<style id="v5-1-mobile-responsive">')
STYLE_END = HTML.index("</style>", STYLE_START)
MOBILE_CSS = HTML[STYLE_START:STYLE_END]


def test_mobile_responsive_layer_covers_phone_and_tablet_breakpoints():
    assert '@media (max-width: 760px)' in MOBILE_CSS
    assert '@media (max-width: 420px)' in MOBILE_CSS
    assert '@media (max-width: 350px)' in MOBILE_CSS
    assert 'min-width: 280px' in MOBILE_CSS
    assert 'min-width: 320px' not in MOBILE_CSS
    assert '@media (min-width: 761px) and (max-width: 1100px)' in MOBILE_CSS


def test_mobile_navigation_is_single_row_and_touch_scrollable():
    assert '.nav {' in MOBILE_CSS
    assert 'flex-wrap: nowrap !important' in MOBILE_CSS
    assert 'overflow-x: auto !important' in MOBILE_CSS
    for page in ('resumo', 'proventos', 'patrimonio', 'lancamentos', 'distribuicao', 'apis', 'seguranca'):
        assert f'data-page="{page}"' in HTML


def test_mobile_tables_keep_readable_columns_inside_horizontal_scrollers():
    assert '.tablewrap:has(> table)' in MOBILE_CSS
    assert 'overflow-x: auto !important' in MOBILE_CSS
    assert 'table-layout: auto !important' in MOBILE_CSS
    assert 'min-width: 930px !important' in MOBILE_CSS
    assert 'min-width: 1120px !important' in MOBILE_CSS


def test_mobile_modal_and_form_controls_are_touch_friendly():
    assert 'min-height: 44px' in MOBILE_CSS
    assert 'max-height: min(92dvh, 900px) !important' in MOBILE_CSS
    assert 'grid-template-columns: minmax(0, 1fr) !important' in MOBILE_CSS
    assert 'env(safe-area-inset-bottom)' in MOBILE_CSS


def test_mobile_styles_preserve_all_application_pages_and_integrations():
    for page in ('page-resumo', 'page-proventos', 'page-patrimonio', 'page-distribuicao', 'page-lancamentos', 'page-apis', 'page-seguranca'):
        assert f'id="{page}"' in HTML
    assert 'id="apiPluggyGrid"' in HTML
    assert 'id="apiProvidersGrid"' in HTML


def test_release_version_and_financial_cache_contract_are_consistent():
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    assert version == '6.1.14'
    main = (ROOT / 'app/main.py').read_text(encoding='utf-8')
    assert 'DASHBOARD_CACHE_FORMAT = 28' in main
    compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    assert 'v=6.1.14' in compose
