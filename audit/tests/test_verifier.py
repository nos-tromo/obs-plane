"""Tests for verifier.py against an in-memory stand-in for loki-audit.

Nothing here needs Docker or a running Loki; the two HTTP tests only bind a
loopback port.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar, override
from urllib.parse import parse_qs, urlparse

import audit_event
import pytest
import verifier as v
from audit_event import Actor, AuditEmitter, ChainHead, ChainProblem

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


def test_a_line_that_is_not_json_is_reported_once() -> None:
    """A marked line without an event id is remembered by its store time, so a re-read does not count it again."""
    loki = FakeLoki()
    loki.add(T0 + 500 * NS, '{"stream":"audit","seq":', labels("docint"))
    state = v.State()

    assert kinds(v.run_once(loki, state, CFG, now_ns=T0 + 600 * NS)) == ["schema"]
    assert v.run_once(loki, state, CFG, now_ns=T0 + 900 * NS) == []  # read again within the overlap
    assert state.problems["schema"] == 1


def test_events_from_another_source_stay_out_of_the_chains() -> None:
    """An event must name the compose project it was sent from (any case); others are foreign_source."""
    loki = FakeLoki()
    impostor = Chain("chorus")  # a complete, consistent chain printed by a docint container
    impostor.query(2)
    impostor.close()
    put(loki, impostor.take(), T0 + NS, NS, labels("docint"))
    genuine = Chain("Translator")  # differs from its label only in case
    genuine.query(2)
    genuine.close()
    lines = genuine.take()
    put(loki, lines, T0 + 10 * NS, NS, labels("translator"))
    loki.add(T0 + 20 * NS, lines[1], labels("docint"))  # a genuine line, resent from docint
    unlabelled = {name: value for name, value in labels("translator").items() if name != "compose_project"}
    loki.add(T0 + 30 * NS, lines[2], unlabelled)

    result = v.check_range(loki, CFG, T0, T0 + 3600 * NS)

    foreign = [problem for problem in result.problems if problem.kind == "foreign_source"]
    assert len(foreign) == 6
    assert (foreign[0].service, foreign[0].instance, foreign[0].detail) == (
        "docint",
        impostor.instance,
        "claims service chorus, sent from project docint",
    )
    assert (foreign[-1].service, foreign[-1].detail) == ("?", "claims service Translator, sent from project ?")
    assert [problem for problem in result.problems if problem.kind != "foreign_source"] == []
    assert set(result.heads) == {genuine.instance}

    state = v.State()
    v.run_once(loki, state, CFG, now_ns=T0 + 100 * NS)
    v.run_once(loki, state, CFG, now_ns=T0 + 200 * NS)  # re-reads every line within the overlap
    assert 'audit_chain_problems_total{kind="foreign_source"} 6\n' in v.render_metrics(state, CFG.interval_s)


def test_a_chain_keeps_one_service() -> None:
    """An event matching its label only case-insensitively still cannot switch its chain's service."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(2)
    lines = chain.take()
    switched = json.loads(lines[2])
    switched["service"] = "DocInt"
    switched["hash"] = audit_event.event_hash(switched)
    lines[2] = audit_event.canonical_json(switched)
    put(loki, lines, T0 + NS, NS, labels("docint"))

    result = v.check_range(loki, CFG, T0, T0 + 3600 * NS)

    assert [(problem.kind, problem.seq) for problem in result.problems] == [("service_mismatch", 2)]
    assert result.heads[chain.instance].seq == 1


