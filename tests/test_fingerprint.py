import json
import os
import random
from pathlib import Path

import pytest

from airdrome.cloud.acoustid import compressed_fingerprint
from airdrome.library import fingerprint
from airdrome.library.fingerprint import FP_MATCH, FingerprintError, FingerprintIndex, compute, similarity


@pytest.fixture()
def fps(make_wav, tmp_path):
    """Fingerprints of seed 1 (gain 1.0 and 0.6 at 22.05 kHz) and seed 2."""
    one = compute(make_wav(tmp_path / "a.wav", 1))
    one_quiet = compute(make_wav(tmp_path / "b.wav", 1, gain=0.6, rate=22050))
    two = compute(make_wav(tmp_path / "c.wav", 2))
    return one, one_quiet, two


def test_same_audio_scores_high(fps):
    """The same signal at another gain and sample rate still matches."""
    one, one_quiet, _ = fps
    assert similarity(one.raw, one_quiet.raw) >= FP_MATCH


def test_different_audio_scores_low(fps):
    """Unrelated signals score well below the match threshold."""
    one, _, two = fps
    assert similarity(one.raw, two.raw) < 0.6


def test_index_finds_match_without_pairwise(fps):
    """The inverted index surfaces the right key among unrelated fingerprints."""
    one, one_quiet, _ = fps
    rng = random.Random(0)
    index = FingerprintIndex()
    for key in range(50):
        index.add(key, [rng.randint(-(2**31), 2**31 - 1) for _ in range(len(one.raw))])
    index.add(1000, one.raw)
    found = index.matches(one_quiet.raw)
    assert found
    assert found[0][0] == 1000
    assert 1000 in index.candidates(one_quiet.raw)


def test_compute_missing_binary(monkeypatch, tmp_path):
    """A missing fpcalc surfaces as FingerprintError."""
    monkeypatch.setenv("PATH", "")
    with pytest.raises(FingerprintError):
        compute(tmp_path / "x.wav")


def test_compute_failure(tmp_path):
    """A non-audio file makes fpcalc exit non-zero."""
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(FingerprintError):
        compute(bad)


def test_values_above_int31_equal_signed_form():
    """Unsigned and signed spellings of the same 32-bit items are identical for similarity."""
    unsigned = [0xFFFFFFFF, 0x80000000, 0xDEADBEEF, 0x12345678] * 10
    signed = [v - 2**32 if v >= 2**31 else v for v in unsigned]
    assert similarity(signed, signed) == 1.0
    assert similarity(unsigned, signed) == 1.0
    # 0xFFFFFFFF vs 0 differ in all 32 bits; a negative int's raw bit_count would be wrong here.
    assert similarity([-1] * 30, [0] * 30, max_offset=0) == 0.0
    # Exactly one item in 30 differs by 32 bits -> 1 - 32/(32*30).
    assert similarity([-1] + [0] * 29, [0] * 30, max_offset=0) == pytest.approx(1 - 1 / 30)


def test_similarity_short_or_empty_is_zero():
    """Overlap under 20 items, or empty input, scores 0.0."""
    assert similarity([], [1] * 50) == 0.0
    assert similarity([1] * 10, [1] * 10) == 0.0


def test_fp_match_sits_in_the_measured_gap() -> None:
    """FP_MATCH lies between the live library's highest false link and lowest same-id pair.

    Calibrated 2026-10-17 (docs/design/identity-ladder.md, *Calibration*): vocal vs instrumental
    versions linked at up to 0.884; the lowest same-recording pair above the gap scored 0.890.
    """
    # A failure here means the threshold moved without new numbers: re-run
    # `scripts/identity_acceptance.py calibrate` and update the bounds and the design doc — do not
    # just revert the constant.
    assert 0.884 < FP_MATCH <= 0.890


# ── fpcalc robustness (fake binary on PATH) ─────────────────────────────────────


