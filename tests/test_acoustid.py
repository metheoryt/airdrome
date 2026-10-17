import http.client
import io
import itertools
import json
import traceback
import urllib.error
import urllib.parse
from email.message import Message

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from airdrome.cloud import acoustid
from airdrome.cloud.acoustid import AcoustIDError, AcoustIDKeyError, identify
from airdrome.enums import BindMethod
from airdrome.models import TrackFile
from airdrome.terminal.app import app
from tests.test_cli_defaults import stub_session  # noqa: F401


KEY = "sekrit-key-123"
MBID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def make_file(session, tmp_path):
    """Factory for TrackFile rows with a stored fingerprint and a real (empty) file on disk."""

    def make(name: str = "a.flac", *, mbid: str | None = None, on_disk: bool = True) -> TrackFile:
        path = tmp_path / name
        if on_disk:
            path.write_bytes(b"x")
        tf = TrackFile(source_path=path, fingerprint=[1, 2, 3], fp_duration=3.0, recording_mbid=mbid)
        session.add(tf)
        session.flush()
        return tf

    return make


class FakeLookup:
    """Queue of canned `lookup` answers (or exceptions to raise) plus a log of the calls made."""

    def __init__(self) -> None:
        """Start with no answers and no calls."""
        self.answers: list = []
        self.calls: list[tuple[SecretStr, str, int]] = []

    def __call__(self, key: SecretStr, fp: str, duration: int):
        """Record the call and return or raise the next queued answer."""
        self.calls.append((key, fp, duration))
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        return ans


@pytest.fixture()
def fake_lookup(monkeypatch) -> FakeLookup:
    """Stub the network and fpcalc."""
    fake = FakeLookup()
    monkeypatch.setattr(acoustid, "lookup", fake)
    monkeypatch.setattr(acoustid, "compressed_fingerprint", lambda path: ("AQAA", 3))
    return fake


def test_stores_confident_single(session, make_file, fake_lookup) -> None:
    """A top result >= 0.9 with exactly one recording id is stored with its AcoustID id."""
    tf = make_file()
    fake_lookup.answers.append([(0.95, "aid-1", [MBID])])
    stats = identify(session, SecretStr("k"))
    assert (stats.seen, stats.stored, stats.ambiguous) == (1, 1, 0)
    assert (tf.recording_mbid, tf.acoustid_id, tf.mbid_source) == (MBID, "aid-1", BindMethod.ACOUSTID)


def test_two_recordings_ambiguous(session, make_file, fake_lookup) -> None:
    """A confident result naming two recordings is not stored."""
    tf = make_file()
    fake_lookup.answers.append([(0.99, "aid-1", [MBID, "22222222-2222-2222-2222-222222222222"])])
    stats = identify(session, SecretStr("k"))
    assert (stats.stored, stats.ambiguous) == (0, 1)
    assert tf.recording_mbid is None


def test_low_score_ambiguous(session, make_file, fake_lookup) -> None:
    """A single recording below the threshold is not stored."""
    tf = make_file()
    fake_lookup.answers.append([(0.89, "aid-1", [MBID])])
    stats = identify(session, SecretStr("k"))
    assert (stats.stored, stats.ambiguous) == (0, 1)
    assert tf.recording_mbid is None


def test_existing_mbid_untouched_and_skipped(session, make_file, fake_lookup) -> None:
    """Files that already have a recording id are not even looked up."""
    tf = make_file(mbid="old")
    stats = identify(session, SecretStr("k"))
    assert stats.seen == 0
    assert fake_lookup.calls == []
    assert tf.recording_mbid == "old"


def test_missing_file_counted(session, make_file, fake_lookup) -> None:
    """A file gone from disk is counted and not looked up."""
    make_file(on_disk=False)
    stats = identify(session, SecretStr("k"))
    assert (stats.seen, stats.missing) == (1, 1)
    assert fake_lookup.calls == []


