"""Extraction of the optional reason (`reason`) of an admin action.

Single source of the body > `HX-Prompt` header precedence shared by
`cancel_task` and `bulk_cancel_task` (`admin_tasks.py`) and `pause_worker` /
`resume_worker` (`admin_workers.py`). It used to be triplicated inline in the
handlers, with normalizations that had already diverged: the header was
stripped and the body was not, so `{"reason": "   "}` through the JSON API
persisted the spaces where the same reason through the header persisted NULL.

Header contract: the dashboard's custom dialog sends `HX-Prompt`
percent-encoded in UTF-8 (`encodeURIComponent` in app.js), because
`setRequestHeader` throws on characters outside Latin-1 and a reason with an
emoji or typographic quotes would kill the request before it left. Here it is
decoded with `unquote`, which is the identity for plain text without `%`: an
API client that sends the header unencoded is not affected, unless its reason
contains literal `%XX` sequences. The JSON body is neither encoded nor
decoded: it arrives as native UTF-8.
"""

from __future__ import annotations

from urllib.parse import unquote

from fastapi import Request


def reason_from_request(request: Request, body_reason: str | None) -> str | None:
    """Normalized reason: the body wins; blank = absent → header.

    Both sources get the same normalization (strip, empty → None): "no
    reason" has a single representation (NULL) whatever the transport.
    """
    reason = body_reason.strip() if body_reason else None
    if not reason:
        hx_prompt = request.headers.get("HX-Prompt")
        if hx_prompt:
            reason = unquote(hx_prompt).strip()
    return reason or None
