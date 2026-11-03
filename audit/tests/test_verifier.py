"""Tests for verifier.py against an in-memory stand-in for loki-audit.

Nothing here needs Docker or a running Loki; the two HTTP tests only bind a
loopback port.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar, override
from urllib.parse import parse_qs, urlparse

import audit_event
import pytest
import verifier as v
from audit_event import Actor, AuditEmitter, ChainProblem

NS = v.NS
T0 = v.parse_time("2026-03-01T00:00:00Z", "t")
CFG = v.Config(
    interval_s=300.0,
    silence_ns=300 * NS,
    retention_ns=86400 * NS,
    page_limit=5000,
    chunk_ns=3600 * NS,
    overlap_ns=900 * NS,
    lag_ns=0,
)


class FakeLoki:
    """In-memory loki-audit: ``[start, end)`` ranges, oldest first, ``limit`` per page.

    With ``shuffle_ties`` it picks an arbitrary subset of lines sharing the
    cut-off timestamp, as real Loki may.
    """

    def __init__(self, *, shuffle_ties: bool = False, seed: int = 7) -> None:
        """Start empty; ``seed`` makes the tie shuffling repeatable."""
        self.entries: dict[tuple[int, str, str], v.Entry] = {}
        self.range_calls: list[tuple[int, int, int]] = []
        self.shuffle_ties = shuffle_ties
        self.rng = random.Random(seed)

    def add(self, ts_ns: int, line: str, labels: dict[str, str]) -> None:
        """Store one line; an identical line in the same stream is kept once."""
        entry = v.Entry(ts_ns, tuple(sorted(labels.items())), line)
        self.entries[entry.key()] = entry  # Loki keeps one copy of an identical line

    def query_range(self, query: v.LogQuery, start_ns: int, end_ns: int, limit: int) -> list[v.Entry]:
        """Return the earliest ``limit`` matching lines in ``[start_ns, end_ns)``."""
        self.range_calls.append((start_ns, end_ns, limit))
        hits = [
            entry
            for entry in self.entries.values()
            if start_ns <= entry.ts_ns < end_ns and query.matches(dict(entry.labels), entry.line)
        ]
        if self.shuffle_ties:
            self.rng.shuffle(hits)
            hits.sort(key=lambda entry: entry.ts_ns)
        else:
            hits.sort(key=v.Entry.key)
        return hits[:limit]

    def count_by(self, query: v.LogQuery, label: str, start_ns: int, end_ns: int) -> dict[str, int]:
        """Count matching lines in ``(start_ns, end_ns]`` by one label."""
        counts: dict[str, int] = {}
        for entry in self.entries.values():
            labels = dict(entry.labels)
            if start_ns < entry.ts_ns <= end_ns and query.matches(labels, entry.line):
                counts[labels.get(label, "")] = counts.get(labels.get(label, ""), 0) + 1
        return counts


class Chain:
    """One emitter writing into a buffer; ``take()`` returns the lines written since the last call."""

    def __init__(self, service: str) -> None:
        """Start a chain for ``service`` with the heartbeat thread off."""
        self.buffer = io.StringIO()
        self.emitter = AuditEmitter(service, "1.0.0", stream=self.buffer, heartbeat_interval_s=0)
        self.emitter.start()
        self._taken = 0

    @property
    def instance(self) -> str:
        """The chain id."""
        return self.emitter.instance

    def query(self, count: int = 1, term: str = "term") -> None:
        """Emit ``count`` query events whose criteria contain ``term``."""
        for index in range(count):
            self.emitter.emit(
                "query", "search.test", Actor(user="user.a", via="edge"), criteria={"q": f"{term} {index}"}
            )

    def beat(self, count: int = 1) -> None:
        """Emit ``count`` heartbeat events."""
        for _ in range(count):
            self.emitter.emit("lifecycle", "heartbeat", Actor(user=None, via="system"))

    def close(self) -> None:
        """End the chain with its shutdown event."""
        self.emitter.close()

    def take(self) -> list[str]:
        """Return the lines written since the previous call."""
        lines = self.buffer.getvalue().splitlines()
        new, self._taken = lines[self._taken :], len(lines)
        return new


def labels(project: str, service: str | None = None, container: str | None = None) -> dict[str, str]:
    """Stream labels of an audit-lane line from one compose service."""
    service = service or f"{project}-backend"
    return {
        "lane": "audit",
        "compose_project": project,
        "compose_service": service,
        "container": container or f"{project}-{service}-1",
    }


def put(loki: FakeLoki, lines: list[str], start_ns: int, step_ns: int, stream: dict[str, str]) -> int:
    """Store lines one ``step_ns`` apart from ``start_ns``; return the next free time."""
    for index, line in enumerate(lines):
        loki.add(start_ns + index * step_ns, line, stream)
    return start_ns + len(lines) * step_ns


def kinds(problems: list[ChainProblem]) -> list[str]:
    """The kinds of a list of problems, in order."""
    return [problem.kind for problem in problems]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Make emitters stamp their events with ``clock["ns"]`` instead of the real time."""
    current = {"ns": T0}

    def stamp() -> str:
        moment = datetime.fromtimestamp(current["ns"] // NS, UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{current['ns'] % NS // 1_000_000:03d}Z"

    monkeypatch.setattr(audit_event, "utc_timestamp", stamp)
    return current


CADDY = (
    '{"level":"info","ts":1772323200.5,"logger":"http.log.access.log0","msg":"handled request",'
    '"request":{"method":"GET","uri":"/docint/api/search?<redacted>"},"user":"user.a",'
    '"uuid":"0b9c2f3e-1111-4222-8333-444455556666","status":200}'
)


# --- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8760h", 8760 * 3600 * NS),
        ("1y", 365 * 86400 * NS),
        ("1h30m", 5400 * NS),
        ("500ms", 500_000_000),
        ("2w", 14 * 86400 * NS),
    ],
)
def test_parse_duration(text: str, expected: int) -> None:
    """Loki durations, including combined and year units, parse to nanoseconds."""
    assert v.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "h", "10x", "-1h", "1h 30m", "0h"])