def test_a_sibling_container_cannot_write_into_a_chain() -> None:
    """A line for a known chain from another container of the same project is set aside."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(2)
    lines = chain.take()
    put(loki, lines, T0 + NS, NS, labels("docint"))
    forged = json.loads(lines[2])  # a valid continuation, printed by the project's frontend
    forged.update(seq=3, prev_hash=forged["hash"], event_id="01a1a000-0000-7000-8000-000000000001")
    forged["hash"] = audit_event.event_hash(forged)
    loki.add(T0 + 10 * NS, audit_event.canonical_json(forged), labels("docint", "docint-frontend"))
    loki.add(T0 + 15 * NS, lines[1], labels("docint", "docint-frontend"))  # a verbatim replay
    chain.query(1)
    chain.close()
    put(loki, chain.take(), T0 + 20 * NS, NS, labels("docint"))

    result = v.check_range(loki, CFG, T0, T0 + 3600 * NS)

    assert [(problem.kind, problem.seq) for problem in result.problems] == [
        ("source_mismatch", 3),
        ("source_mismatch", 1),
    ]
    assert result.problems[0].detail == (
        "chain of container docint-docint-backend-1, sent from docint-docint-frontend-1"
    )
    head = result.heads[chain.instance]
    assert (head.seq, head.closed) == (4, True)


def test_the_container_binding_survives_a_restart(tmp_path: Path) -> None:
    """A restarted verifier still knows which container each open chain belongs to."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(1)
    put(loki, chain.take(), T0 + NS, NS, labels("docint"))
    path = tmp_path / "state.json"
    first = v.State()
    v.run_once(loki, first, CFG, now_ns=T0 + 600 * NS, persist=lambda: first.save(path))

    second = v.State.load(path)
    assert second.sources == {chain.instance: ("docint", "docint-docint-backend-1")}
    chain.query(1)
    put(loki, chain.take(), T0 + 700 * NS, NS, labels("docint", "docint-worker"))
    found = v.run_once(loki, second, CFG, now_ns=T0 + 800 * NS)

    assert [(problem.kind, problem.instance) for problem in found] == [("source_mismatch", chain.instance)]
    assert second.heads[chain.instance].seq == 1


def test_pruning_drops_the_binding_with_its_head() -> None:
    """A chain closed long ago is forgotten together with its container binding."""
    stamp = "2026-02-01T00:00:00.000Z"
    old = ChainHead(service="docint", instance="a" * 16, seq=3, hash="0" * 64, ts=stamp, closed=True)
    live = ChainHead(service="docint", instance="b" * 16, seq=3, hash="0" * 64, ts=stamp, closed=False)
    state = v.State(heads={old.instance: old, live.instance: live})
    state.sources = {old.instance: ("docint", "docint-a-1"), live.instance: ("docint", "docint-b-1")}

    v.prune(state, seen_before_ns=0, heads_before_ns=T0)

    assert set(state.heads) == set(state.sources) == {live.instance}


def forged_start(instance: str) -> str:
    """A well-formed start event for an existing chain id, as another container could print it."""
    start = json.loads(Chain("docint").take()[0])
    start["instance"] = instance
    start["hash"] = audit_event.event_hash(start)
    return audit_event.canonical_json(start)


def running_chain(loki: FakeLoki, at_ns: int) -> Chain:
    """A docint chain whose start and first three queries were written before ``at_ns``; seq 4 and 5 are stored."""
    chain = Chain("docint")
    chain.query(3)
    chain.take()
    chain.query(2)
    put(loki, chain.take(), at_ns, NS, labels("docint"))
    return chain


