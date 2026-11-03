"""Verify, export and anchor the audit lane held in loki-audit.

Subcommands (operator guide: docs/audit.md):

``serve``
    Every ``AUDIT_VERIFY_INTERVAL`` seconds, read the hash-chained lines that
    arrived since the last run and check them with
    :func:`audit_event.verify_chain`, continuing from the chain heads kept in
    ``AUDIT_STATE_DIR``. Results are Prometheus metrics on port 9105. The first
    run without state reads the whole retention window.
``verify --from --to``
    Check one time range and print a report. Exit 1 on any problem.
``export --from --to [--service] [--ref]``
    Write every audit-lane line in the range, chained or not, to the export
    directory as JSONL, with a ``.sha256`` next to it.
``anchor --day``
    Write ``anchor-<day>.json``: line counts per compose project, the SHA-256
    of that day's full export, and the chain heads at the end of the day.

A hash chain proves that lines were not altered, not who wrote them, so
every chained event is first bound to where it came from: its ``service``
must equal, ignoring case, the ``compose_project`` label Alloy took from the
Docker API, which a container cannot set for itself. An event that does not,
or that arrived without the label, is a ``foreign_source`` problem and is
kept out of the chains. Problems are reported by kind, service, instance and
sequence number, never with event content.

Stdlib only; Python 3.12+. Reads loki-audit through its read-only proxy
(loki-audit-read) and never writes to it.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, override

from audit_event import ChainHead, ChainProblem, canonical_json, verify_chain

NS = 1_000_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Substring every hash-chained line carries (canonical JSON is compact).
CHAIN_MARKER = '"stream":"audit"'
LANE_LABELS = (("lane", "audit"),)
PROBLEM_KINDS = (
    "schema",
    "bad_hash",
    "broken_link",
    "seq_gap",
    "seq_repeat",
    "no_start",
    "service_mismatch",
    "unterminated",
    "foreign_source",
)
METRICS_PORT = 9105
#: Lines per query (Loki's default ``max_entries_limit_per_query``), and the
#: most lines checked at once, which bounds memory however busy a day was.
PAGE_LIMIT = 5000
#: Long ranges are read in day slices (Loki's ``max_query_length`` is 30 days).
CHUNK_NS = 86400 * NS
#: Each run re-reads this much before its cursor, so lines that reached the
#: store late are still checked. Lines already checked are skipped by event id.
OVERLAP_NS = 900 * NS
#: Runs stop this far behind the clock, so a batch still in flight is not cut.
LAG_NS = 15 * NS
#: Closed chains are forgotten after this; Loki rejects older lines anyway.
HEAD_KEEP_NS = 7 * 86400 * NS
STATE_VERSION = 1
ANCHOR_VERSION = 1

_DURATION = re.compile(r"(\d+)(ms|y|w|d|h|m|s)")
_UNIT_NS = {
    "ms": 1_000_000,
    "s": NS,
    "m": 60 * NS,
    "h": 3600 * NS,
    "d": 86400 * NS,
    "w": 7 * 86400 * NS,
    "y": 365 * 86400 * NS,
}
_SAFE = re.compile(r"[^A-Za-z0-9._:/-]")


class LokiError(RuntimeError):
    """Loki could not answer a query. The message never contains the query."""


def parse_duration(text: str) -> int:
    """Parse a Loki/Prometheus duration such as ``8760h`` or ``1y`` into nanoseconds.

    Args:
        text: The duration, a sequence of ``<integer><unit>`` with units
            ``ms``, ``s``, ``m``, ``h``, ``d``, ``w`` or ``y``.

    Returns:
        The duration in nanoseconds.

    Raises:
        ValueError: If the text is not a positive duration.
    """
    text = text.strip()
    total = 0
    pos = 0
    for match in _DURATION.finditer(text):
        if match.start() != pos:
            break
        total += int(match.group(1)) * _UNIT_NS[match.group(2)]
        pos = match.end()
    if pos != len(text) or total <= 0:
        raise ValueError(f"not a duration: {text!r}")
    return total


def parse_time(text: str, option: str) -> int:
    """Parse an ISO 8601 date or timestamp into nanoseconds since the epoch.

    Args:
        text: A date (``2026-01-31``, midnight UTC) or a timestamp; one
            without a zone is taken as UTC.
        option: The option name, for the error message.

    Returns:
        Nanoseconds since the Unix epoch.

    Raises:
        ValueError: If the text is not an ISO 8601 date or timestamp.
    """
    try:
        if len(text) == 10:
            moment = datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=UTC)
        else:
            moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{option} is not an ISO 8601 date or timestamp") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return ((moment - EPOCH) // timedelta(microseconds=1)) * 1000


def format_ns(ts_ns: int) -> str:
    """Format nanoseconds since the epoch as RFC 3339 UTC with nine fraction digits.

    Args:
        ts_ns: Nanoseconds since the Unix epoch.

    Returns:
        A timestamp such as ``2026-01-31T12:00:00.000000001Z``.
    """
    seconds, fraction = divmod(ts_ns, NS)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:09d}Z"


def _whole(ts_ns: int, compact: bool = False) -> str:
    """Format a time to the second, for file names and report headers.

    Args:
        ts_ns: Nanoseconds since the Unix epoch.
        compact: File-name form (``20260131T120000Z``) instead of ISO 8601.

    Returns:
        The formatted time.
    """
    moment = datetime.fromtimestamp(ts_ns // NS, UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ" if compact else "%Y-%m-%dT%H:%M:%SZ")


def safe(value: object, limit: int = 64) -> str:
    """Reduce a value from an event to a short token that is safe to log.

    Args:
        value: Typically a service name or chain id; anything else is reduced.
        limit: Maximum length kept.

    Returns:
        The value with every character outside ``[A-Za-z0-9._:/-]`` replaced by
        ``_`` and cut to ``limit`` characters, or ``?`` when empty.
    """
    text = _SAFE.sub("_", str(value))[:limit]
    return text or "?"


def _label(value: object) -> str:
    """Like :func:`safe` for strings; anything else becomes ``?``.

    Args:
        value: A field taken from an untrusted event.

    Returns:
        The sanitised string, or ``?``.
    """
    return safe(value) if isinstance(value, str) else "?"


def _logql_string(text: str) -> str:
    """Quote a value as a LogQL double-quoted string.

    Args:
        text: The raw value.

    Returns:
        The quoted value; JSON string escaping is valid LogQL (Go) escaping.
    """
    return json.dumps(text, ensure_ascii=False)


@dataclass(frozen=True)
class LogQuery:
    """A LogQL log query: label equality matchers plus literal line filters.

    Attributes:
        labels: ``(name, value)`` equality matchers of the stream selector.
        contains: Literals every line must contain (``|=``).
        excludes: Literals no line may contain (``!=``).
    """

    labels: tuple[tuple[str, str], ...] = LANE_LABELS
    contains: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()

    def logql(self) -> str:
        """Render the query as LogQL.

        Returns:
            The query text.
        """
        selector = ", ".join(f"{name}={_logql_string(value)}" for name, value in self.labels)
        parts = ["{" + selector + "}"]
        parts += [f"|= {_logql_string(text)}" for text in self.contains]
        parts += [f"!= {_logql_string(text)}" for text in self.excludes]
        return " ".join(parts)

    def matches(self, labels: Mapping[str, str], line: str) -> bool:
        """Evaluate the query against one line, as Loki would.

        Args:
            labels: The line's stream labels.
            line: The line.

        Returns:
            Whether the line is selected.
        """
        return (
            all(labels.get(name, "") == value for name, value in self.labels)
            and all(text in line for text in self.contains)
            and not any(text in line for text in self.excludes)
        )


CHAINED = LogQuery(contains=(CHAIN_MARKER,))
UNCHAINED = LogQuery(excludes=(CHAIN_MARKER,))


@dataclass(frozen=True)
class Entry:
    """One stored line.

    Attributes:
        ts_ns: The store timestamp, in nanoseconds since the epoch.
        labels: The stream labels as sorted ``(name, value)`` pairs.
        line: The line exactly as stored.
    """

    ts_ns: int
    labels: tuple[tuple[str, str], ...]
    line: str

    def key(self) -> tuple[int, str, str]:
        """Return the total order used for every listing: time, labels, line.

        Returns:
            The sort key, which also identifies the entry.
        """
        return (self.ts_ns, canonical_json(dict(self.labels)), self.line)

    def record(self) -> str:
        """Return the entry as one export line (canonical JSON, no newline).

        Returns:
            ``{"labels": ..., "line": ..., "ts": ...}`` in canonical JSON.
        """
        return canonical_json({"labels": dict(self.labels), "line": self.line, "ts": format_ns(self.ts_ns)})


class LokiReader(Protocol):
    """The two queries the verifier needs from Loki."""

    def query_range(self, query: LogQuery, start_ns: int, end_ns: int, limit: int) -> list[Entry]:
        """Return the earliest ``limit`` lines in ``[start_ns, end_ns)``, oldest first."""
        ...

    def count_by(self, query: LogQuery, label: str, start_ns: int, end_ns: int) -> dict[str, int]:
        """Count the lines in the range by the value of one label."""
        ...


class HttpLoki:
    """Reads Loki over its HTTP API."""

    def __init__(self, base_url: str, timeout_s: float = 60.0) -> None:
        """Remember where Loki is.

        Args:
            base_url: Loki's base URL, e.g. ``http://loki-audit-read:3100``.
            timeout_s: Timeout for each request.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        """Run one GET request and return the ``data`` member of the answer.

        Args:
            path: The API path.
            params: Query parameters.

        Returns:
            The decoded ``data`` object.

        Raises:
            LokiError: On any failure; the message carries no query text.
        """
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            raise LokiError(f"loki answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LokiError(f"loki unreachable or unreadable ({type(exc).__name__})") from None
        if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(body.get("data"), dict):
            raise LokiError("loki returned an unexpected answer")
        return body["data"]

    def query_range(self, query: LogQuery, start_ns: int, end_ns: int, limit: int) -> list[Entry]:
        """Return the earliest ``limit`` lines in ``[start_ns, end_ns)``, oldest first.

        Args:
            query: The log query.
            start_ns: Range start, inclusive.
            end_ns: Range end, exclusive.
            limit: Maximum number of lines.

        Returns:
            The lines, sorted by :meth:`Entry.key`.

        Raises:
            LokiError: If the query fails.
        """
        data = self._get(
            "/loki/api/v1/query_range",
            {
                "query": query.logql(),
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": str(limit),
                "direction": "forward",
            },
        )
        if data.get("resultType") != "streams":
            raise LokiError("loki returned an unexpected result type")
        entries: list[Entry] = []
        for stream in data.get("result", []):
            labels = tuple(sorted((str(k), str(v)) for k, v in stream.get("stream", {}).items()))
            for value in stream.get("values", []):
                entries.append(Entry(int(value[0]), labels, str(value[1])))
        entries.sort(key=Entry.key)
        return entries

    def count_by(self, query: LogQuery, label: str, start_ns: int, end_ns: int) -> dict[str, int]:
        """Count the lines in ``(start_ns, end_ns]`` by the value of one label.

        Args:
            query: The log query.
            label: The label to group by.
            start_ns: Range start.
            end_ns: Range end.

        Returns:
            Line counts by label value.

        Raises:
            LokiError: If the query fails.
        """
        range_ms = max(1, (end_ns - start_ns) // 1_000_000)
        expr = f"sum by ({label}) (count_over_time({query.logql()} [{range_ms}ms]))"
        data = self._get("/loki/api/v1/query", {"query": expr, "time": str(end_ns)})
        counts: dict[str, int] = {}
        for item in data.get("result", []):
            key = str(item.get("metric", {}).get(label, ""))
            counts[key] = counts.get(key, 0) + int(float(item["value"][1]))
        return counts


def chunks(start_ns: int, end_ns: int, size_ns: int) -> Iterator[tuple[int, int]]:
    """Split ``[start_ns, end_ns)`` into consecutive slices.

    Args:
        start_ns: Range start.
        end_ns: Range end.
        size_ns: Slice length.

    Yields:
        ``(slice_start, slice_end)`` pairs covering the range.
    """
    cursor = start_ns
    while cursor < end_ns:
        nxt = min(cursor + size_ns, end_ns)
        yield cursor, nxt
        cursor = nxt


def batched(entries: Iterable[Entry], size: int) -> Iterator[list[Entry]]:
    """Group time-ordered lines into batches of about ``size``, keeping their order.

    A batch is never cut between lines with the same timestamp: two events of
    one chain stamped alike are ordered by their line text, not their ``seq``,
    and only checking them together puts them back in ``seq`` order.

    Args:
        entries: The lines, oldest first.
        size: The batch size to aim for.

    Yields:
        Consecutive batches.
    """
    batch: list[Entry] = []
    for entry in entries:
        if len(batch) >= size and entry.ts_ns != batch[-1].ts_ns:
            yield batch
            batch = []
        batch.append(entry)
    if batch:
        yield batch


def fetch(
    loki: LokiReader,
    query: LogQuery,
    start_ns: int,
    end_ns: int,
    *,
    limit: int = PAGE_LIMIT,
    chunk_ns: int = CHUNK_NS,
) -> Iterator[Entry]:
    """Yield every line in ``[start_ns, end_ns)`` once, in :meth:`Entry.key` order.

    Pages are requested forward by timestamp. A page ends part-way through
    its last timestamp, so the next page starts at that timestamp again; lines
    of that timestamp are held back, merged with the next page and deduplicated,
    which keeps the order independent of where pages happen to split.

    Args:
        loki: The store.
        query: The log query.
        start_ns: Range start, inclusive.
        end_ns: Range end, exclusive.
        limit: Lines per page.
        chunk_ns: Length of the slices long ranges are read in.

    Yields:
        The lines, oldest first.
    """
    for chunk_start, chunk_end in chunks(start_ns, end_ns, chunk_ns):
        page_start = chunk_start
        held: dict[tuple[int, str, str], Entry] = {}
        while True:
            page = loki.query_range(query, page_start, chunk_end, limit)
            known = len(held)
            for entry in page:
                held.setdefault(entry.key(), entry)
            if len(page) < limit:
                yield from sorted(held.values(), key=Entry.key)
                break
            last_ts = max(entry.ts_ns for entry in page)
            if last_ts == page_start and len(held) == known:
                # A full page of one timestamp with nothing new: more than
                # `limit` lines share this nanosecond. Move past it.
                yield from sorted(held.values(), key=Entry.key)
                held = {}
                page_start += 1
                continue
            yield from sorted((e for e in held.values() if e.ts_ns < last_ts), key=Entry.key)
            held = {k: e for k, e in held.items() if e.ts_ns == last_ts}
            page_start = last_ts


@dataclass
class State:
    """What the verifier carries from one run to the next.

    Attributes:
        cursor_ns: End of the last checked range; ``None`` before the first run.
        last_success: Unix time of the last complete run.
        events_verified: Chained events checked so far.
        errors: Failed runs so far.
        problems: Problems found so far, by kind.
        unchained: Unchained audit-lane lines seen so far, by compose project.
        heads: The last verified position of each chain.
        seen: ``event_id -> (line digest, store time)`` of recently checked
            events, so re-read lines are skipped.
    """

    cursor_ns: int | None = None
    last_success: float = 0.0
    events_verified: int = 0
    errors: int = 0
    problems: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PROBLEM_KINDS, 0))
    unchained: dict[str, int] = field(default_factory=dict)
    heads: dict[str, ChainHead] = field(default_factory=dict)
    seen: dict[str, tuple[str, int]] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        """Return the JSON form.

        Returns:
            The state as a JSON-compatible mapping.
        """
        return {
            "version": STATE_VERSION,
            "cursor_ns": self.cursor_ns,
            "last_success": self.last_success,
            "events_verified": self.events_verified,
            "errors": self.errors,
            "problems": self.problems,
            "unchained": self.unchained,
            "heads": {instance: dataclasses.asdict(head) for instance, head in self.heads.items()},
            "seen": {event_id: list(value) for event_id, value in self.seen.items()},
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> State:
        """Rebuild a state from its JSON form.

        Args:
            data: What :meth:`to_json` returned.

        Returns:
            The state.

        Raises:
            ValueError: If the data is from another state version.
        """
        if data.get("version") != STATE_VERSION:
            raise ValueError("unknown verifier state version")
        problems = dict.fromkeys(PROBLEM_KINDS, 0)
        problems.update({str(k): int(v) for k, v in data.get("problems", {}).items()})
        return cls(
            cursor_ns=None if data.get("cursor_ns") is None else int(data["cursor_ns"]),
            last_success=float(data.get("last_success", 0.0)),
            events_verified=int(data.get("events_verified", 0)),
            errors=int(data.get("errors", 0)),
            problems=problems,
            unchained={str(k): int(v) for k, v in data.get("unchained", {}).items()},
            heads={instance: ChainHead(**head) for instance, head in data.get("heads", {}).items()},
            seen={str(k): (str(v[0]), int(v[1])) for k, v in data.get("seen", {}).items()},
        )

    @classmethod
    def load(cls, path: Path) -> State:
        """Read the state file, or start fresh when there is none.

        Args:
            path: The state file.

        Returns:
            The stored state, or an empty one.
        """
        if not path.exists():
            return cls()
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        """Write the state file atomically.

        Args:
            path: The state file.
        """
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def _time_ns(value: object) -> int | None:
    """Read an event timestamp (ISO 8601 with a zone) as nanoseconds.

    Args:
        value: The ``ts`` of an event or chain head, of any type.

    Returns:
        Nanoseconds since the epoch, or ``None`` when it is not a timestamp.
    """
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return ((moment - EPOCH) // timedelta(microseconds=1)) * 1000


def check_window(
    state: State,
    entries: Sequence[Entry],
    *,
    silence_ns: int,
    cap_ns: int,
    window_start_ns: int | None = None,
) -> tuple[int, list[ChainProblem]]:
    """Check one window of chained lines and advance the chain heads.

    Every line is first bound to its source: an event whose ``service`` is
    not, ignoring case, the line's ``compose_project`` label, or a line
    without that label, is a ``foreign_source`` problem and stays out of the
    chains — a container could otherwise print a complete fake chain in
    another service's name.

    Lines whose ``event_id`` was already checked with identical content are
    skipped (re-read pages, a line ingested twice); a different line under a
    known id is checked again and so surfaces as a problem.

    Open chains that went silent are left to :func:`audit_event.verify_chain`
    (``now``/``silence``), with ``now`` set to the newest event seen rather
    than the clock: an outage of the collector or the store silences every
    chain at once and so reports none, while one chain stopping as others go
    on is reported once. ``now`` never passes ``cap_ns``, so a line dated in
    the future cannot make every other chain look silent.

    Args:
        state: Heads and recently seen ids; updated in place.
        entries: The window's chained lines, oldest first.
        silence_ns: How long an open chain may stay silent before it is
            reported ``unterminated``; also the margin within which a chain
            counts as already running when ``window_start_ns`` opens.
        cap_ns: The latest time events can have: the clock, or the end of
            the range being checked.
        window_start_ns: Start of a range being read from scratch. A chain
            without a head whose first line lies within ``silence_ns`` of it
            is taken up mid-chain instead of being reported as ``no_start``.

    Returns:
        The number of events checked and the problems found.
    """
    problems: list[ChainProblem] = []
    events: list[tuple[Entry, dict[str, Any]]] = []
    malformed: list[object] = []
    for entry in entries:
        project = dict(entry.labels).get("compose_project", "")
        try:
            event: object = json.loads(entry.line)
        except ValueError:
            problems.append(ChainProblem(safe(project), "?", None, "schema", "line is not JSON"))
            continue
        fields: dict[str, Any] = event if isinstance(event, dict) else {}
        service = fields.get("service")
        foreign = not project or (isinstance(service, str) and service.lower() != project.lower())
        event_id = fields.get("event_id")
        if isinstance(event_id, str):
            key = f"{event_id}\x00{project}" if foreign else event_id
            digest = hashlib.sha256(entry.line.encode("utf-8")).hexdigest()[:32]
            prior = state.seen.get(key)
            if prior is not None and prior[0] == digest:
                continue
            if prior is None:
                state.seen[key] = (digest, entry.ts_ns)
        if foreign:
            seq = fields.get("seq")
            detail = f"claims service {_label(service)}, sent from project {_label(project or None)}"
            problems.append(
                ChainProblem(
                    service=safe(project),
                    instance=_label(fields.get("instance")),
                    seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
                    kind="foreign_source",
                    detail=detail,
                )
            )
            continue
        if isinstance(event, dict):
            events.append((entry, event))
        else:
            malformed.append(event)

    checkpoints = dict(state.heads)
    if window_start_ns is not None:
        firsts: dict[str, tuple[Entry, dict[str, Any]]] = {}
        for entry, event in events:
            instance, seq = event.get("instance"), event.get("seq")
            if not isinstance(instance, str) or instance in checkpoints:
                continue
            if not isinstance(seq, int) or isinstance(seq, bool):
                continue
            current = firsts.get(instance)
            if current is None or seq < current[1]["seq"]:
                firsts[instance] = (entry, event)
        for instance, (entry, event) in firsts.items():
            prev_hash = event.get("prev_hash")
            if event["seq"] > 0 and isinstance(prev_hash, str) and entry.ts_ns - window_start_ns <= silence_ns:
                checkpoints[instance] = ChainHead(
                    service=str(event.get("service")),
                    instance=instance,
                    seq=event["seq"] - 1,
                    hash=prev_hash,
                    ts=str(event.get("ts", "")),
                    closed=False,
                )

    times = [_time_ns(event.get("ts")) for _, event in events]
    times += [_time_ns(head.ts) for head in checkpoints.values()]
    known_times = [moment for moment in times if moment is not None]
    now = None
    if known_times:
        now = EPOCH + timedelta(microseconds=min(max(known_times), cap_ns) // 1000)
    report = verify_chain(
        [event for _, event in events] + malformed,
        checkpoints,
        now=now,
        silence=timedelta(microseconds=silence_ns // 1000),
    )
    problems.extend(report.problems)
    state.heads = dict(report.heads)
    return report.events, problems


def prune(state: State, *, seen_before_ns: int, heads_before_ns: int) -> None:
    """Forget ids that can no longer be re-read and chains closed long ago.

    Args:
        state: Updated in place.
        seen_before_ns: Drop remembered event ids stored before this time.
        heads_before_ns: Drop closed chains whose last event is older.
    """
    state.seen = {event_id: value for event_id, value in state.seen.items() if value[1] >= seen_before_ns}
    for instance, head in list(state.heads.items()):
        if head.closed and (_time_ns(head.ts) or 0) < heads_before_ns:
            del state.heads[instance]


@dataclass(frozen=True)
class Config:
    """Verifier settings.

    Attributes:
        interval_s: Seconds between runs (``AUDIT_VERIFY_INTERVAL``).
        silence_ns: Quiet time after which an open chain counts as ended
            (``AUDIT_SILENCE_S``).
        retention_ns: How far back the store keeps lines (``AUDIT_RETENTION``).
        page_limit: Lines per query.
        chunk_ns: Slice length for long ranges.
        overlap_ns: How much each run re-reads before its cursor.
        lag_ns: How far behind the clock a run stops.
    """

    interval_s: float = 300.0
    silence_ns: int = 300 * NS
    retention_ns: int = 8760 * 3600 * NS
    page_limit: int = PAGE_LIMIT
    chunk_ns: int = CHUNK_NS
    overlap_ns: int = OVERLAP_NS
    lag_ns: int = LAG_NS

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        """Read the settings from the environment.

        Args:
            env: The environment.

        Returns:
            The settings.

        Raises:
            ValueError: If a value is not usable.
        """
        interval = float(env.get("AUDIT_VERIFY_INTERVAL", "300"))
        silence = float(env.get("AUDIT_SILENCE_S", "300"))
        if interval <= 0 or silence <= 0:
            raise ValueError("AUDIT_VERIFY_INTERVAL and AUDIT_SILENCE_S must be positive")
        return cls(
            interval_s=interval,
            silence_ns=int(silence * NS),
            retention_ns=parse_duration(env.get("AUDIT_RETENTION", "8760h")),
        )


def run_once(
    loki: LokiReader,
    state: State,
    cfg: Config,
    *,
    now_ns: int,
    lock: AbstractContextManager[Any] | None = None,
    persist: Callable[[], None] | None = None,
    report: Callable[[ChainProblem], None] | None = None,
) -> list[ChainProblem]:
    """Check everything that arrived since the last run.

    Reads from the cursor (minus the overlap) up to ``now_ns`` in slices and
    saves progress after each slice. The first run starts at the retention
    boundary.

    Args:
        loki: The store.
        state: Carried between runs; updated in place.
        cfg: Settings.
        now_ns: The current time.
        lock: Held while ``state`` changes, so metrics never see it half-updated.
        persist: Called after each slice and at the end, to save ``state``.
        report: Called for each problem as soon as it is found.

    Returns:
        Every problem found in this run.

    Raises:
        LokiError: If a query fails; progress up to the failed slice is kept.
    """
    guard = lock if lock is not None else nullcontext()
    found: list[ChainProblem] = []

    def record(problems: list[ChainProblem]) -> None:
        for problem in problems:
            state.problems[problem.kind] = state.problems.get(problem.kind, 0) + 1
            found.append(problem)
            if report is not None:
                report(problem)

    end_ns = now_ns - cfg.lag_ns
    floor_ns = end_ns - cfg.retention_ns
    first_pass = state.cursor_ns is None
    count_from = floor_ns if state.cursor_ns is None else max(state.cursor_ns, floor_ns)
    start_ns = floor_ns if state.cursor_ns is None else max(state.cursor_ns - cfg.overlap_ns, floor_ns)

    for chunk_start, chunk_end in chunks(start_ns, end_ns, cfg.chunk_ns):
        chained = fetch(loki, CHAINED, chunk_start, chunk_end, limit=cfg.page_limit, chunk_ns=cfg.chunk_ns)
        for batch in batched(chained, cfg.page_limit):
            with guard:
                checked, problems = check_window(
                    state,
                    batch,
                    silence_ns=cfg.silence_ns,
                    cap_ns=now_ns,
                    window_start_ns=floor_ns if first_pass else None,
                )
                state.events_verified += checked
                record(problems)
        count_start = max(chunk_start, count_from)
        unchained: dict[str, int] = {}
        if count_start < chunk_end:
            unchained = loki.count_by(UNCHAINED, "compose_project", count_start, chunk_end)
        with guard:
            for project, count in unchained.items():
                key = project or "?"
                state.unchained[key] = state.unchained.get(key, 0) + count
            state.cursor_ns = max(state.cursor_ns or chunk_end, chunk_end)
            prune(state, seen_before_ns=chunk_end - cfg.overlap_ns, heads_before_ns=now_ns - HEAD_KEEP_NS)
        if persist is not None:
            persist()
    return found


def render_metrics(state: State, interval_s: float) -> str:
    """Render the verifier's metrics in the Prometheus text format.

    Args:
        state: The current state.
        interval_s: The configured run interval.

    Returns:
        The exposition text.
    """

    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    families: list[tuple[str, str, str, list[tuple[str, float]]]] = [
        ("audit_events_verified_total", "counter", "Hash-chained audit events checked.", [("", state.events_verified)]),
        (
            "audit_chain_problems_total",
            "counter",
            "Problems found in the audit chains, by kind.",
            [(f'{{kind="{escape(kind)}"}}', count) for kind, count in sorted(state.problems.items())],
        ),
        (
            "audit_chains_open",
            "gauge",
            "Chains that have not ended with a shutdown event.",
            [("", sum(1 for head in state.heads.values() if not head.closed))],
        ),
        (
            "audit_unchained_lines_total",
            "counter",
            "Audit-lane lines without a hash chain, by compose project.",
            [(f'{{project="{escape(project)}"}}', count) for project, count in sorted(state.unchained.items())],
        ),
        (
            "audit_verifier_last_success_timestamp_seconds",
            "gauge",
            "Unix time of the last complete verification run.",
            [("", state.last_success)],
        ),
        ("audit_verifier_errors_total", "counter", "Verification runs that failed.", [("", state.errors)]),
        ("audit_verifier_interval_seconds", "gauge", "Configured time between verification runs.", [("", interval_s)]),
    ]
    lines: list[str] = []
    for name, kind, help_text, samples in families:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{labels} {value}" for labels, value in samples)
    return "\n".join(lines) + "\n"


def start_metrics_server(render: Callable[[], str], port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Serve ``GET /metrics`` in a background thread.

    Args:
        render: Produces the exposition text.
        port: TCP port; ``0`` picks a free one.
        host: Address to bind.

    Returns:
        The running server; call ``shutdown()`` to stop it.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            body = render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    return server


def _log(message: str) -> None:
    """Write one log line to stdout.

    Args:
        message: A logfmt line.
    """
    print(message, flush=True)


def _problem_line(problem: ChainProblem) -> str:
    """Format a problem for the log: kind, service, instance, seq only.

    Args:
        problem: The problem.

    Returns:
        A logfmt line without event content.
    """
    seq = "" if problem.seq is None else problem.seq
    return (
        f'level=warn msg="audit chain problem" kind={safe(problem.kind)} '
        f"service={safe(problem.service)} instance={safe(problem.instance)} seq={seq}"
    )


def serve(loki: LokiReader, cfg: Config, state_path: Path, port: int = METRICS_PORT) -> int:
    """Run the verification loop until SIGTERM or SIGINT.

    Args:
        loki: The store.
        cfg: Settings.
        state_path: Where the state is kept between runs.
        port: Metrics port.

    Returns:
        The exit code.
    """
    state = State.load(state_path)
    lock = threading.Lock()
    stop = threading.Event()

    def render() -> str:
        with lock:
            return render_metrics(state, cfg.interval_s)

    def persist() -> None:
        state.save(state_path)

    server = start_metrics_server(render, port)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    _log(
        f'level=info msg="audit verifier started" interval_s={cfg.interval_s:g} '
        f"silence_s={cfg.silence_ns / NS:g} first_pass={str(state.cursor_ns is None).lower()}"
    )
    while not stop.is_set():
        try:
            problems = run_once(
                loki,
                state,
                cfg,
                now_ns=time.time_ns(),
                lock=lock,
                persist=persist,
                report=lambda problem: _log(_problem_line(problem)),
            )
            with lock:
                state.last_success = time.time()
            persist()
            open_chains = sum(1 for head in state.heads.values() if not head.closed)
            _log(
                f'level=info msg="verification run complete" events_total={state.events_verified} '
                f"chains_open={open_chains} problems={len(problems)}"
            )
        except (LokiError, OSError) as exc:
            with lock:
                state.errors += 1
            reason = str(exc) if isinstance(exc, LokiError) else type(exc).__name__
            _log(f'level=error msg="verification run failed" error="{safe(reason, 120)}"')
        stop.wait(cfg.interval_s)
    server.shutdown()
    _log('level=info msg="audit verifier stopped"')
    return 0


@dataclass
class RangeResult:
    """What a one-shot check of a time range found.

    Attributes:
        events: Chained events checked.
        problems: Every problem found.
        heads: Chain heads at the end of the range; ``closed`` also marks a
            chain reported ``unterminated``.
        unchained: Unchained lines by compose project.
    """

    events: int
    problems: list[ChainProblem]
    heads: dict[str, ChainHead]
    unchained: dict[str, int]


def check_range(loki: LokiReader, cfg: Config, start_ns: int, end_ns: int) -> RangeResult:
    """Check the chained lines of one time range from scratch.

    Chains already running when the range opens are taken up mid-chain.

    Args:
        loki: The store.
        cfg: Settings.
        start_ns: Range start, inclusive.
        end_ns: Range end, exclusive.

    Returns:
        The result.
    """
    state = State()
    events = 0
    problems: list[ChainProblem] = []
    unchained: dict[str, int] = {}
    for chunk_start, chunk_end in chunks(start_ns, end_ns, cfg.chunk_ns):
        chained = fetch(loki, CHAINED, chunk_start, chunk_end, limit=cfg.page_limit, chunk_ns=cfg.chunk_ns)
        for batch in batched(chained, cfg.page_limit):
            checked, found = check_window(
                state, batch, silence_ns=cfg.silence_ns, cap_ns=end_ns, window_start_ns=start_ns
            )
            events += checked
            problems.extend(found)
        for project, count in loki.count_by(UNCHAINED, "compose_project", chunk_start, chunk_end).items():
            unchained[project or "?"] = unchained.get(project or "?", 0) + count
        prune(state, seen_before_ns=chunk_end - cfg.overlap_ns, heads_before_ns=0)
    return RangeResult(events=events, problems=problems, heads=dict(state.heads), unchained=unchained)


def cmd_verify(loki: LokiReader, cfg: Config, start_ns: int, end_ns: int, out: Callable[[str], None] = print) -> int:
    """Check a time range and print a report.

    Args:
        loki: The store.
        cfg: Settings.
        start_ns: Range start, inclusive.
        end_ns: Range end, exclusive.
        out: Where report lines go.

    Returns:
        ``0`` when no problem was found, ``1`` otherwise.
    """
    result = check_range(loki, cfg, start_ns, end_ns)
    open_chains = sum(1 for head in result.heads.values() if not head.closed)
    out(f"audit trail {_whole(start_ns)} .. {_whole(end_ns)}")
    out(f"chained events checked: {result.events} in {len(result.heads)} chains ({open_chains} open at the end)")
    by_project = ", ".join(f"{safe(project)} {count}" for project, count in sorted(result.unchained.items()))
    out(f"unchained lines: {sum(result.unchained.values())}" + (f" ({by_project})" if by_project else ""))
    if not result.problems:
        out("problems: none")
        return 0
    out(f"problems: {len(result.problems)}")
    for problem in result.problems:
        seq = "-" if problem.seq is None else str(problem.seq)
        out(
            f"  {problem.kind:<15} service={safe(problem.service)} instance={safe(problem.instance)} "
            f"seq={seq}  {problem.detail}"
        )
    return 1


def _write_atomic(path: Path, chunks_of_text: Iterator[str]) -> str:
    """Write text to a file atomically and return its SHA-256.

    Args:
        path: The destination.
        chunks_of_text: The content, piece by piece.

    Returns:
        The lowercase hex SHA-256 of the written bytes.
    """
    digest = hashlib.sha256()
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for text in chunks_of_text:
            data = text.encode("utf-8")
            digest.update(data)
            handle.write(text)
    os.replace(tmp, path)
    return digest.hexdigest()


def cmd_export(
    loki: LokiReader,
    cfg: Config,
    out_dir: Path,
    start_ns: int,
    end_ns: int,
    *,
    service: str | None = None,
    ref: str | None = None,
) -> Path:
    """Export every audit-lane line of a range as JSONL, plus its ``.sha256``.

    Args:
        loki: The store.
        cfg: Settings.
        out_dir: The export directory.
        start_ns: Range start, inclusive.
        end_ns: Range end, exclusive.
        service: Only lines from this compose project.
        ref: Only lines containing this literal text.

    Returns:
        The path of the JSONL file.
    """
    labels = LANE_LABELS + ((("compose_project", service),) if service else ())
    query = LogQuery(labels=labels, contains=(ref,) if ref else ())
    name = f"audit-{_whole(start_ns, compact=True)}-{_whole(end_ns, compact=True)}"
    if service:
        name += f"-{safe(service)}"
    if ref:
        name += f"-ref{hashlib.sha256(ref.encode('utf-8')).hexdigest()[:8]}"
    path = out_dir / f"{name}.jsonl"
    count = 0

    def records() -> Iterator[str]:
        nonlocal count
        for entry in fetch(loki, query, start_ns, end_ns, limit=cfg.page_limit, chunk_ns=cfg.chunk_ns):
            count += 1
            yield entry.record() + "\n"

    digest = _write_atomic(path, records())
    (out_dir / f"{path.name}.sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    print(f"wrote {path.name}: {count} lines, sha256 {digest}", file=sys.stderr)
    return path


def cmd_anchor(loki: LokiReader, cfg: Config, out_dir: Path, day: date, *, now_ns: int) -> Path:
    """Write the anchor of one UTC day.

    The anchor's ``sha256`` is that of the day's full export (``export`` over
    the same day without filters), so an export can be checked against it.

    Args:
        loki: The store.
        cfg: Settings.
        out_dir: The export directory.
        day: The UTC day.
        now_ns: The current time.

    Returns:
        The path of the anchor file.

    Raises:
        ValueError: If the day has not ended long enough ago for late lines
            to have arrived.
    """
    start_ns = parse_time(day.isoformat(), "--day")
    end_ns = start_ns + 86400 * NS
    if now_ns < end_ns + cfg.overlap_ns:
        raise ValueError("--day must have ended at least 15 minutes ago")
    digest = hashlib.sha256()
    lines = 0
    by_project: dict[str, int] = {}
    state = State()
    events = 0
    problems: list[ChainProblem] = []
    for chunk_start, chunk_end in chunks(start_ns, end_ns, cfg.chunk_ns):
        every = fetch(loki, LogQuery(), chunk_start, chunk_end, limit=cfg.page_limit, chunk_ns=cfg.chunk_ns)
        for batch in batched(every, cfg.page_limit):
            for entry in batch:
                digest.update((entry.record() + "\n").encode("utf-8"))
                project = dict(entry.labels).get("compose_project", "") or "?"
                by_project[project] = by_project.get(project, 0) + 1
            lines += len(batch)
            chained = [entry for entry in batch if CHAIN_MARKER in entry.line]
            checked, found = check_window(
                state, chained, silence_ns=cfg.silence_ns, cap_ns=end_ns, window_start_ns=start_ns
            )
            events += checked
            problems.extend(found)
        prune(state, seen_before_ns=chunk_end - cfg.overlap_ns, heads_before_ns=0)
    heads = dict(state.heads)
    kinds: dict[str, int] = {}
    for problem in problems:
        kinds[problem.kind] = kinds.get(problem.kind, 0) + 1
    anchor = {
        "anchor_version": ANCHOR_VERSION,
        "day": day.isoformat(),
        "from": _whole(start_ns),
        "to": _whole(end_ns),
        "lines": lines,
        "lines_by_project": dict(sorted(by_project.items())),
        "sha256": digest.hexdigest(),
        "chained_events": events,
        "chains": [dataclasses.asdict(head) for head in sorted(heads.values(), key=lambda h: (h.service, h.instance))],
        "chain_problems": dict(sorted(kinds.items())),
    }
    path = out_dir / f"anchor-{day.isoformat()}.json"
    _write_atomic(path, iter([json.dumps(anchor, indent=2, sort_keys=True, ensure_ascii=False) + "\n"]))
    print(f"wrote {path.name}: {lines} lines, {len(heads)} chains, {sum(kinds.values())} problems", file=sys.stderr)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments without the program name; defaults to ``sys.argv[1:]``.

    Returns:
        The exit code: ``0`` ok, ``1`` problems found, ``2`` usage or store error.
    """
    parser = argparse.ArgumentParser(prog="verifier.py", description="Verify, export and anchor the audit lane.")
    parser.add_argument("--loki", default=os.environ.get("AUDIT_LOKI_URL", "http://loki-audit-read:3100"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="verify continuously and serve metrics")
    verify = commands.add_parser("verify", help="check a time range")
    export = commands.add_parser("export", help="export a time range as JSONL")
    for sub in (verify, export):
        sub.add_argument("--from", dest="start", required=True, help="ISO 8601 date or timestamp (UTC if no zone)")
        sub.add_argument("--to", dest="end", required=True, help="ISO 8601 date or timestamp, exclusive")
    export.add_argument("--service", help="only lines from this compose project")
    export.add_argument("--ref", help="only lines containing this literal text")
    anchor = commands.add_parser("anchor", help="write the anchor of one UTC day")
    anchor.add_argument("--day", required=True, help="YYYY-MM-DD")
    for sub in (export, anchor):
        sub.add_argument("--out", type=Path, default=Path("/exports"), help="export directory")
    args = parser.parse_args(argv)

    try:
        cfg = Config.from_env(os.environ)
        loki = HttpLoki(args.loki)
        if args.command == "serve":
            state_dir = Path(os.environ.get("AUDIT_STATE_DIR", "/state"))
            return serve(loki, cfg, state_dir / "verifier-state.json")
        if args.command == "anchor":
            path = cmd_anchor(loki, cfg, args.out, date.fromisoformat(args.day), now_ns=time.time_ns())
            print(path.name)
            return 0
        start_ns = parse_time(args.start, "--from")
        end_ns = parse_time(args.end, "--to")
        if start_ns >= end_ns:
            raise ValueError("--from must be before --to")
        if args.command == "verify":
            return cmd_verify(loki, cfg, start_ns, end_ns)
        path = cmd_export(loki, cfg, args.out, start_ns, end_ns, service=args.service, ref=args.ref)
        print(path.name)
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LokiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