def test_request_failure_counted_and_run_continues(session, make_file, fake_lookup) -> None:
    """A network error on one file is counted under `failed`; the next file is still looked up."""
    make_file("a.flac")
    b = make_file("b.flac")
    fake_lookup.answers.extend([AcoustIDError("boom"), [(0.95, "aid-2", [MBID])]])
    stats = identify(session, SecretStr("k"))
    assert (stats.failed, stats.stored) == (1, 1)
    assert b.recording_mbid == MBID


def test_invalid_key_aborts(session, make_file, fake_lookup) -> None:
    """An invalid-key answer aborts the run instead of failing every file."""
    make_file("a.flac")
    make_file("b.flac")
    fake_lookup.answers.append(AcoustIDKeyError("rejected"))
    with pytest.raises(AcoustIDKeyError):
        identify(session, SecretStr("secret-key"))
    assert len(fake_lookup.calls) == 1


def test_limit(session, make_file, fake_lookup) -> None:
    """`limit` caps how many files are looked up."""
    make_file("a.flac")
    make_file("b.flac")
    fake_lookup.answers.append([])
    assert identify(session, SecretStr("k"), limit=1).seen == 1


def test_rate_limit_spacing(session, make_file, fake_lookup, monkeypatch) -> None:
    """Consecutive request starts are at least 1/3 s apart on the injected clock."""
    for i in range(3):
        make_file(f"{i}.flac")
        fake_lookup.answers.append([])
    now = [0.0]
    starts: list[float] = []
    real = fake_lookup

    def spy(key: str, fp: str, duration: int):
        starts.append(now[0])
        return real(key, fp, duration)

    monkeypatch.setattr(acoustid, "lookup", spy)
    # Lookups take no time on the fake clock, so the sleeps alone set the spacing.
    identify(
        session, SecretStr("k"), clock=lambda: now[0], sleep=lambda sec: now.__setitem__(0, now[0] + sec)
    )
    assert len(starts) == 3
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(g >= 1 / 3 - 1e-9 for g in gaps)


def test_missing_key_message(stub_session, monkeypatch) -> None:  # noqa: F811
    """Without a client key the command exits non-zero with a message and does no lookup."""
    monkeypatch.setattr("airdrome.conf.settings.acoustid_client_key", None)
    result = CliRunner().invoke(app, ["maint", "acoustid"])
    assert result.exit_code == 1
    assert "ACOUSTID_CLIENT_KEY" in result.output


def test_exactly_threshold_stores(session, make_file, fake_lookup) -> None:
    """The 0.9 boundary is inclusive."""
    tf = make_file()
    fake_lookup.answers.append([(0.9, "aid-1", [MBID])])
    assert identify(session, SecretStr("k")).stored == 1
    assert tf.recording_mbid == MBID


class FakeResponse(io.BytesIO):
    """A urlopen response: a readable body usable as a context manager."""


@pytest.fixture()
def urlopen(monkeypatch):
    """Patch `urllib.request.urlopen`; set `.result` to a payload dict or an exception to raise."""

    class Fake:
        """Records the request and plays back `result`."""

        result: object = {"status": "ok", "results": []}

        def __init__(self) -> None:
            """Start with no recorded request."""
            self.seen: dict = {}

        def __call__(self, url, data=None, timeout=None):
            """Record the call, then raise or return the configured result."""
            self.seen = {"url": url, "data": data, "timeout": timeout}
            if isinstance(self.result, Exception):
                raise self.result
            return FakeResponse(json.dumps(self.result).encode())

    fake = Fake()
    monkeypatch.setattr(acoustid.urllib.request, "urlopen", fake)
    return fake


def http_error(code: int, payload: object) -> urllib.error.HTTPError:
    """An HTTPError whose body is the JSON of `payload`."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return urllib.error.HTTPError(acoustid.LOOKUP_URL, code, "Bad Request", Message(), io.BytesIO(body))


def assert_key_free(exc: BaseException) -> None:
    """The key appears nowhere in the exception's text, repr or full traceback."""
    for text in (str(exc), repr(exc), "".join(traceback.format_exception(exc))):
        assert KEY not in text


