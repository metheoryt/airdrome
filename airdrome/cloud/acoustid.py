"""AcoustID lookup: resolve stored chromaprint fingerprints to MusicBrainz recording ids."""

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.console import detail
from airdrome.enums import BindMethod
from airdrome.library.fingerprint import FingerprintError, run_fpcalc
from airdrome.models import TrackFile


LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
STORE_THRESHOLD = 0.9  # minimum top-result score to trust a recording id
MIN_INTERVAL = 1 / 3  # AcoustID allows at most 3 requests per second
TIMEOUT = 10  # seconds per request
_INVALID_KEY_CODE = 4  # AcoustID error code for "invalid API key"


class AcoustIDError(RuntimeError):
    """One lookup failed (network, HTTP or an API error); the run carries on with the next file."""


class AcoustIDKeyError(AcoustIDError):
    """The service rejected the client key; every further lookup would fail the same way."""


@dataclass
class IdentifyStats:
    """Counters for one `identify` run."""

    seen: int = 0
    stored: int = 0
    ambiguous: int = 0  # looked up, but no single confident recording id
    missing: int = 0  # file not on disk
    fp_failed: int = 0  # fpcalc could not fingerprint the file
    failed: int = 0  # network / HTTP / API error on this file


def compressed_fingerprint(path: Path) -> tuple[str, int]:
    """Fingerprint the first 120 s of `path` in AcoustID's compressed form; return it with the duration."""
    # `identify` only selects files whose raw fingerprint is already stored (it passed `compute`'s
    # item check), so for an exit-3 run a non-empty compressed string is enough.
    data = run_fpcalc(path, raw=False)
    try:
        return str(data["fingerprint"]), int(float(data["duration"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise FingerprintError(f"unparseable fpcalc output for {path}") from exc


def lookup(client_key: SecretStr, fingerprint: str, duration: int) -> list[tuple[float, str, list[str]]]:
    """POST one fingerprint to AcoustID; return `(score, acoustid_id, recording_ids)` per result.

    Every failure other than a rejected key becomes `AcoustIDError(...) from None`: messages and
    the (suppressed) cause never carry the request, which holds the key.
    """
    secret = client_key.get_secret_value()
    body = urllib.parse.urlencode(
        {"client": secret, "meta": "recordingids", "duration": duration, "fingerprint": fingerprint}
    ).encode()
    try:
        try:
            with urllib.request.urlopen(LOOKUP_URL, data=body, timeout=TIMEOUT) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            # AcoustID reports API errors (bad key, bad fingerprint) as HTTP 400 with a JSON body.
            try:
                payload = json.load(exc)
            except ValueError, OSError, http.client.HTTPException:
                raise AcoustIDError(f"AcoustID answered HTTP {exc.code}") from None
        return _parse(payload, secret)
    except AcoustIDError:
        raise
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, http.client.HTTPException) as exc:
        raise AcoustIDError(f"AcoustID request failed: {type(exc).__name__}") from None


def _parse(payload: object, secret: str) -> list[tuple[float, str, list[str]]]:
    """Turn a decoded AcoustID response into result triples; anything malformed is an `AcoustIDError`."""
    try:
        if not isinstance(payload, dict):
            raise TypeError("not an object")
        if payload.get("status") != "ok":
            err = payload.get("error") or {}
            if err.get("code") == _INVALID_KEY_CODE:
                raise AcoustIDKeyError("AcoustID rejected the client key (invalid API key)")
            message = str(err.get("message", "unknown")).replace(secret, "***")
            raise AcoustIDError(f"AcoustID error: {message}")
        return [
            (
                float(r.get("score", 0.0)),
                str(r.get("id", "")),
                [rec["id"] for rec in r.get("recordings", [])],
            )
            for r in payload.get("results", [])
        ]
    except AcoustIDError:
        raise
    except KeyError, ValueError, TypeError, AttributeError:
        raise AcoustIDError("AcoustID returned a malformed response") from None


def identify(
    s: Session,
    client_key: SecretStr,
    *,
    limit: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> IdentifyStats:
    """Fill `recording_mbid` for fingerprinted files that lack one, when AcoustID is confident.

    Stores only when the top result scores at least `STORE_THRESHOLD` and names exactly one
    recording; an existing `recording_mbid` is never touched. Requests start at least
    `MIN_INTERVAL` apart (`clock`/`sleep` are injectable for tests). Writes only the DB.
    """
    stmt = (
        select(TrackFile)
        .where(TrackFile.fingerprint.is_not(None), TrackFile.recording_mbid.is_(None))
        .order_by(TrackFile.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    stats = IdentifyStats()
    last_start: float | None = None
    for tf in s.scalars(stmt).all():
        stats.seen += 1
        path = tf.absolute_path
        if not path.exists():
            stats.missing += 1
            continue
        try:
            fp, duration = compressed_fingerprint(path)
        except FingerprintError:
            stats.fp_failed += 1
            continue
        if last_start is not None and (wait := MIN_INTERVAL - (clock() - last_start)) > 0:
            sleep(wait)
        last_start = clock()
        try:
            results = lookup(client_key, fp, duration)
        except AcoustIDKeyError:
            raise
        except AcoustIDError as exc:
            stats.failed += 1
            detail(f"lookup failed for {path}: {exc}")
            continue
        top = max(results, key=lambda r: r[0], default=None)
        if top is None or top[0] < STORE_THRESHOLD or len(top[2]) != 1:
            stats.ambiguous += 1
            detail(f"ambiguous: {path}")
            continue
        tf.recording_mbid = top[2][0]
        tf.acoustid_id = top[1] or None
        tf.mbid_source = BindMethod.ACOUSTID
        s.flush()
        stats.stored += 1
    return stats
