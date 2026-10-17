"""Chromaprint fingerprints: an `fpcalc` wrapper, a similarity score and an inverted index."""

import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


# "Same recording" threshold, calibrated on the live library 2026-10-17 (identity-ladder.md,
# *Calibration*): vocal vs instrumental versions matched at up to 0.884, while no same-recording
# pair scored between 0.685 and 0.890. Above ~0.9 versions sharing their first two minutes
# (clean/explicit, demos, some instrumentals) overlap true matches; only the id veto and the
# duration gate separate those.
FP_MATCH: float = 0.89
_MASK = 0xFFFFFFFF
_MIN_OVERLAP = 20
# fpcalc decodes 120 s of audio in well under a second; a hang (a stuck network mount, a pathological
# file) must cost one counted failure, not the whole run.
FPCALC_TIMEOUT = 60.0
# fpcalc 1.6 exits 3 on an audio decode error *after* printing the fingerprint of what it did decode
# (31 files of the live library); such output is kept when it is usable.
_FPCALC_DECODE_ERROR = 3


class FingerprintError(RuntimeError):
    """Raised when `fpcalc` is missing, fails, or prints something unparseable."""


@dataclass(frozen=True)
class Fingerprint:
    """A raw chromaprint (signed int32 items) and the audio duration in seconds."""

    raw: list[int]
    duration: float


def _usable(data: dict, *, raw: bool) -> bool:
    """Whether a decode-error run's fingerprint is worth keeping: enough raw items, or a non-empty string."""
    fp = data.get("fingerprint")
    if raw:
        return isinstance(fp, list) and len(fp) >= _MIN_OVERLAP
    return isinstance(fp, str) and bool(fp)


def run_fpcalc(path: Path, *, raw: bool) -> dict:
    """Run `fpcalc -json` over the first 120 s of `path` (`-raw` items or the compressed string).

    Every failure is a `FingerprintError`: a missing binary, a timeout, a non-zero exit or output that
    is not a JSON object. Exit 3 (a decode error) is accepted when the printed fingerprint is usable.
    Output is decoded with `errors="replace"`, so a stray non-UTF-8 byte cannot raise mid-run.
    """
    args = ["fpcalc", *(["-raw"] if raw else []), "-json", "-length", "120", str(path)]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=FPCALC_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError(f"fpcalc timed out on {path}") from exc
    except OSError as exc:
        raise FingerprintError(f"cannot run fpcalc: {exc}") from exc
    if proc.returncode not in (0, _FPCALC_DECODE_ERROR):
        raise FingerprintError(f"fpcalc failed on {path}: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise FingerprintError(f"unparseable fpcalc output for {path}") from exc
    if not isinstance(data, dict):
        raise FingerprintError(f"unparseable fpcalc output for {path}")
    if proc.returncode != 0 and not _usable(data, raw=raw):
        raise FingerprintError(f"fpcalc failed on {path}: {proc.stderr.strip()}")
    return data


def compute(path: Path) -> Fingerprint:
    """Fingerprint the first 120 s of `path` with `fpcalc`; items are stored as signed int32."""
    data = run_fpcalc(path, raw=True)
    try:
        # fpcalc prints unsigned 32-bit values; Postgres ARRAY(Integer) is signed, so convert here.
        raw = [v - 2**32 if v >= 2**31 else v for v in data["fingerprint"]]
        return Fingerprint(raw=raw, duration=float(data["duration"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise FingerprintError(f"unparseable fpcalc output for {path}") from exc


def _distance(a: list[int], b: list[int]) -> float:
    """Mean-bit-agreement of two equally long slices, in [0, 1]."""
    # Mask first: bit_count() of a negative Python int counts the magnitude, not the 32-bit pattern.
    diff = sum(((x ^ y) & _MASK).bit_count() for x, y in zip(a, b, strict=True))
    return 1 - diff / (32 * len(a))


def similarity(a: list[int], b: list[int], max_offset: int = 80) -> float:
    """Best bit-agreement over relative offsets in [-max_offset, max_offset]; 0.0 if overlap < 20."""
    best = 0.0
    for off in range(-max_offset, max_offset + 1):
        # off > 0 shifts b left relative to a: compare a[off:] with b[:].
        xa, xb = (a[off:], b) if off >= 0 else (a, b[-off:])
        n = min(len(xa), len(xb))
        if n < _MIN_OVERLAP:
            continue
        best = max(best, _distance(xa[:n], xb[:n]))
    return best


class FingerprintIndex:
    """Inverted index on coarse fingerprint items, so lookups avoid pairwise comparison."""

    def __init__(self) -> None:
        """Start empty."""
        self._buckets: dict[int, set[int]] = defaultdict(set)
        self._fps: dict[int, list[int]] = {}

    @staticmethod
    def _coarse(fp: list[int]) -> set[int]:
        """Drop the low 4 bits (the noisiest) of each item; mask so signed items bucket as unsigned."""
        return {(v & _MASK) >> 4 for v in fp}

    def add(self, key: int, fp: list[int]) -> None:
        """Index `fp` under `key`."""
        self._fps[key] = fp
        for b in self._coarse(fp):
            self._buckets[b].add(key)

    def candidates(self, fp: list[int], min_shared: int = 10) -> list[int]:
        """Keys sharing at least `min_shared` distinct coarse items with `fp`."""
        counts: dict[int, int] = defaultdict(int)
        for b in self._coarse(fp):
            for key in self._buckets.get(b, ()):
                counts[key] += 1
        return [k for k, c in counts.items() if c >= min_shared]

    def matches(self, fp: list[int], threshold: float = FP_MATCH) -> list[tuple[int, float]]:
        """Candidates scored by `similarity`, those at or above `threshold`, best first."""
        scored = [(k, similarity(fp, self._fps[k])) for k in self.candidates(fp)]
        return sorted((m for m in scored if m[1] >= threshold), key=lambda m: m[1], reverse=True)