def test_parse_duration_rejects(text: str) -> None:
    """Malformed, negative and zero durations are rejected."""
    with pytest.raises(ValueError, match="not a duration"):
        v.parse_duration(text)


def test_parse_time_accepts_dates_and_zones() -> None:
    """Dates mean midnight UTC; zoned and naive timestamps both parse."""
    assert v.parse_time("2026-03-01", "--from") == T0
    assert v.parse_time("2026-03-01T01:00:00+01:00", "--from") == T0
    assert v.parse_time("2026-03-01T00:00:00", "--from") == T0
    with pytest.raises(ValueError, match="--to is not an ISO 8601"):
        v.parse_time("yesterday", "--to")


def test_logql_quotes_every_value() -> None:
    """Selector values and line filters are quoted so any text stays a literal."""
    ref = 'say "hi" \\ there'
    query = v.LogQuery(labels=(("lane", "audit"), ("compose_project", "docint")), contains=(ref,))
    assert query.logql() == '{lane="audit", compose_project="docint"} |= ' + json.dumps(ref)
    assert v.UNCHAINED.logql() == '{lane="audit"} != "\\"stream\\":\\"audit\\""'


# --- paging ------------------------------------------------------------------


@pytest.mark.parametrize(("per_ts", "limit"), [(3, 4), (4, 4), (1, 1), (2, 3)])
def test_fetch_returns_every_line_once_in_order(per_ts: int, limit: int) -> None:
    """Paging returns every line exactly once, in order, however pages split ties."""
    loki = FakeLoki(shuffle_ties=True)
    streams = [labels("docint"), labels("edge-plane", "caddy")]
    for index in range(30):
        loki.add(T0 + (index // per_ts) * NS, f"line {index}", streams[index % 2])
    expected = sorted(loki.entries.values(), key=v.Entry.key)

    got = list(v.fetch(loki, v.LogQuery(), T0, T0 + 3600 * NS, limit=limit))

    assert got == expected
    assert len(loki.range_calls) > len(expected) // limit


def test_fetch_slices_long_ranges() -> None:
    """Long ranges are read slice by slice."""
    loki = FakeLoki()
    loki.add(T0 + 10 * NS, "a", labels("docint"))
    loki.add(T0 + 7200 * NS, "b", labels("docint"))
    got = list(v.fetch(loki, v.LogQuery(), T0, T0 + 3 * 3600 * NS, chunk_ns=3600 * NS))
    assert [entry.line for entry in got] == ["a", "b"]
    assert [(start - T0) // NS for start, _, _ in loki.range_calls] == [0, 3600, 7200]


# --- chain checks ------------------------------------------------------------


def test_verify_accepts_an_intact_chain() -> None:
    """An intact chain verifies clean; gateway lines are counted, not checked."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(5)
    chain.close()
    put(loki, chain.take(), T0 + NS, NS, labels("docint"))
    loki.add(T0 + 30 * NS, CADDY, labels("edge-plane", "caddy"))
    out: list[str] = []

    assert v.cmd_verify(loki, CFG, T0, T0 + 3600 * NS, out=out.append) == 0
    assert "chained events checked: 7 in 1 chains (0 open at the end)" in out
    assert "unchained lines: 1 (edge-plane 1)" in out
    assert out[-1] == "problems: none"


def test_verify_reports_an_altered_line_and_a_removed_one_without_content() -> None:
    """An altered and a removed line are reported by position, without event content."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(8, term="private words")
    lines = chain.take()
    altered = json.loads(lines[3])
    altered["criteria"] = {"q": "something else"}
    lines[3] = json.dumps(altered, sort_keys=True, separators=(",", ":"))
    del lines[6]
    put(loki, lines, T0 + NS, NS, labels("docint"))
    out: list[str] = []

    assert v.cmd_verify(loki, CFG, T0, T0 + 3600 * NS, out=out.append) == 1
    report = "\n".join(out)
    assert "bad_hash" in report
    assert "seq_gap" in report
    assert f"instance={chain.instance} seq=3" in report
    assert "private words" not in report
    assert "user.a" not in report


def test_a_line_ingested_twice_is_not_a_problem() -> None:
    """The same line stored twice, e.g. after re-ingestion, is checked once."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(3)
    chain.close()
    lines = chain.take()
    put(loki, lines, T0 + NS, NS, labels("docint"))
    put(loki, lines, T0 + NS, NS, {**labels("docint"), "filename": "replayed"})

    result = v.check_range(loki, CFG, T0, T0 + 3600 * NS)

    assert result.problems == []
    assert result.events == len(lines)


def test_an_altered_copy_under_a_known_event_id_is_reported() -> None:
    """A changed line reusing a known event id is not skipped as a duplicate."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(4)
    lines = chain.take()
    nxt = put(loki, lines, T0 + NS, NS, labels("docint"))
    copy = json.loads(lines[2])
    copy["outcome"] = "denied"
    loki.add(nxt, json.dumps(copy, sort_keys=True, separators=(",", ":")), labels("docint"))

    result = v.check_range(loki, CFG, T0, T0 + 3600 * NS)

    assert "seq_repeat" in kinds(result.problems)


def test_events_are_bound_to_their_container() -> None:
    """Events must name their container's project and stay in one container."""
    loki = FakeLoki()
    impostor = Chain("chorus")
    impostor.query(3)
    put(loki, impostor.take(), T0 + NS, NS, labels("docint"))
    mover = Chain("translator")
    mover.query(4)
    lines = mover.take()
    put(loki, lines[:2], T0 + 10 * NS, NS, labels("translator"))
    put(loki, lines[2:], T0 + 20 * NS, NS, labels("translator", container="translator-other-1"))

    result = v.check_range(loki, CFG, T0, T0 + 3600 * NS)

    mismatches = [problem for problem in result.problems if problem.kind == "source_mismatch"]
    assert [(problem.instance, problem.detail) for problem in mismatches] == [
        (impostor.instance, "service chorus in a container of project docint"),
        (mover.instance, "chain continues in another container"),
    ]


def test_first_pass_takes_up_running_chains_but_flags_a_missing_start() -> None:
    """A chain running at the window start is taken up; one starting mid-window without seq 0 is not."""
    loki = FakeLoki()
    now = T0 + 2 * 86400 * NS
    floor = now - CFG.retention_ns
    running = Chain("docint")
    running.query(3)
    put(loki, running.take(), floor - 3600 * NS, NS, labels("docint"))
    running.beat(10)
    running.close()
    put(loki, running.take(), floor + 30 * NS, 60 * NS, labels("docint"))
    late = Chain("translator")
    late.query(5)
    late.close()
    put(loki, late.take()[3:], floor + 1800 * NS, NS, labels("translator"))

    found = v.run_once(loki, v.State(), CFG, now_ns=now)

    assert [(problem.kind, problem.instance) for problem in found] == [("no_start", late.instance)]


# --- serve: state across runs, silence ----------------------------------------


def test_checkpoints_persist_between_runs(tmp_path: Path) -> None:
    """A second process continues from the saved heads and skips lines it re-reads."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(5)
    put(loki, chain.take(), T0 + NS, NS, labels("docint"))
    path = tmp_path / "state.json"
    first = v.State()
    assert v.run_once(loki, first, CFG, now_ns=T0 + 600 * NS, persist=lambda: first.save(path)) == []

    chain.query(5)
    chain.close()
    put(loki, chain.take(), T0 + 700 * NS, NS, labels("docint"))
    second = v.State.load(path)
    assert second.cursor_ns == T0 + 600 * NS
    found = v.run_once(loki, second, CFG, now_ns=T0 + 1300 * NS, persist=lambda: second.save(path))

    assert found == []
    assert second.events_verified == 12  # start, 10 queries, shutdown: each once despite the overlap
    head = second.heads[chain.instance]
    assert (head.seq, head.closed) == (11, True)
    assert v.State.load(path).heads[chain.instance] == head


def test_lines_arriving_late_within_the_overlap_are_still_checked() -> None:
    """Lines stored behind the cursor but within the overlap are checked."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(3)
    put(loki, chain.take(), T0 + 100 * NS, NS, labels("docint"))
    state = v.State()
    v.run_once(loki, state, CFG, now_ns=T0 + 600 * NS)

    chain.query(3)
    put(loki, chain.take(), T0 + 400 * NS, NS, labels("docint"))  # older than the cursor
    chain.close()
    put(loki, chain.take(), T0 + 700 * NS, NS, labels("docint"))

    assert v.run_once(loki, state, CFG, now_ns=T0 + 1000 * NS) == []
    assert state.heads[chain.instance].closed
    assert state.events_verified == 8


def test_a_silent_chain_is_reported_once_as_unterminated(clock: dict[str, int]) -> None:
    """A chain that stops without shutdown while others go on is reported once."""
    loki = FakeLoki()
    clock["ns"] = T0 + NS
    crashed = Chain("docint")
    crashed.query(2)
    put(loki, crashed.take(), T0 + NS, NS, labels("docint"))
    clock["ns"] = T0 + 30 * NS
    alive = Chain("translator")
    for minute in range(20):
        clock["ns"] = T0 + (60 * minute + 30) * NS
        alive.beat()
        put(loki, alive.take(), clock["ns"], NS, labels("translator"))
    state = v.State()

    found = v.run_once(loki, state, CFG, now_ns=T0 + 1300 * NS)
    assert [(problem.kind, problem.instance) for problem in found] == [("unterminated", crashed.instance)]
    assert state.heads[crashed.instance].closed

    for minute in range(20, 30):
        clock["ns"] = T0 + (60 * minute + 30) * NS
        alive.beat()
        put(loki, alive.take(), clock["ns"], NS, labels("translator"))
    assert v.run_once(loki, state, CFG, now_ns=T0 + 1900 * NS) == []
    assert state.problems["unterminated"] == 1
    assert "audit_chains_open 1\n" in v.render_metrics(state, CFG.interval_s)


def test_a_line_dated_in_the_future_cannot_end_other_chains(clock: dict[str, int]) -> None:
    """Silence is measured up to the clock at most, whatever a line claims."""
    loki = FakeLoki()
    clock["ns"] = T0 + NS
    honest = Chain("docint")
    honest.beat()
    put(loki, honest.take(), T0 + NS, NS, labels("docint"))
    clock["ns"] = T0 + 365 * 86400 * NS
    forward = Chain("translator")
    forward.beat()
    put(loki, forward.take(), T0 + 10 * NS, NS, labels("translator"))

    assert v.run_once(loki, v.State(), CFG, now_ns=T0 + 60 * NS) == []


def test_a_chain_spread_over_many_batches_verifies_clean() -> None:
    """Checking page by page carries the chain across batch boundaries."""
    loki = FakeLoki(shuffle_ties=True)
    chain = Chain("docint")
    chain.query(20)
    chain.close()
    for index, line in enumerate(chain.take()):
        loki.add(T0 + NS + index // 2, line, labels("docint"))
    state = v.State()

    found = v.run_once(loki, state, v.Config(retention_ns=86400 * NS, page_limit=3, lag_ns=0), now_ns=T0 + 600 * NS)

    assert found == []
    assert (state.events_verified, state.heads[chain.instance].seq) == (22, 21)


def test_first_pass_reads_the_retention_window_in_day_slices() -> None:
    """A first run over an empty year costs one range query per day."""
    loki = FakeLoki()
    v.run_once(loki, v.State(), v.Config(retention_ns=365 * 86400 * NS, lag_ns=0), now_ns=T0)
    assert len(loki.range_calls) == 365


def test_an_outage_that_silences_every_chain_is_not_reported(clock: dict[str, int]) -> None:
    """When every chain goes quiet at once, no chain is reported unterminated."""
    loki = FakeLoki()
    chains = [Chain("docint"), Chain("translator")]
    for minute in range(10):
        for offset, chain in enumerate(chains):
            clock["ns"] = T0 + (60 * minute + offset) * NS
            chain.beat()
            put(loki, chain.take(), clock["ns"], NS, labels(chain.emitter.service))
    state = v.State()

    assert v.run_once(loki, state, CFG, now_ns=T0 + 3000 * NS) == []
    assert sum(1 for head in state.heads.values() if not head.closed) == 2


def test_problem_log_lines_carry_no_event_content() -> None:
    """Problem log lines hold kind, service, instance and seq only, sanitised."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(3, term="private words")
    lines = chain.take()
    del lines[1]
    put(loki, lines, T0 + NS, NS, labels("docint"))
    logged: list[str] = []

    v.run_once(loki, v.State(), CFG, now_ns=T0 + 600 * NS, report=lambda p: logged.append(v._problem_line(p)))

    assert logged == [
        f'level=warn msg="audit chain problem" kind=seq_gap service=docint instance={chain.instance} seq=2'
    ]
    hostile = ChainProblem("a b\nlevel=info", "x" * 200, 1, "bad_hash", "d")
    line = v._problem_line(hostile)
    assert "\n" not in line
    assert "service=a_b_level_info " in line


# --- metrics -----------------------------------------------------------------


def test_metrics_text() -> None:
    """Every metric is exposed with HELP/TYPE and parses as a sample."""
    state = v.State(events_verified=5, errors=1, last_success=1772323200.5)
    state.problems["bad_hash"] = 2
    state.unchained = {"edge-plane": 7}
    text = v.render_metrics(state, 300.0)

    for name, kind in [
        ("audit_events_verified_total", "counter"),
        ("audit_chain_problems_total", "counter"),
        ("audit_chains_open", "gauge"),
        ("audit_unchained_lines_total", "counter"),
        ("audit_verifier_last_success_timestamp_seconds", "gauge"),
        ("audit_verifier_errors_total", "counter"),
        ("audit_verifier_interval_seconds", "gauge"),
    ]:
        assert f"# TYPE {name} {kind}\n" in text
    for problem_kind in v.PROBLEM_KINDS:
        assert f'audit_chain_problems_total{{kind="{problem_kind}"}} ' in text
    assert 'audit_chain_problems_total{kind="bad_hash"} 2\n' in text
    assert 'audit_unchained_lines_total{project="edge-plane"} 7\n' in text
    assert "audit_events_verified_total 5\n" in text
    assert "audit_verifier_last_success_timestamp_seconds 1772323200.5\n" in text
    assert "audit_verifier_interval_seconds 300.0\n" in text
    for line in text.splitlines():
        if not line.startswith("#"):
            float(line.rsplit(" ", 1)[1])


def test_metrics_server_serves_only_metrics() -> None:
    """The metrics server answers ``/metrics`` and nothing else."""
    server = v.start_metrics_server(lambda: "x 1\n", 0, "127.0.0.1")
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"x 1\n"
            assert response.headers["Content-Type"].startswith("text/plain; version=0.0.4")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base}/", timeout=5)
        assert error.value.code == 404
    finally:
        server.shutdown()


def test_state_round_trips_through_json() -> None:
    """The saved state reads back identical."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(1)
    put(loki, chain.take(), T0 + NS, NS, labels("docint"))
    state = v.State(cursor_ns=T0, events_verified=3, unchained={"edge-plane": 2})
    entries = list(v.fetch(loki, v.CHAINED, T0, T0 + 60 * NS))
    v.check_window(state, entries, silence_ns=CFG.silence_ns, cap_ns=T0 + 60 * NS)

    restored = v.State.from_json(json.loads(json.dumps(state.to_json())))

    assert restored == state


# --- export and anchor ---------------------------------------------------------


def _trail(loki: FakeLoki) -> tuple[Chain, Chain]:
    """A day with a chain running across midnight, one that ends, and gateway lines."""
    running = Chain("docint")
    running.query(2)
    put(loki, running.take(), T0 - 600 * NS, NS, labels("docint"))
    for minute in range(30):
        running.beat()
        put(loki, running.take(), T0 + (60 * minute + 5) * NS, NS, labels("docint"))
    ended = Chain("translator")
    ended.query(3)
    ended.close()
    for index, line in enumerate(ended.take()):  # two lines per nanosecond
        loki.add(T0 + 120 * NS + index // 2, line, labels("translator"))
    for index in range(5):
        loki.add(T0 + (200 + index) * NS, CADDY.replace("0b9c2f3e", f"0b9c2f3{index}"), labels("edge-plane", "caddy"))
    loki.add(T0 + 86400 * NS + NS, CADDY, labels("edge-plane", "caddy"))  # next day
    return running, ended


def test_export_writes_jsonl_with_checksum_and_filters(tmp_path: Path) -> None:
    """Exports are sorted JSONL with a checksum; filters narrow them, the reference stays out of the name."""
    loki = FakeLoki()
    _trail(loki)
    end = T0 + 86400 * NS

    path = v.cmd_export(loki, CFG, tmp_path, T0, end)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert path.name == "audit-20260301T000000Z-20260302T000000Z.jsonl"
    assert len(records) == 30 + 5 + 5
    assert all(set(record) == {"labels", "line", "ts"} for record in records)
    assert [record["ts"] for record in records] == sorted(record["ts"] for record in records)
    checksum = (tmp_path / f"{path.name}.sha256").read_text()
    assert checksum == f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"

    gateway = v.cmd_export(loki, CFG, tmp_path, T0, end, service="edge-plane")
    assert gateway.name.endswith("-edge-plane.jsonl")
    assert len(gateway.read_text().splitlines()) == 5

    one = v.cmd_export(loki, CFG, tmp_path, T0, end, ref="0b9c2f32")
    assert "0b9c2f32" not in one.name
    assert one.name.endswith(f"-ref{hashlib.sha256(b'0b9c2f32').hexdigest()[:8]}.jsonl")
    assert len(one.read_text().splitlines()) == 1


def test_export_does_not_depend_on_page_size(tmp_path: Path) -> None:
    """The same range exports to the same bytes whatever the page size."""
    small, large = tmp_path / "small", tmp_path / "large"
    small.mkdir()
    large.mkdir()
    loki = FakeLoki(shuffle_ties=True)
    _trail(loki)
    end = T0 + 86400 * NS

    a = v.cmd_export(loki, v.Config(page_limit=2, lag_ns=0), small, T0, end)
    b = v.cmd_export(loki, v.Config(page_limit=5000, lag_ns=0), large, T0, end)

    assert a.read_bytes() == b.read_bytes()


def test_anchor_is_deterministic_and_matches_the_day_export(tmp_path: Path) -> None:
    """The anchor is reproducible and its digest is the day export's checksum."""
    loki = FakeLoki(shuffle_ties=True)
    running, ended = _trail(loki)
    day = date(2026, 3, 1)
    later = T0 + 2 * 86400 * NS
    first, second, export_dir = tmp_path / "1", tmp_path / "2", tmp_path / "x"
    for directory in (first, second, export_dir):
        directory.mkdir()

    a = v.cmd_anchor(loki, v.Config(page_limit=3, lag_ns=0), first, day, now_ns=later)
    b = v.cmd_anchor(loki, v.Config(page_limit=5000, lag_ns=0), second, day, now_ns=later)
    export = v.cmd_export(loki, CFG, export_dir, T0, T0 + 86400 * NS)

    assert a.name == "anchor-2026-03-01.json"
    assert a.read_bytes() == b.read_bytes()
    anchor = json.loads(a.read_text())
    assert anchor["sha256"] == hashlib.sha256(export.read_bytes()).hexdigest()
    assert anchor["lines"] == 40
    assert anchor["lines_by_project"] == {"docint": 30, "edge-plane": 5, "translator": 5}
    assert anchor["chain_problems"] == {}
    heads = {head["instance"]: head for head in anchor["chains"]}
    assert (heads[running.instance]["seq"], heads[running.instance]["closed"]) == (32, False)
    assert (heads[ended.instance]["seq"], heads[ended.instance]["closed"]) == (4, True)


def test_anchor_refuses_a_day_that_has_not_settled(tmp_path: Path) -> None:
    """A day that ended less than the overlap ago is not anchored yet."""
    with pytest.raises(ValueError, match="ended at least 15 minutes ago"):
        v.cmd_anchor(FakeLoki(), CFG, tmp_path, date(2026, 3, 1), now_ns=T0 + 86400 * NS + 60 * NS)


# --- the HTTP client and the command line ---------------------------------------


class _StubLoki(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, list[str]]]] = []

    def do_GET(self) -> None:
        """Answer like Loki's query API; a query containing ``fail`` gets HTTP 500."""
        url = urlparse(self.path)
        params = parse_qs(url.query)
        _StubLoki.seen.append({"path": [url.path], **params})
        if "fail" in params.get("query", [""])[0]:
            self.send_error(500)
            return
        if url.path == "/loki/api/v1/query_range":
            data = {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"lane": "audit", "compose_project": "docint"},
                        "values": [["1772323200000000001", "b"], ["1772323200000000000", "a"]],
                    }
                ],
            }
        else:
            data = {
                "resultType": "vector",
                "result": [{"metric": {"compose_project": "edge-plane"}, "value": [1772323200, "7"]}],
            }
        body = json.dumps({"status": "success", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet."""
        return


@pytest.fixture
def stub_loki() -> Iterator[str]:
    """A loopback HTTP server standing in for Loki's query API."""
    _StubLoki.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubLoki)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_http_client_reads_streams_and_counts(stub_loki: str) -> None:
    """The HTTP client sends forward range queries and parses streams and vectors."""
    loki = v.HttpLoki(stub_loki)

    entries = loki.query_range(v.CHAINED, T0, T0 + NS, 100)
    assert [(entry.ts_ns, entry.line) for entry in entries] == [(1772323200000000000, "a"), (1772323200000000001, "b")]
    assert dict(entries[0].labels) == {"lane": "audit", "compose_project": "docint"}
    sent = _StubLoki.seen[-1]
    assert sent["query"] == [v.CHAINED.logql()]
    assert (sent["start"], sent["end"], sent["limit"], sent["direction"]) == (
        [str(T0)],
        [str(T0 + NS)],
        ["100"],
        ["forward"],
    )

    assert loki.count_by(v.UNCHAINED, "compose_project", T0, T0 + 3600 * NS) == {"edge-plane": 7}
    assert _StubLoki.seen[-1]["query"] == [
        f"sum by (compose_project) (count_over_time({v.UNCHAINED.logql()} [3600000ms]))"
    ]

    with pytest.raises(v.LokiError) as error:
        loki.query_range(v.LogQuery(contains=("fail secret-ref",)), T0, T0 + NS, 10)
    assert str(error.value) == "loki answered HTTP 500"


def test_command_line_errors_do_not_echo_the_reference(capsys: pytest.CaptureFixture[str]) -> None:
    """A usage error never repeats the ``--ref`` value."""
    code = v.main(["export", "--from", "nope", "--to", "2026-03-02", "--ref", "secret-ref-123"])

    err = capsys.readouterr().err
    assert code == 2
    assert "--from is not an ISO 8601 date or timestamp" in err
    assert "secret-ref-123" not in err
