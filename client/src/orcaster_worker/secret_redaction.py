"""Masking of the secrets delivered by the server.

Everything the worker sends back (streamed stdout/stderr, synthetic error
output) and what it stores in its local idempotency storage has the values
delivered by the server of 8 or more characters replaced by ``***``. Shorter
values are not masked: with so few characters the replacement would overwrite
legitimate text and protects nothing real.

It is best-effort: an encoded or transformed value (base64, trimmed, split by
the task itself) is not recognized. A value split between two stream chunks is
recognized: ``split`` holds back the tail that could be the start of a value
until the next chunk is seen. The tail is decided on the raw text, before
masking: if one value is a prefix of another, masking first would cover the
short one and leave the rest of the long one in view.

A single-line compound value (``tok1,tok2``, or space-separated) also masks each
part of 8 or more characters, because the task usually uses the parts on their
own. A structured value (multiline, JSON or PEM) is not split by commas or
spaces: its pieces (``-----BEGIN``, ``"client_email":``) would match unrelated
text. It is masked whole and, if multiline, so is each line of 8 or more
characters that is neither PEM armor nor a pure JSON structural token.

If the value is valid JSON, the task usually parses it and uses its fields
individually (``private_key``, ``private_key_id`` of a service account). So each
string leaf of 8 or more characters is also masked (not the keys), each data
line of a multiline leaf (the body of a PEM) and the escaped form of the leaf
(``json.dumps``, with literal ``\\n``), with and without non-ASCII escaping.

The worker local logs are masked with ``SecretLogFilter``, installed on the
root logger handlers (``install_log_redaction``). The filter uses the active
task redactor (``set_active_redactor``): the worker runs a single task at a
time, so a process-global registry is enough and it also covers logs emitted
from threads (``asyncio.to_thread``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterable, Iterator
from typing import Any

MASK = "***"
MIN_MASK_LEN = 8

# Separators of a compound value: comma and any whitespace.
_PART_SEPARATORS = re.compile(r"[,\s]+")
# Markers of a structured value, which is not split on separators.
_STRUCTURED_MARKERS = ("\n", "{", "-----BEGIN")
# PEM armor line and a line made only of JSON structural punctuation.
_PEM_ARMOR_LINE = re.compile(r"-----(BEGIN|END) [A-Z0-9 ]+-----")
_JSON_STRUCTURAL_LINE = re.compile(r"[\s{}\[\],:]*")


def _data_lines(value: str) -> Iterator[str]:
    """Lines of ``value`` without PEM armor or pure JSON structural tokens."""
    for line in value.splitlines():
        line = line.strip()
        if _PEM_ARMOR_LINE.fullmatch(line) or _JSON_STRUCTURAL_LINE.fullmatch(line):
            continue
        yield line


def _json_string_leaves(obj: Any) -> Iterator[str]:
    """String leaves of an already parsed JSON document; keys do not count."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _json_string_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_string_leaves(v)


def _json_leaf_patterns(value: str) -> set[str]:
    """Patterns of the string leaves if ``value`` is JSON; empty if it is not."""
    # Absurd nesting (``[[[[...``) exhausts the recursion: it is treated as
    # non-JSON and the usual patterns remain.
    try:
        leaves = list(_json_string_leaves(json.loads(value)))
    except (ValueError, RecursionError):
        return set()
    patterns: set[str] = set()
    for leaf in leaves:
        patterns.add(leaf)
        if "\n" in leaf:
            patterns.update(_data_lines(leaf))
        # Escaped form without the quotes: the task re-serializes the leaf.
        patterns.add(json.dumps(leaf)[1:-1])
        patterns.add(json.dumps(leaf, ensure_ascii=False)[1:-1])
    return patterns


def _mask_patterns(value: str) -> set[str]:
    """Patterns that mask ``value``: the whole value plus its parts."""
    # A single-line JSON without braces (an array) goes through the parts branch
    # and still adds its leaves.
    leaves = _json_leaf_patterns(value)
    if not any(marker in value for marker in _STRUCTURED_MARKERS):
        return {value, *_PART_SEPARATORS.split(value), *leaves}
    return {value, *_data_lines(value), *leaves}