def test_lookup_request_and_parsing(urlopen) -> None:
    """The POST carries url, form body and a timeout; an ok payload is parsed to triples."""
    urlopen.result = {
        "status": "ok",
        "results": [{"id": "aid", "score": 0.97, "recordings": [{"id": MBID}]}, {"id": "b", "score": 0.1}],
    }
    out = acoustid.lookup(SecretStr(KEY), "AQAB", 215)
    assert out == [(0.97, "aid", [MBID]), (0.1, "b", [])]
    assert urlopen.seen["url"] == "https://api.acoustid.org/v2/lookup"
    assert urlopen.seen["timeout"] == acoustid.TIMEOUT
    form = urllib.parse.parse_qs(urlopen.seen["data"].decode())
    assert form == {"client": [KEY], "meta": ["recordingids"], "duration": ["215"], "fingerprint": ["AQAB"]}


def test_lookup_invalid_key_from_http_error_body(urlopen) -> None:
    """An HTTP 400 JSON body with error code 4 is a key error that does not echo the key."""
    urlopen.result = http_error(400, {"status": "error", "error": {"code": 4, "message": "invalid API key"}})
    with pytest.raises(AcoustIDKeyError) as ei:
        acoustid.lookup(SecretStr(KEY), "AQAB", 1)
    assert_key_free(ei.value)


@pytest.mark.parametrize(
    "result",
    [
        urllib.error.URLError("refused"),
        TimeoutError(),
        http.client.IncompleteRead(b""),
        http.client.BadStatusLine("x"),
        http_error(500, b"<html>not json"),
        {"status": "ok", "results": [{"id": "a", "score": "high"}]},
        {"status": "ok", "results": [{"id": "a", "score": 1.0, "recordings": [{}]}]},
        {"status": "ok", "results": [None]},
        ["not", "an", "object"],
        {"status": "error", "error": {"code": 3, "message": f"bad request for {KEY}"}},
    ],
    ids=[
        "urlerror",
        "timeout",
        "incomplete",
        "badstatus",
        "http-nonjson",
        "bad-score",
        "no-rec-id",
        "null-result",
        "non-object",
        "api-error-echoing-key",
    ],
)
def test_lookup_failures_are_key_free_errors(urlopen, result) -> None:
    """Every non-key failure is an AcoustIDError that carries no key anywhere."""
    urlopen.result = result
    with pytest.raises(AcoustIDError) as ei:
        acoustid.lookup(SecretStr(KEY), "AQAB", 1)
    assert not isinstance(ei.value, AcoustIDKeyError)
    assert_key_free(ei.value)


def test_malformed_payload_counted_failed(session, make_file, urlopen, monkeypatch) -> None:
    """Through `identify`, a malformed answer is counted under `failed` and the run goes on."""
    monkeypatch.setattr(acoustid, "compressed_fingerprint", lambda path: ("AQAA", 3))
    make_file()
    urlopen.result = ["junk"]
    stats = identify(session, SecretStr(KEY))
    assert (stats.failed, stats.stored) == (1, 0)


def test_invalid_key_abort_message_has_no_key(stub_session, urlopen, monkeypatch) -> None:  # noqa: F811
    """The CLI abort for a rejected key says so without echoing the key."""
    monkeypatch.setattr("airdrome.conf.settings.acoustid_client_key", SecretStr(KEY))
    urlopen.result = http_error(400, {"status": "error", "error": {"code": 4, "message": "invalid API key"}})

    def fake_identify(s, key, *, limit=None):
        return acoustid.lookup(key, "AQAB", 1)

    monkeypatch.setattr("airdrome.terminal.maint.identify", fake_identify)
    result = CliRunner().invoke(app, ["maint", "acoustid"])
    assert result.exit_code == 1
    assert "invalid API key" in result.output
    assert KEY not in result.output
