import os
import re

import pytest


def _read(repo_root: str, rel_path: str) -> str:
    p = os.path.join(repo_root, rel_path)
    with open(p, "r", encoding="utf-8") as f:
        return f.read()

def _require_docs_sync() -> bool:
    """
    Local dev workflow:
      - edit + serve `frontend/`
      - run pytest without needing `docs/` perfectly synced every time
      - when ready to publish, sync `docs/` and run: REQUIRE_DOCS_SYNC=1 pytest -q
    """
    return os.environ.get("REQUIRE_DOCS_SYNC", "") == "1"


def test_frontend_has_first_load_modal_and_onboarding_keys():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    content = _read(repo_root, "frontend/main.js")
    assert "function showFirstLoadModal" in content
    assert "ms_intro_modal_v1" in content
    assert "ms_onboarding_v1" in content


def test_frontend_has_furious_glow_css_and_modal_css():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    css = _read(repo_root, "frontend/css/style.css")
    assert "#filters-toggle.furious-glow" in css
    assert "@keyframes furiousGlow" in css
    assert ".ms-intro-backdrop" in css
    assert ".ms-intro-modal" in css


def test_frontend_stat_cards_have_tooltip_html():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    html = _read(repo_root, "frontend/index.html")
    # We expect 4 analytics cards with rich tooltip content
    assert html.count("data-tooltip-html=") >= 4
    assert "SAR Detections" in html
    assert "SAR matched to AIS" in html
    assert "Dark Traffic Clusters" in html


def test_frontend_tooltips_support_tap_toggle():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    js = _read(repo_root, "frontend/main.js")
    # Contract: tap-to-toggle uses a class and a click listener for closing
    assert "tooltip-open" in js
    assert "document.addEventListener('click', closeAll" in js


