"""Browser regression test for the kebab menus of the worker cards.

The defect only shows up AFTER a deck poll: htmx emits one `htmx:beforeSwap`
and two `htmx:afterSwap` per poll, and the second left the menu `<ul>` orphaned
in `<body>`, immune to Escape and to an outside click. A text test over
`app.js` cannot pin it, so the real `_workers_deck.html` template is served
here with the real assets (`app.js`, `app.css`, vendored htmx and idiomorph)
and driven with headless Chromium.

Playwright is NOT a project dependency: without it (or without its Chromium)
the whole module is skipped. To run it:

    uv run --with playwright python -m pytest tests/test_row_actions_kebab_browser.py

`KEBAB_SHOTS_DIR=<dir>` saves screenshots of each theme for visual review.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from orcaster_server.api.admin_ui import templates  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = SERVER_DIR / "orcaster_server" / "static"
BASE_HTML = SERVER_DIR / "orcaster_server" / "templates" / "base.html"

# Short poll so the test does not wait 3s per tick; the code path is the same
# as the `every 3s` of dashboard.html.
POLL = "400ms"

_INITIAL_STATUS = {"w1": "online", "w2": "paused", "w3": "offline"}
_NAMES = {"w1": "worker-a-1", "w2": "worker-b-1", "w3": "worker-c-1"}

COUNTS_JS = """() => ({
  open: document.querySelectorAll('details.row-actions[open]').length,
  portals: document.querySelectorAll('body > ul.row-actions-portal').length,
  uls: document.querySelectorAll('ul[dir="rtl"]').length,
  cards: document.querySelectorAll('.worker-card').length,
})"""

FOCUS_JS = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return 'body';
  if (el.tagName === 'SUMMARY') return 'summary:' + el.closest('.worker-card').id;
  return el.id || el.textContent.trim();
}"""

FOCUS_ON_BODY_JS = """() => document.activeElement === document.body
  && document.querySelector(':focus') === null"""


def _confirm_dialog_markup() -> str:
    source = BASE_HTML.read_text(encoding="utf-8")
    start = source.index('<dialog id="confirm-dialog"')
    return source[start : source.index("</dialog>", start) + len("</dialog>")]


