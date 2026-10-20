"""Keyset paging over a list sorted in Python: the cursor names the last row served, not a position.

An offset cursor shifts under the reader: a `skip` verdict on page 1 removes a row, and offset 50 then
starts one row late, so a row is never shown. A keyset cursor carries the sort key of the last row served
and the next page starts strictly after it, so rows that leave or join the list between calls move nothing
else. The cursor also names its list, so one list's cursor is refused by another.
"""

import base64
import binascii
import json
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass


MAX_LIMIT = 200

type Key = tuple[int | str, ...]


@dataclass(frozen=True)
class Window[T]:
    """One served slice of a sorted list: its items, the list's total, and the next page's cursor."""

    items: list[T]
    total: int
    next_cursor: str | None


def encode_cursor(name: str, key: Key) -> str:
    """Opaque cursor for "after `key` in list `name`"."""
    raw = json.dumps([name, list(key)], ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(name: str, cursor: str | None) -> Key | None:
    """The key a cursor of list `name` points after (None: first page); anything else raises `ValueError`."""
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        owner, key = json.loads(raw)
    except TypeError, ValueError, binascii.Error:
        raise ValueError("bad cursor") from None
    if owner != name or not isinstance(key, list) or not all(_scalar(k) for k in key):
        raise ValueError("bad cursor")
    # JSON hands lists back; the key compares as a tuple, like the keys it is bisected against.
    return tuple(key)


def _scalar(value: object) -> bool:
    """Whether `value` is a key element: an int (not a bool) or a str."""
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def window[T](
    name: str, items: list[T], key: Callable[[T], Key], cursor: str | None, limit: int
) -> Window[T]:
    """The page of `items` (sorted by unique `key`) after `cursor`; `limit` clamped to 1..200."""
    limit = max(1, min(limit, MAX_LIMIT))
    after = decode_cursor(name, cursor)
    try:
        start = 0 if after is None else bisect_right(items, after, key=key)
    except TypeError:  # a forged cursor whose element types differ from this list's keys
        raise ValueError("bad cursor") from None
    served = items[start : start + limit]
    more = start + limit < len(items)
    return Window(
        items=served, total=len(items), next_cursor=encode_cursor(name, key(served[-1])) if more else None
    )
