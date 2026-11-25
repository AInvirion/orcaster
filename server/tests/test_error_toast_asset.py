"""Error-reporting surface of the dashboard and cache-busting of the assets.

There is no JS test tooling in the repo, so what can be pinned
server-side is that the listener and the styles exist, and that the version
query string in `base.html` has been bumped: without that bump, Cloudflare and the
browser serve the cached `app.js` and the change is not visible in production.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
APP_JS = SERVER_DIR / "orcaster_server" / "static" / "app.js"
APP_CSS = SERVER_DIR / "orcaster_server" / "static" / "app.css"
BASE_HTML = SERVER_DIR / "orcaster_server" / "templates" / "base.html"


def test_app_js_reports_htmx_response_errors() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert "htmx:responseError" in source
    assert "function initErrorToast" in source
    assert "function showErrorToast" in source
    # The call must be in the ready() block, otherwise the listener
    # is never attached.
    ready_block = source.split("ready(function () {", 1)[1].split("});", 1)[0]
    assert "initErrorToast();" in ready_block


def test_app_css_defines_toast_surface() -> None:
    source = APP_CSS.read_text(encoding="utf-8")
    for selector in (".toast-stack", ".toast-error", ".toast-close"):
        assert selector in source, selector


def test_base_html_cache_bust_is_shared_and_bumped() -> None:
    source = BASE_HTML.read_text(encoding="utf-8")
    tokens = re.findall(r"/static/app\.(?:css|js)\?v=([\w.-]+)", source)
    assert len(tokens) == 2, tokens
    # CSS and JS are bumped together: if they diverge, one of the two stays cached.
    assert tokens[0] == tokens[1]
    # These are values already shipped in earlier releases. Guards against the
    # recurring mistake of touching app.js/app.css and forgetting the bump.
    assert tokens[0] not in ("ts-local-33", "ts-local-34", "ts-local-35")


def _function_body(source: str, name: str) -> str:
    """Extract the `{ ... }` body of `function <name>(...)` by counting braces.

    Counts brace depth instead of cutting at the first `}` so as not to
    truncate in the middle of the nested handlers (the `close` listener and the
    `setTimeout` callback live inside `showErrorToast`).
    """
    marker = f"function {name}("
    start = source.index(marker)
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


def test_show_error_toast_caps_stack_size() -> None:
    # Without this cap, a sustained 502 (restart with MIGRATE_ON_STARTUP) against
    # the pollers without a gate (workers every 3s, tasks every 5s) fills the corner
    # with unlimited toasts.
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "showErrorToast")
    assert re.search(r"stack\.children\.length\s*>=\s*\w+", body), body
    assert "firstElementChild.remove()" in body


def test_show_error_toast_dedupes_repeated_message() -> None:
    # The early-exit guard is what prevents stacking the same repeated
    # message; without it, the cap would only limit HOW MANY duplicates are
    # stacked, not whether they are stacked.
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "showErrorToast")
    assert "lastElementChild" in body
    assert ".toast-text" in body
    assert re.search(r"if\s*\(lastText[^)]*\)\s*return;", body), body


def test_show_error_toast_cancels_timer_on_manual_dismiss() -> None:
    # If the operator closes the toast by hand, the auto-dismiss setTimeout
    # must be cancelled; without clearTimeout, the old timer stays alive.
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "showErrorToast")
    assert "const timer = setTimeout(" in body
    assert "clearTimeout(timer)" in body


def test_toast_stack_is_created_by_init_not_by_show() -> None:
    # Live region gotcha: the #toast-stack node must exist in the
    # tree BEFORE the first mutation or the first announcement is lost to
    # a screen reader. That requires initErrorToast() (which runs at
    # startup) to create it, not showErrorToast() (which only runs at the first
    # error).
    source = APP_JS.read_text(encoding="utf-8")
    init_body = _function_body(source, "initErrorToast")
    show_body = _function_body(source, "showErrorToast")
    assert "stack.id = 'toast-stack'" in init_body
    assert "document.body.appendChild(stack)" in init_body
    assert "stack.id = 'toast-stack'" not in show_body
    assert "document.body.appendChild(stack)" not in show_body


def test_confirm_dialog_percent_encodes_hx_prompt() -> None:
    # setRequestHeader throws (SyntaxError) with characters outside Latin-1:
    # a reason with an emoji or typographic quotes would kill the request before
    # it is sent, with no toast (it is htmx:sendError, not htmx:responseError). BOTH
    # header writers (the htmx:configRequest listener for declarative triggers
    # and the data-confirm-url mode fetch) must percent-encode; the server
    # decodes in `reason_from_request`.
    source = APP_JS.read_text(encoding="utf-8")
    encoded = re.findall(r"\['HX-Prompt'\]\s*=\s*encodeURIComponent\(", source)
    assert len(encoded) == 2, encoded
    # No raw assignment remains. The `\s*` INSIDE the lookahead is
    # necessary: with `=\s*(?!encode...)` the backtracking of `\s*` evaluates the
    # lookahead before the space and makes it always pass.
    assert not re.search(r"\['HX-Prompt'\]\s*=(?!\s*encodeURIComponent\()", source)