def _build_app() -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    state: dict[str, Any] = {"polls": 0, "status": dict(_INITIAL_STATUS), "deleted": set()}
    dialog = _confirm_dialog_markup()

    def worker(wid: str) -> dict[str, Any]:
        # Values change on every poll so the morph has real work to do.
        tick = state["polls"]
        return {
            "id": wid,
            "name": _NAMES[wid],
            "hostname": _NAMES[wid],
            "status": state["status"][wid],
            "os": "linux",
            "arch": "amd64",
            "ram_mb": 16000,
            "ram_used_mb": 4000 + (tick % 7) * 100,
            "disk_mb": 100000,
            "disk_used_mb": 30000,
            "cpu_pct": 10 + tick % 5,
            "active_count": 1,
            "active_task_id": None,
            "free_disk_fraction": 0.7,
            "version": "0.52.2",
            "last_heartbeat_at": datetime.now(UTC),
            "heartbeat_age_seconds": 1,
            "task_secrets_supported": True,
        }

    @app.get("/", response_class=HTMLResponse)
    def index(theme: str = "light") -> str:
        # Same structure as dashboard.html: a pane with a `morph:innerHTML` poll
        # and the <span> that receives the OOB of the platform summary.
        return f"""<!doctype html>
<html lang="en" data-theme="{theme}"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/app.css">
<script src="/static/htmx.min.js"></script>
<script src="/static/idiomorph-ext.min.js"></script>
<script defer src="/static/app.js"></script></head>
<body hx-ext="morph"><main style="padding:2rem;max-width:960px">
<button id="outside-btn" type="button">Outside control</button>
<aside class="workers-pane"><h2>Workers
<span id="workers-platform-summary" class="workers-platform-summary"></span></h2>
<div class="workers-pane-body" aria-live="polite" hx-get="/admin/workers"
     hx-trigger="load, every {POLL}, refresh" hx-swap="morph:innerHTML">
<p class="muted">Loading workers...</p></div></aside></main>
{dialog}</body></html>"""

    @app.get("/admin/workers")
    def deck(request: Request) -> Any:
        state["polls"] += 1
        workers = [worker(wid) for wid in _NAMES if wid not in state["deleted"]]
        return templates.TemplateResponse(
            request,
            "_workers_deck.html",
            {
                "workers": workers,
                "common_platform": "linux · amd64",
                "heartbeat_timeout_seconds": 60,
                "claim_min_free_fraction": 0.0,
                "task_secrets_min_version": "0.47.0",
            },
        )

    @app.post("/admin/workers/{wid}/{op}")
    def pause_resume(wid: str, op: str) -> JSONResponse:
        state["status"][wid] = "paused" if op == "pause" else "online"
        return JSONResponse({"status": state["status"][wid]})

    @app.delete("/admin/workers/{wid}")
    def delete(wid: str) -> HTMLResponse:
        state["deleted"].add(wid)
        return HTMLResponse("")

    @app.post("/_reset")
    def reset() -> dict[str, bool]:
        state.update(polls=0, status=dict(_INITIAL_STATUS), deleted=set())
        return {"ok": True}

    return app


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(_build_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the harness server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as pw:
        try:
            instance = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - Chromium binary missing
            pytest.skip(f"Playwright Chromium not available: {exc}")
        yield instance
        instance.close()


@pytest.fixture(params=["light", "dark"])
def page(request: pytest.FixtureRequest, browser: Any, base_url: str) -> Iterator[Any]:
    theme = request.param
    context = browser.new_context(viewport={"width": 1100, "height": 820})
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg.request.post(f"{base_url}/_reset")
    pg.goto(f"{base_url}/?theme={theme}")
    pg.wait_for_selector("#worker-card-w3")
    pg.theme = theme
    yield pg
    context.close()
    assert errors == [], errors


def _polls(pg: Any, count: int = 1) -> None:
    for _ in range(count):
        with pg.expect_response("**/admin/workers"):
            pass
    # Margin for the swap and the settle (20ms) of the last poll to finish.
    pg.wait_for_timeout(120)


def _open(pg: Any, wid: str) -> None:
    pg.locator(f"#worker-card-{wid} summary").click()
    pg.wait_for_selector("body > ul.row-actions-portal")


def _counts(pg: Any) -> dict[str, int]:
    return pg.evaluate(COUNTS_JS)


def _assert_closed(pg: Any) -> None:
    counts = _counts(pg)
    assert counts["open"] == 0, counts
    assert counts["portals"] == 0, counts
    # One <ul> per card: the morph neither duplicated nor lost any.
    assert counts["uls"] == counts["cards"], counts


def _shot(pg: Any, name: str) -> None:
    shots = os.environ.get("KEBAB_SHOTS_DIR")
    if shots:
        # Let transitions finish (hover/focus, dialog entrance).
        pg.wait_for_timeout(350)
        Path(shots).mkdir(parents=True, exist_ok=True)
        pg.screenshot(path=str(Path(shots) / f"{pg.theme}-{name}.png"))


def test_only_one_menu_is_open_and_it_survives_polls(page: Any) -> None:
    _open(page, "w1")
    _polls(page, 3)
    counts = _counts(page)
    assert (counts["open"], counts["portals"]) == (1, 1), counts
    _shot(page, "1-menu-a-after-3-polls")

    _open(page, "w2")
    page.wait_for_function(
        "() => document.querySelector('#worker-card-w2 details').open"
        " && !document.querySelector('#worker-card-w1 details').open"
    )
    _polls(page, 3)
    counts = _counts(page)
    assert (counts["open"], counts["portals"]) == (1, 1), counts
    assert page.locator("body > ul.row-actions-portal a").first.inner_text() == "Resume"
    _shot(page, "2-menu-b-replaces-a")

    # The menu stays attached to its button after the polls (right edge aligned,
    # right below), not in the corner or at a stale position.
    menu = page.locator("body > ul.row-actions-portal").bounding_box()
    button = page.locator("#worker-card-w2 summary").bounding_box()
    assert abs((menu["x"] + menu["width"]) - (button["x"] + button["width"])) <= 1
    assert 0 <= menu["y"] - (button["y"] + button["height"]) <= 8


def test_escape_closes_and_returns_focus_to_kebab(page: Any) -> None:
    _open(page, "w1")
    _polls(page, 2)
    page.keyboard.press("Escape")
    _assert_closed(page)
    assert page.evaluate(FOCUS_JS) == "summary:worker-card-w1"
    _polls(page, 2)
    _assert_closed(page)


def test_outside_click_closes_without_stealing_focus(page: Any) -> None:
    _open(page, "w2")
    _polls(page, 2)
    page.locator("#outside-btn").click()
    _assert_closed(page)
    assert page.evaluate(FOCUS_JS) == "outside-btn"
    _polls(page, 1)
    _assert_closed(page)


def test_choosing_an_action_closes_menu_and_action_still_works(page: Any) -> None:
    _open(page, "w1")
    _polls(page, 1)
    page.locator("body > ul.row-actions-portal a", has_text="Pause").click()
    page.wait_for_selector("#confirm-dialog[open]")
    _assert_closed(page)
    _shot(page, "3-confirm-dialog-no-menu-behind")

    # Dismiss: focus returns to the kebab button, not to <body>.
    page.locator("[data-confirm-cancel]").click()
    assert page.evaluate(FOCUS_JS) == "summary:worker-card-w1"

    _open(page, "w1")
    page.locator("body > ul.row-actions-portal a", has_text="Pause").click()
    page.wait_for_selector("#confirm-dialog[open]")
    with page.expect_response("**/admin/workers/w1/pause"):
        page.locator("#confirm-dialog-ok").click()
    page.wait_for_selector("#worker-card-w1.worker-status-paused")
    assert page.evaluate(FOCUS_JS) == "summary:worker-card-w1"
    _assert_closed(page)


def test_force_delete_removes_card_and_leaves_no_orphan(page: Any) -> None:
    _open(page, "w3")
    _polls(page, 1)
    page.locator("body > ul.row-actions-portal a", has_text="Delete worker").click()
    page.wait_for_selector("#confirm-dialog[open]")
    with page.expect_response("**/admin/workers/w3"):
        page.locator("#confirm-dialog-ok").click()
    page.wait_for_selector("#worker-card-w3", state="detached")
    _polls(page, 1)
    _assert_closed(page)
    assert _counts(page)["cards"] == 2
    assert page.evaluate(FOCUS_ON_BODY_JS)


def test_confirmed_force_delete_leaves_focus_on_body(page: Any) -> None:
    # Product decision: when the deleted card disappears, focus stays on
    # document.body; it does not jump to a neighbouring card.
    _open(page, "w2")
    _polls(page, 1)
    page.locator("body > ul.row-actions-portal a", has_text="Delete (force)").click()
    page.wait_for_selector("#confirm-dialog[open]")
    _assert_closed(page)
    with page.expect_response("**/admin/workers/w2?force=true"):
        page.locator("#confirm-dialog-ok").click()
    page.wait_for_selector("#worker-card-w2", state="detached")
    assert page.evaluate(FOCUS_ON_BODY_JS)
    # The following polls morph the deck without the card: focus stays on
    # <body> and no orphan portal is left.
    _polls(page, 2)
    assert page.evaluate(FOCUS_ON_BODY_JS)
    _assert_closed(page)
    assert _counts(page)["cards"] == 2


def test_card_morphed_away_by_poll_leaves_focus_on_body(page: Any, base_url: str) -> None:
    # The poll (morph) removes the card, not the Delete swap itself: the focused
    # kebab button disappears and focus must end up on <body>.
    _open(page, "w2")
    page.keyboard.press("Escape")
    assert page.evaluate(FOCUS_JS) == "summary:worker-card-w2"
    page.request.delete(f"{base_url}/admin/workers/w2?force=true")
    page.wait_for_selector("#worker-card-w2", state="detached")
    _polls(page, 1)
    assert page.evaluate(FOCUS_ON_BODY_JS)
    _assert_closed(page)


def test_cancelled_force_delete_returns_focus_to_kebab(page: Any) -> None:
    _open(page, "w2")
    _polls(page, 1)
    page.locator("body > ul.row-actions-portal a", has_text="Delete (force)").click()
    page.wait_for_selector("#confirm-dialog[open]")
    page.locator("[data-confirm-cancel]").click()
    page.wait_for_selector("#confirm-dialog", state="hidden")
    assert page.evaluate(FOCUS_JS) == "summary:worker-card-w2"
    _polls(page, 2)
    assert page.evaluate(FOCUS_JS) == "summary:worker-card-w2"
    _assert_closed(page)
    assert _counts(page)["cards"] == 3


def test_menu_is_usable_with_keyboard_only(page: Any) -> None:
    page.locator("#worker-card-w2 summary").focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("body > ul.row-actions-portal")
    _polls(page, 1)

    page.keyboard.press("ArrowDown")
    assert page.evaluate(FOCUS_JS) == "Resume"
    page.keyboard.press("ArrowDown")
    assert page.evaluate(FOCUS_JS) == "Delete (force)"
    _shot(page, "4-keyboard-focus-danger-item")
    # Focus inside the menu survives the poll (the <ul> moves away and back).
    _polls(page, 2)
    assert page.evaluate(FOCUS_JS) == "Delete (force)"
    page.keyboard.press("ArrowDown")
    assert page.evaluate(FOCUS_JS) == "Resume"
    page.keyboard.press("End")
    assert page.evaluate(FOCUS_JS) == "Delete (force)"
    # Shift+Tab from the last item goes back to the previous one; it does not close the menu.
    page.keyboard.press("Shift+Tab")
    assert page.evaluate(FOCUS_JS) == "Resume"
    counts = _counts(page)
    assert (counts["open"], counts["portals"]) == (1, 1), counts
    page.keyboard.press("Tab")
    assert page.evaluate(FOCUS_JS) == "Delete (force)"
    page.keyboard.press("Home")
    assert page.evaluate(FOCUS_JS) == "Resume"
    page.keyboard.press("Shift+Tab")
    assert page.evaluate(FOCUS_JS) == "summary:worker-card-w2"
    page.keyboard.press("Tab")
    assert page.evaluate(FOCUS_JS) == "Resume"
    _shot(page, "5-keyboard-focus-first-item")

    page.keyboard.press("Enter")
    page.wait_for_selector("#confirm-dialog[open]")
    _assert_closed(page)
    page.keyboard.press("Escape")
    page.wait_for_selector("#confirm-dialog", state="hidden")
    assert page.evaluate(FOCUS_JS) == "summary:worker-card-w2"

    # Tab from the last item closes the menu and continues its natural course.
    page.keyboard.press("Enter")
    page.wait_for_selector("body > ul.row-actions-portal")
    page.keyboard.press("End")
    page.keyboard.press("Tab")
    _assert_closed(page)
    assert page.evaluate(FOCUS_JS) != "body"


def test_menu_without_js_is_a_native_details(browser: Any, base_url: str) -> None:
    context = browser.new_context(java_script_enabled=False)
    pg = context.new_page()
    # Without JS there is no htmx to load the deck: the fragment is served directly.
    pg.goto(f"{base_url}/admin/workers")
    summary = pg.locator("#worker-card-w1 summary")
    link = pg.locator("#worker-card-w1 details a", has_text="Pause")
    assert not link.is_visible()
    summary.click()
    assert link.is_visible()
    summary.click()
    assert not link.is_visible()
    context.close()