def test_a_start_after_other_lines_of_its_chain_binds_nothing() -> None:
    """A start printed after lines of a chain met mid-way cannot be genuine: it is checked, never bound."""
    loki = FakeLoki()
    chain = running_chain(loki, T0 + 10 * NS)
    loki.add(T0 + 10 * NS + NS // 2, forged_start(chain.instance), labels("docint", "docint-frontend"))
    state = v.State()

    checked, problems = v.check_window(
        state,
        list(v.fetch(loki, v.CHAINED, T0, T0 + 60 * NS)),
        silence_ns=CFG.silence_ns,
        cap_ns=T0 + 60 * NS,
        window_start_ns=T0,
    )

    assert [(problem.kind, problem.seq) for problem in problems] == [("ambiguous_source", 0), ("seq_gap", 4)]
    assert (problems[0].service, problems[0].detail) == (
        "docint",
        "unbound chain, lines from docint-docint-backend-1 and docint-docint-frontend-1",
    )
    assert (checked, state.sources, state.heads[chain.instance].seq) == (3, {}, 5)


def test_a_start_for_a_chain_already_in_the_state_binds_nothing() -> None:
    """A chain with a head in the state has been met: a start arriving in a later run is not bound."""
    loki = FakeLoki()
    now = T0 + 86400 * NS  # the first run reads from T0
    chain = running_chain(loki, T0 + 10 * NS)
    state = v.State()
    assert v.run_once(loki, state, CFG, now_ns=now) == []

    loki.add(now + 10 * NS, forged_start(chain.instance), labels("docint", "docint-frontend"))
    chain.query(1)
    put(loki, chain.take(), now + 11 * NS, NS, labels("docint"))
    found = v.run_once(loki, state, CFG, now_ns=now + 60 * NS)

    assert state.sources == {}
    assert [(problem.kind, problem.seq) for problem in found] == [
        ("ambiguous_source", 6),
        ("seq_gap", 0),
        ("seq_gap", 6),
    ]
    assert state.events_verified == 4


@pytest.mark.parametrize(
    ("change", "found"),
    [({"operation": "heartbeat"}, []), ({"prev_hash": "e" * 64}, ["broken_link"])],
)
def test_only_the_start_event_binds_a_new_chain(change: dict[str, str], found: list[str]) -> None:
    """Only the emitter's lifecycle start at seq 0, naming the genesis hash, binds a chain."""
    chain = Chain("docint")
    first = json.loads(chain.take()[0])
    first.update(change)
    first["hash"] = audit_event.event_hash(first)
    loki = FakeLoki()
    loki.add(T0 + NS, audit_event.canonical_json(first), labels("docint"))
    state = v.State()

    checked, problems = v.check_window(
        state, list(v.fetch(loki, v.CHAINED, T0, T0 + 60 * NS)), silence_ns=CFG.silence_ns, cap_ns=T0 + 60 * NS
    )

    assert (checked, kinds(problems), state.sources, state.heads[chain.instance].seq) == (1, found, {}, 0)


def test_a_chain_taken_up_mid_way_stays_unbound_and_checks_clean() -> None:
    """A chain running when the verifier first starts is checked clean to its end and never bound."""
    loki = FakeLoki()
    now = T0 + 86400 * NS  # the first run reads from T0
    chain = running_chain(loki, T0 + 10 * NS)
    state = v.State()
    assert v.run_once(loki, state, CFG, now_ns=now) == []

    chain.query(2)
    chain.close()
    put(loki, chain.take(), now + 10 * NS, NS, labels("docint"))
    assert v.run_once(loki, state, CFG, now_ns=now + 60 * NS) == []

    head = state.heads[chain.instance]
    assert (head.seq, head.closed, state.sources, state.events_verified) == (8, True, {}, 5)


def put_with_copies(loki: FakeLoki, lines: list[str], at_ns: int) -> None:
    """Store each line from the backend, and one second later a copy of it from the frontend.

    The copies keep their ``seq`` but not the chain's hashes, as a container
    without the store's content would print them.
    """
    for index, line in enumerate(lines):
        forged = json.loads(line)
        forged.update(prev_hash="f" * 64, event_id=f"01a1a000-0000-7000-8000-00000000000{index}")
        forged["hash"] = audit_event.event_hash(forged)
        loki.add(at_ns + 2 * index * NS, line, labels("docint"))
        loki.add(at_ns + (2 * index + 1) * NS, audit_event.canonical_json(forged), labels("docint", "docint-frontend"))


def test_an_unbound_chain_from_two_containers_is_checked_in_full_and_reported() -> None:
    """Nothing is set aside: every line is checked, the second container is reported once per run."""
    loki = FakeLoki()
    now = T0 + 86400 * NS
    chain = running_chain(loki, T0 + 10 * NS)
    state = v.State()
    assert v.run_once(loki, state, CFG, now_ns=now) == []
    chain.query(2)
    put_with_copies(loki, chain.take(), now + 10 * NS)  # seq 6 and 7
    expected = [("ambiguous_source", 6), ("seq_repeat", 6), ("seq_repeat", 7)]

    found = v.run_once(loki, state, CFG, now_ns=now + 60 * NS)

    assert [(problem.kind, problem.seq) for problem in found] == expected
    assert found[0].detail == "unbound chain, lines from docint-docint-backend-1 and docint-docint-frontend-1"
    assert (state.events_verified, state.sources) == (6, {})  # two lines in the first run, all four now
    assert v.run_once(loki, state, CFG, now_ns=now + 120 * NS) == []  # read again: already reported
    assert (state.problems["ambiguous_source"], state.problems["seq_repeat"], state.events_verified) == (1, 2, 6)
    result = v.check_range(loki, CFG, T0, now + 120 * NS)  # make audit-verify finds them again
    assert [(problem.kind, problem.seq) for problem in result.problems] == expected


def test_a_second_container_is_reported_once_per_run_across_batches() -> None:
    """The run, not the batch, decides: with one line per batch the frontend is still reported, once."""
    loki = FakeLoki()
    now = T0 + 86400 * NS
    one_by_one = dataclasses.replace(CFG, page_limit=1)
    chain = running_chain(loki, T0 + 10 * NS)
    state = v.State()
    assert v.run_once(loki, state, one_by_one, now_ns=now) == []
    chain.query(2)
    put_with_copies(loki, chain.take(), now + 10 * NS)

    found = v.run_once(loki, state, one_by_one, now_ns=now + 60 * NS)
    ranged = v.check_range(loki, one_by_one, T0, now + 120 * NS)

    for problems in (found, ranged.problems):
        assert [(problem.kind, problem.seq) for problem in problems if problem.kind == "ambiguous_source"] == [
            ("ambiguous_source", 6)
        ]
        assert len(problems) > 1  # the copies also break the chain
    assert state.events_verified == 6


def test_a_chain_id_seen_again_after_pruning_is_a_new_chain(clock: dict[str, int]) -> None:
    """Once a closed chain is pruned, its id binds again at its next start, like any new chain."""
    chain = Chain("docint")
    chain.close()
    lines = chain.take()
    state = v.State()
    window = [v.Entry(T0 + index, tuple(sorted(labels("docint").items())), line) for index, line in enumerate(lines)]
    v.check_window(state, window, silence_ns=CFG.silence_ns, cap_ns=T0 + 60 * NS)
    assert state.sources == {chain.instance: ("docint", "docint-docint-backend-1")}

    v.prune(state, seen_before_ns=T0 + 60 * NS, heads_before_ns=T0 + NS)
    assert (state.heads, state.sources) == ({}, {})
    reused = v.Entry(T0 + 120 * NS, tuple(sorted(labels("docint", "docint-worker").items())), lines[0])
    v.check_window(state, [reused], silence_ns=CFG.silence_ns, cap_ns=T0 + 180 * NS)

    assert state.sources == {chain.instance: ("docint", "docint-docint-worker-1")}


def test_many_forged_starts_are_checked_in_linear_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thousands of forged starts for one chain cost one pass over the lines, nothing per start."""
    calls: list[int] = []
    verify = v.verify_chain

    def counting(events: list[object], *args: Any, **kwargs: Any) -> audit_event.ChainReport:
        calls.append(len(events))
        return verify(events, *args, **kwargs)

    monkeypatch.setattr(v, "verify_chain", counting)
    chain = Chain("docint")
    chain.query(4)
    met_first = chain.take()[3:]  # seq 3 and 4: the chain is met mid-way, so it stays unbound
    template = json.loads(forged_start(chain.instance))

    def window(count: int) -> list[v.Entry]:
        entries = [
            v.Entry(T0 + index, tuple(sorted(labels("docint").items())), line) for index, line in enumerate(met_first)
        ]
        for index in range(count):
            forged = dict(template, event_id=f"forged-{index}")
            forged["hash"] = audit_event.event_hash(forged)
            stream = tuple(sorted(labels("docint", f"docint-forger-{index}").items()))
            entries.append(v.Entry(T0 + 100 + index, stream, audit_event.canonical_json(forged)))
        return entries

    def fastest(entries: list[v.Entry]) -> tuple[float, list[ChainProblem]]:
        timings: list[float] = []
        problems: list[ChainProblem] = []
        for _ in range(3):
            started = time.perf_counter()
            _, problems = v.check_window(v.State(), entries, silence_ns=CFG.silence_ns, cap_ns=T0 + 60 * NS)
            timings.append(time.perf_counter() - started)
        return min(timings), problems

    small, _ = fastest(window(1000))
    large, problems = fastest(window(4000))

    assert calls == [1002] * 3 + [4002] * 3  # every line checked once, in a single call
    assert sum(problem.kind == "ambiguous_source" for problem in problems) == 4000
    assert large < 10 * small  # 4x the lines: about 4x the time, not 16x


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


def test_nothing_may_follow_a_shutdown_across_runs(tmp_path: Path) -> None:
    """An ended chain stays ended through the saved state: a later continuation is after_shutdown."""
    loki = FakeLoki()
    chain = Chain("docint")
    chain.query(1)
    chain.close()
    lines = chain.take()
    put(loki, lines, T0 + NS, NS, labels("docint"))
    path = tmp_path / "state.json"
    first = v.State()
    assert v.run_once(loki, first, CFG, now_ns=T0 + 600 * NS, persist=lambda: first.save(path)) == []

    shutdown = json.loads(lines[-1])  # a valid next event for the chain, from its own container
    appended = dict(shutdown, seq=3, prev_hash=shutdown["hash"], operation="heartbeat")
    appended.update(event_id="01a1a000-0000-7000-8000-0000000000e1")
    appended["hash"] = audit_event.event_hash(appended)
    loki.add(T0 + 700 * NS, audit_event.canonical_json(appended), labels("docint"))
    second = v.State.load(path)
    found = v.run_once(loki, second, CFG, now_ns=T0 + 800 * NS)

    assert [(problem.kind, problem.seq) for problem in found] == [("after_shutdown", 3)]
    head = second.heads[chain.instance]
    assert (head.seq, head.ended) == (2, True)


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
    bound = ChainHead(
        service="docint", instance="a" * 16, seq=3, hash="0" * 64, ts="2026-03-01T00:00:00Z", closed=False
    )
    state.heads = {
        bound.instance: bound,
        "b" * 16: dataclasses.replace(bound, instance="b" * 16),  # open, start not met
        "c" * 16: dataclasses.replace(bound, instance="c" * 16, closed=True, ended=True),
    }
    state.sources = {bound.instance: ("docint", "docint-docint-backend-1")}
    text = v.render_metrics(state, 300.0)

    for name, kind in [
        ("audit_events_verified_total", "counter"),
        ("audit_chain_problems_total", "counter"),
        ("audit_chains_open", "gauge"),
        ("audit_chains_unbound", "gauge"),
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
    assert "audit_chains_open 2\n" in text
    assert "audit_chains_unbound 1\n" in text
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
    chain.close()
    put(loki, chain.take(), T0 + NS, NS, labels("docint"))
    state = v.State(cursor_ns=T0, events_verified=3, unchained={"edge-plane": 2})
    entries = list(v.fetch(loki, v.CHAINED, T0, T0 + 60 * NS))
    v.check_window(state, entries, silence_ns=CFG.silence_ns, cap_ns=T0 + 60 * NS)

    restored = v.State.from_json(json.loads(json.dumps(state.to_json())))

    assert restored == state
    assert restored.sources == {chain.instance: ("docint", "docint-docint-backend-1")}
    assert restored.heads[chain.instance].ended
    without_bindings = {key: value for key, value in state.to_json().items() if key != "sources"}
    assert v.State.from_json(without_bindings).sources == {}  # nothing is bound on load
    older = json.loads(json.dumps(state.to_json()))
    for head in older["heads"].values():
        del head["ended"]  # written before heads carried it
    assert not v.State.from_json(older).heads[chain.instance].ended


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


def test_audit_emitters_parse_strictly() -> None:
    """Pairs are project/service, any case; blank is the federation list, * is no list, typos stop."""
    assert v.parse_emitters(None) == v.DEFAULT_EMITTERS
    assert v.parse_emitters("  ") == v.DEFAULT_EMITTERS
    assert v.parse_emitters("*") is None
    assert v.parse_emitters(" Chorus/Backend , docint/backend,") == {("chorus", "backend"), ("docint", "backend")}
    for bad in ("chorus", "chorus/", "/backend", "chorus/backend/x", ",", " , "):
        with pytest.raises(ValueError, match="AUDIT_EMITTERS"):
            v.parse_emitters(bad)
    assert v.Config.from_env({}).emitters == v.DEFAULT_EMITTERS
    assert v.Config.from_env({"AUDIT_EMITTERS": "*"}).emitters is None
    assert ("edge-plane", "auth-audit") in v.DEFAULT_EMITTERS
    assert ("data-plane", "admin-audit") in v.DEFAULT_EMITTERS
    assert all(project == project.lower() and service == service.lower() for project, service in v.DEFAULT_EMITTERS)


def test_lines_from_an_unlisted_container_stay_out_of_the_chains() -> None:
    """With an emitter list, a sibling container's chain in the project's name is unlisted_source."""
    loki = FakeLoki()
    cfg = dataclasses.replace(CFG, emitters=v.parse_emitters("chorus/backend"))
    genuine = Chain("chorus")
    genuine.query(2)
    genuine.close()
    put(loki, genuine.take(), T0 + NS, NS, labels("chorus", "backend"))
    other_case = Chain("chorus")  # listed, only the label case differs
    other_case.close()
    put(loki, other_case.take(), T0 + 10 * NS, NS, labels("Chorus", "BACKEND", "chorus-backend-2"))
    impostor = Chain("chorus")  # complete and consistent, printed by the project's frontend
    impostor.query(1)
    impostor.close()
    impostor_lines = impostor.take()
    put(loki, impostor_lines, T0 + 20 * NS, NS, labels("chorus", "frontend"))
    unlabelled = {name: value for name, value in labels("chorus", "backend").items() if name != "compose_service"}
    stray = Chain("chorus")
    stray.close()
    stray_lines = stray.take()
    put(loki, stray_lines, T0 + 40 * NS, NS, unlabelled)

    result = v.check_range(loki, cfg, T0, T0 + 3600 * NS)

    unlisted = [problem for problem in result.problems if problem.kind == "unlisted_source"]
    assert len(unlisted) == len(impostor_lines) + len(stray_lines)
    assert (unlisted[0].service, unlisted[0].instance, unlisted[0].detail) == (
        "chorus",
        impostor.instance,
        "sent from chorus/frontend, not a listed emitter",
    )
    assert unlisted[-1].detail == "sent from chorus/?, not a listed emitter"
    assert [problem for problem in result.problems if problem.kind != "unlisted_source"] == []
    assert set(result.heads) == {genuine.instance, other_case.instance}

    # The scheduled run applies the same list, and counts the kind for the alert.
    state = v.State()
    found = v.run_once(loki, state, cfg, now_ns=T0 + 100 * NS)
    assert {problem.kind for problem in found} == {"unlisted_source"}
    assert state.problems["unlisted_source"] == len(unlisted)

    # Without a list (``*``), the same lines are checked like any other.
    assert [p.kind for p in v.check_range(loki, CFG, T0, T0 + 3600 * NS).problems] == []