def test_no_toast_popups_only_console_logging():
    """
    UX contract: we do not show toast/pop-up messages for success/error.
    The helpers must log to console only (and keep their signatures).
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    fe_utils = _read(repo_root, "frontend/utils.js")

    # No DOM creation for toasts
    assert "success-message" not in fe_utils
    assert "error-message" not in fe_utils
    # Console-only behavior
    assert "console.info" in fe_utils
    assert "console.error" in fe_utils

    if not _require_docs_sync():
        pytest.skip("docs/ sync checks disabled for local frontend dev. Set REQUIRE_DOCS_SYNC=1 to enable.")
    docs_utils = _read(repo_root, "docs/utils.js")
    assert "success-message" not in docs_utils
    assert "error-message" not in docs_utils
    assert "console.info" in docs_utils
    assert "console.error" in docs_utils


def test_stats_grid_base_and_sidebar_layout():
    """
    UX contract: standalone .stats-grid keeps a 1×4 column layout for the four stat cards.
    Inside .map-right-sidebar #summary-stats, the grid is 2×2 (repeat(2,...)) so the
    narrow sidebar stays readable (replaces the old bottom overlay layout).
    """

    def _assert_layout(css: str) -> None:
        base_blocks = re.findall(r"(?m)^\.stats-grid\s*\{[^}]*\}", css, flags=re.DOTALL)
        assert base_blocks, "Expected standalone .stats-grid rule"
        assert any("grid-template-columns" in b and "repeat(4" in b for b in base_blocks), (
            "Expected base .stats-grid to use four columns"
        )

        sidebar_blocks = re.findall(
            r"\.map-right-sidebar\s+#summary-stats\s+\.stats-grid\s*\{[^}]*\}",
            css,
            flags=re.DOTALL,
        )
        assert sidebar_blocks, "Expected .map-right-sidebar #summary-stats .stats-grid override(s)"
        for block in sidebar_blocks:
            assert "grid-template-columns" in block
            assert "repeat(2" in block

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    _assert_layout(_read(repo_root, "frontend/css/style.css"))
    if not _require_docs_sync():
        pytest.skip("docs/ sync checks disabled for local frontend dev. Set REQUIRE_DOCS_SYNC=1 to enable.")
    _assert_layout(_read(repo_root, "docs/css/style.css"))


def test_stat_tooltips_are_viewport_clamped_and_body_rendered():
    """
    UX contract: stat-card tooltips should never bleed off-screen.
    Implementation contract:
      - tooltip divs are appended to document.body (so transforms don't break positioning)
      - CSS uses a global .custom-tooltip selector (not only as a child of .stat-card)
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    fe_js = _read(repo_root, "frontend/main.js")
    fe_css = _read(repo_root, "frontend/css/style.css")

    # JS: body append + clamped positioning
    assert "document.body.appendChild(tooltipDiv)" in fe_js
    assert "Math.max" in fe_js and "Math.min" in fe_js  # clamp logic exists
    assert "positionTooltip" in fe_js

    # CSS: global tooltip class exists (and the old scoped selector is gone)
    assert ".custom-tooltip {" in fe_css
    assert "stat-card[data-tooltip-html] .custom-tooltip" not in fe_css

    if not _require_docs_sync():
        pytest.skip("docs/ sync checks disabled for local frontend dev. Set REQUIRE_DOCS_SYNC=1 to enable.")
    docs_js = _read(repo_root, "docs/main.js")
    docs_css = _read(repo_root, "docs/css/style.css")
    assert "document.body.appendChild(tooltipDiv)" in docs_js
    assert "Math.max" in docs_js and "Math.min" in docs_js
    assert "positionTooltip" in docs_js
    assert ".custom-tooltip {" in docs_css
    assert "stat-card[data-tooltip-html] .custom-tooltip" not in docs_css


def test_about_container_stretches_with_open_children():
    """
    UX contract: when #about-container is open, it should stretch with its accordion children.
    (No internal max-height clamp on the container/content, aside from the collapsed state.)
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    css = _read(repo_root, "frontend/css/style.css")

    about = re.search(r"#about-container\s*\{[^}]*\}", css, flags=re.DOTALL)
    assert about, "Expected #about-container rule"
    assert "max-height: none" in about.group(0)

    # Match the standalone rule, not the collapsed selector
    content = re.search(r"(?m)^\s*\.about-accordion-content\s*\{[^}]*\}", css, flags=re.DOTALL)
    assert content, "Expected standalone .about-accordion-content rule"
    assert "max-height: none" in content.group(0)
    assert "max-height: 300px" not in content.group(0)


def test_docs_mirror_contains_modal_css_and_tooltips():
    if not _require_docs_sync():
        pytest.skip("docs/ sync checks disabled for local frontend dev. Set REQUIRE_DOCS_SYNC=1 to enable.")
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    docs_css = _read(repo_root, "docs/css/style.css")
    docs_html = _read(repo_root, "docs/index.html")
    docs_js = _read(repo_root, "docs/main.js")

    assert ".ms-intro-backdrop" in docs_css
    assert ".ms-intro-modal" in docs_css
    assert "#filters-toggle.furious-glow" in docs_css or "furiousGlow" in docs_css

    assert "data-tooltip-html=" in docs_html
    assert "SAR matched to AIS" in docs_html

    # Tooltip tap-toggle logic mirrored
    assert "tooltip-open" in docs_js
    assert "document.addEventListener('click', closeAll" in docs_js


def test_docs_is_synced_from_frontend_static_bundle():
    """
    Publishing contract: GitHub Pages serves `docs/`, which must be kept in sync with `frontend/`.

    We keep this strict for the core static bundle so deploys never "look different"
    across environments (local vs GitHub Pages).
    """
    if not _require_docs_sync():
        pytest.skip("docs/ sync checks disabled for local frontend dev. Set REQUIRE_DOCS_SYNC=1 to enable.")
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    for rel in [
        "index.html",
        "main.js",
        "utils.js",
        "config.js",
        "css/style.css",
    ]:
        fe = _read(repo_root, f"frontend/{rel}")
        docs = _read(repo_root, f"docs/{rel}")
        assert fe == docs, f"docs/{rel} is out of sync with frontend/{rel}. Run ./scripts/sync_docs.sh"


def test_date_validation_uses_local_midnight_not_utc_parsing():
    """
    Date input contract: avoid timezone drift from `new Date("YYYY-MM-DD")` (UTC parse).
    The UI should parse date-input values as local-midnight calendar dates.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    fe_js = _read(repo_root, "frontend/main.js")

    assert "function parseLocalDateInput" in fe_js
    assert "const max = formatDateYYYYMMDD(maxDate);" in fe_js
    assert "const start = parseLocalDateInput(startDate);" in fe_js
    assert "const end = parseLocalDateInput(endDate);" in fe_js