class SecretRedactor:
    """Replace the given values with ``MASK``, longest first."""

    def __init__(self, values: Iterable[str]) -> None:
        unique: set[str] = set()
        for v in values:
            if not isinstance(v, str):
                continue
            unique.update(_mask_patterns(v))
        unique = {v for v in unique if len(v) >= MIN_MASK_LEN}
        # Longest first: `re` alternation takes the first alternative that
        # matches at each position, so a value that contains another is masked
        # whole and does not leave its remainder in view.
        self._values: tuple[str, ...] = tuple(sorted(unique, key=len, reverse=True))
        self._pattern = (
            re.compile("|".join(re.escape(v) for v in self._values)) if self._values else None
        )

    @property
    def active(self) -> bool:
        return self._pattern is not None

    def redact(self, text: str) -> str:
        if self._pattern is None or not text:
            return text
        return self._pattern.sub(MASK, text)

    def redact_obj(self, obj: Any) -> Any:
        """Recursively mask the strings of dicts and lists (JSON-like)."""
        if self._pattern is None:
            return obj
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj

    def split(self, text: str) -> tuple[str, str]:
        """Split off the raw tail of ``text`` that could be the start of a value
        and mask the rest: returns ``(emittable, held)``.

        The tail is computed on the raw text: if one value is a prefix of another
        and the cut falls inside the long one, masking first would cover the short
        one and leave the rest of the long one in view. The cut never falls
        inside a value that already matches whole: it moves to the start of that
        value, which is held back entirely.

        The caller prepends ``held`` to the next chunk of the same stream and
        passes it through ``redact`` on close."""
        if self._pattern is None or not text:
            return text, ""
        hold = self._held_suffix_len(text)
        if hold == 0:
            return self.redact(text), ""
        cut = len(text) - hold
        for match in self._pattern.finditer(text):
            if match.start() < cut < match.end():
                cut = match.start()
                break
            if match.start() >= cut:
                break
        return self.redact(text[:cut]), text[cut:]

    def _held_suffix_len(self, text: str) -> int:
        """Length of the longest suffix of ``text`` that is a proper prefix of some
        value. It only tries the positions where the value's first character
        appears within the final window, so the cost does not grow with the
        stream."""
        best = 0
        n = len(text)
        for v in self._values:
            start = max(0, n - (len(v) - 1))
            i = text.find(v[0], start)
            while i != -1:
                if v.startswith(text[i:]):
                    best = max(best, n - i)
                    break  # the first matching position is the longest suffix
                i = text.find(v[0], i + 1)
        return best


# --- Local logs ---------------------------------------------------------------

_active: SecretRedactor | None = None


def set_active_redactor(redactor: SecretRedactor | None) -> None:
    """Set (or clear with None) the redactor of the running task for the logs."""
    global _active
    _active = redactor if redactor is not None and redactor.active else None


def current_redactor() -> SecretRedactor | None:
    return _active


@contextlib.contextmanager
def active_redactor(redactor: SecretRedactor | None) -> Iterator[None]:
    """Activate ``redactor`` during the block and restore the previous one on exit."""
    previous = _active
    set_active_redactor(redactor)
    try:
        yield
    finally:
        set_active_redactor(previous)


class SecretLogFilter(logging.Filter):
    """Mask the message, traceback and stack of each record with the active
    redactor. Never drops records.

    Pre-formats the traceback into ``exc_text``: ``logging.Formatter`` uses
    ``exc_text`` if it is already set, so the handler prints the masked version
    and does not format the original exception again."""

    def filter(self, record: logging.LogRecord) -> bool:
        redactor = _active
        if redactor is None:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True  # broken message: logging itself reports it
        masked = redactor.redact(message)
        if masked != message:
            record.msg = masked
            record.args = None
        if record.exc_info and record.exc_info[1] is not None and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redactor.redact(record.stack_info)
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Add ``SecretLogFilter`` to each handler of ``logger`` (root by default).
    Idempotent. It goes on the handlers and not on the logger because a
    logger's filters do not see the records its children propagate."""
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, SecretLogFilter) for f in handler.filters):
            handler.addFilter(SecretLogFilter())