@pytest.fixture()
def fake_fpcalc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory: put an `fpcalc` on PATH that prints `stdout` (bytes), `stderr` (bytes) and exits `code`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def _install(stdout: bytes = b"", stderr: bytes = b"", code: int = 0, sleep: float = 0.0) -> None:
        (bindir / "out").write_bytes(stdout)
        (bindir / "err").write_bytes(stderr)
        script = bindir / "fpcalc"
        script.write_text(
            f'#!/bin/sh\nsleep {sleep}\ncat "{bindir}/out"\ncat "{bindir}/err" >&2\nexit {code}\n'
        )
        script.chmod(0o755)

    return _install


def _raw_json(n: int) -> bytes:
    """fpcalc -raw -json output with `n` items."""
    return json.dumps({"duration": 200.5, "fingerprint": list(range(1, n + 1))}).encode()


def test_exit_3_with_usable_fingerprint_is_kept(fake_fpcalc, tmp_path) -> None:
    """fpcalc 1.6 prints a fingerprint, then exits 3 on a decode error: enough items are kept."""
    fake_fpcalc(stdout=_raw_json(30), stderr=b"ERROR: Error decoding audio frame", code=3)
    fp = compute(tmp_path / "x.mp3")
    assert (len(fp.raw), fp.duration) == (30, 200.5)


def test_exit_3_with_too_few_items_fails(fake_fpcalc, tmp_path) -> None:
    """A decode error that left fewer items than one comparison needs is a failure."""
    fake_fpcalc(stdout=_raw_json(5), code=3)
    with pytest.raises(FingerprintError):
        compute(tmp_path / "x.mp3")


def test_exit_3_without_json_fails(fake_fpcalc, tmp_path) -> None:
    """Exit 3 with nothing parseable is a failure, not a crash."""
    fake_fpcalc(stderr=b"ERROR: could not open", code=3)
    with pytest.raises(FingerprintError):
        compute(tmp_path / "x.mp3")


def test_other_nonzero_exit_fails_even_with_json(fake_fpcalc, tmp_path) -> None:
    """Only exit 3 is salvaged."""
    fake_fpcalc(stdout=_raw_json(30), code=2)
    with pytest.raises(FingerprintError):
        compute(tmp_path / "x.mp3")


def test_non_utf8_output_is_a_fingerprint_error(fake_fpcalc, tmp_path) -> None:
    """Stray non-UTF-8 bytes on either stream never escape as UnicodeDecodeError."""
    fake_fpcalc(stdout=b"\xff\xfe not json", stderr=b"bad \xff path", code=2)
    with pytest.raises(FingerprintError, match="bad"):
        compute(tmp_path / "x.mp3")
    fake_fpcalc(stdout=b"\xff\xfe not json")
    with pytest.raises(FingerprintError):
        compute(tmp_path / "x.mp3")


def test_timeout_is_a_fingerprint_error(fake_fpcalc, tmp_path, monkeypatch) -> None:
    """A hung fpcalc is killed after FPCALC_TIMEOUT and counted as a failure."""
    monkeypatch.setattr(fingerprint, "FPCALC_TIMEOUT", 0.2)
    fake_fpcalc(stdout=_raw_json(30), sleep=5)
    with pytest.raises(FingerprintError, match="timed out"):
        compute(tmp_path / "x.mp3")
    with pytest.raises(FingerprintError, match="timed out"):
        compressed_fingerprint(tmp_path / "x.mp3")


def test_compressed_fingerprint_shares_the_exit_3_rule(fake_fpcalc, tmp_path) -> None:
    """AcoustID's compressed call goes through the same helper: exit 3 with a fingerprint string is kept."""
    fake_fpcalc(stdout=json.dumps({"duration": 200.5, "fingerprint": "AQAAxyz"}).encode(), code=3)
    assert compressed_fingerprint(tmp_path / "x.mp3") == ("AQAAxyz", 200)
    fake_fpcalc(stdout=json.dumps({"duration": 200.5, "fingerprint": ""}).encode(), code=3)
    with pytest.raises(FingerprintError):
        compressed_fingerprint(tmp_path / "x.mp3")
