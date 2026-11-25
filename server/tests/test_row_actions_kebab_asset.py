"""Kebab menu of the worker cards: static surface.

The real behaviour (a single open menu, closing by Escape / outside click /
action, focus returning to the button, surviving the deck polls) is covered by
`test_row_actions_kebab_browser.py`, which needs Playwright and is skipped
without it. What is pinned here always runs: the pieces of `app.js` whose
absence reintroduces the defect, the markup they depend on and the cache-bust.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
APP_JS = SERVER_DIR / "orcaster_server" / "static" / "app.js"
APP_CSS = SERVER_DIR / "orcaster_server" / "static" / "app.css"
BASE_HTML = SERVER_DIR / "orcaster_server" / "templates" / "base.html"
WORKERS_DECK = SERVER_DIR / "orcaster_server" / "templates" / "_workers_deck.html"


def _function_body(source: str, name: str) -> str:
    """Extract the `{ ... }` body of `function <name>(...)` by counting braces."""
    start = source.index(f"function {name}(")
    brace_start = source.index("{", start)
    depth = 0
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start : i + 1]
    raise AssertionError(f"closing brace of function {name} not found")


def _row_actions() -> str:
    return _function_body(APP_JS.read_text(encoding="utf-8"), "initRowActions")


def test_init_row_actions_is_wired() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert "initRowActions();" in _function_body(source, "initDashboard")


def test_state_is_declared_before_any_listener() -> None:
    # TDZ trap: a `let`/`const` used by a listener attached before its
    # declaration blows up with a ReferenceError on the first event.
    body = _row_actions()
    first_listener = body.index("addEventListener(")
    for decl in ("let active = null;", "let pendingSwap = null;"):
        assert decl in body, decl
        assert body.index(decl) < first_listener, decl


def test_swap_mark_is_consumed_once() -> None:
    # htmx emits one htmx:afterSwap per settled element. The mark left by
    # beforeSwap is consumed by the first; the second must not close anything.
    body = _row_actions()
    after_swap = _function_body(body, "afterSwap")
    assert "pendingSwap = null" in after_swap
    assert "_wasOpen" not in body
    for event in (
        "htmx:beforeSwap",
        "htmx:oobBeforeSwap",
        "htmx:afterSwap",
        "htmx:oobAfterSwap",
        "htmx:swapError",
    ):
        assert f"'{event}'" in body, event


def test_orphan_portals_are_swept_on_close_and_after_swap() -> None:
    body = _row_actions()
    assert "body > ul.row-actions-portal" in _function_body(body, "sweepOrphans")
    assert "sweepOrphans();" in _function_body(body, "closePortal")
    assert "sweepOrphans();" in _function_body(body, "afterSwap")


def test_listeners_are_delegated_without_timers() -> None:
    # The setTimeout that attached the per-menu click listener could run after
    # the menu closed and leave a listener without an owner.
    body = _row_actions()
    assert "setTimeout(" not in body
    assert body.count("document.addEventListener('click'") == 2
    assert body.count("document.addEventListener('keydown'") == 1


def test_close_paths_return_focus_to_the_kebab_button() -> None:
    body = _row_actions()
    assert "focusSummary(cur.det)" in _function_body(body, "closeMenu")
    # Escape and the chosen action return focus; an outside click does not.
    assert re.search(
        r"ev\.key === 'Escape'\) \{\s*ev\.preventDefault\(\);\s*closeMenu\(true\)", body
    )
    assert "cur.ul.contains(item)) closeMenu(true)" in body
    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Tab"):
        assert f"'{key}'" in body, key
    # Tab only leaves the menu from the last item WITHOUT Shift: Shift+Tab there
    # goes back to the previous item instead of closing.
    assert "idx === last && !ev.shiftKey" in body


def test_removed_card_leaves_focus_on_body() -> None:
    # Product decision: deleted card => focus on document.body, without jumping
    # to a neighbour. Tracking starts in beforeSwap, before the early return for
    # "no open menu", and is resolved in afterSwap.
    body = _row_actions()
    before_swap = _function_body(body, "beforeSwap")
    assert before_swap.index("rememberFocusedKebab();") < before_swap.index("if (!active) return;")
    assert "releaseFocusOfRemovedKebab();" in _function_body(body, "afterSwap")
    release = _function_body(body, "releaseFocusOfRemovedKebab")
    assert "el.blur()" in release
    assert ".focus(" not in release


def test_workers_deck_markup_matches_what_the_script_expects() -> None:
    source = WORKERS_DECK.read_text(encoding="utf-8")
    # Native <details>: without JS it opens and closes with its <summary>.
    # data-keep-open makes the poll morph leave `open` alone; the <li> id makes
    # idiomorph keep the node (and its focus) across polls.
    assert '<details class="dropdown row-actions worker-card-kebab" data-keep-open>' in source
    assert '<summary aria-label="Actions for worker' in source
    assert 'id="worker-card-{{ w.id }}"' in source


def test_menu_items_have_a_keyboard_focus_ring() -> None:
    source = APP_CSS.read_text(encoding="utf-8")
    assert "ul.row-actions-portal > li > a:focus-visible" in source
    assert "details.row-actions > ul > li > a:focus-visible" in source


def test_cache_bust_bumped_for_kebab_fix() -> None:
    source = BASE_HTML.read_text(encoding="utf-8")
    tokens = re.findall(r"/static/app\.(?:css|js)\?v=([\w.-]+)", source)
    assert len(tokens) == 2, tokens
    assert tokens[0] == tokens[1]
    # ts-local-55 is the value shipped BEFORE this fix.
    assert tokens[0] != "ts-local-55"
