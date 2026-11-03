# Audit trail

obs-plane keeps a data-protection audit trail apart from the operational
logs: records of who collected, changed, queried, disclosed, combined or
deleted which data, when and with what stated purpose. It has its own store
(`loki-audit`), its own reader (`grafana-audit`), its own retention, and a
verifier that checks the records have not been altered. The design is deploy
[ADR 0003](../../deploy/docs/decisions/0003-federation-audit-trail.md); the
day-to-day runbook is deploy's
[runbooks/audit.md](../../deploy/docs/runbooks/audit.md). This page is the
reference for the store itself.

## What feeds it

| Producer | Lines | Integrity |
|---|---|---|
| Federation services using the shared audit library (`audit_event.py`, canonical in `nos-tromo/.github`, vendored here as `audit/audit_event.py`) | One JSON line per event, marked `"stream":"audit"`: action, operation, outcome, actor, stated purpose, references and bounded search criteria, never content | Hash chain per process, re-checked by the verifier |
| The edge gateway's access log (`"logger":"http.log.access…"`), accepted only from edge-plane's `caddy` container | One line per request: account (`user`), request id (`uuid`), path without its query string, status | Daily anchors only |
| Open WebUI's audit log (`"auditable": true`), accepted only from open-webui-service's `open-webui` container | One record per authenticated API request, without request or response bodies | Daily anchors only |

Each chain belongs to one process. Its events carry `seq` and the hash of the
previous event, so a removed, inserted, reordered or altered line breaks the
chain. A `start` event opens it, a `heartbeat` follows every 60 s, and a
`shutdown` event closes it cleanly.

## How lines get there

Alloy (`alloy/config.alloy`) reads every container's output once and decides
per line, in one place (`loki.process "lane"`):

- **Audit records** get the label `lane="audit"` and go to `loki-audit`,
  **unredacted**: the search criteria in them are part of the record. A
  chained line (`"stream":"audit"`) counts from any container, because the
  verifier binds each one to its source ([Source binding](#source-binding)).
  The two third-party markers carry no chain, so they count only from their
  own containers: the gateway's from compose project `edge-plane`, service
  `caddy`; Open WebUI's from `open-webui-service`, service `open-webui`.
  The same text printed by any other container is an ordinary line — it
  goes to the operational Loki, redacted — so no container can slip a fake
  gateway or Open WebUI record into the trail.
- **Everything else** goes to the operational Loki, with query-string values
  redacted as before.
- **The audit store's own logs** (`loki-audit`, `loki-audit-read`,
  `grafana-audit`, `grafana-audit-guard`) go to neither: they can name what
  reviewers searched for. They stay in the container logs on the host
  (`make logs S=loki-audit`).

So the gateway's access log is no longer in the operational Loki; the
gateway's other logs are. `scripts/check_alloy_lanes.sh` runs the real config
on fixture lines (`alloy/lanes-test/`) and checks where each one lands; CI
runs it on every change.

## Retention

`AUDIT_RETENTION` in `.env` (a Loki duration, default `8760h`, one year) is
how long the store keeps lines. Set it to the period the applicable retention
rules prescribe before the trail holds data that must be kept. Loki's
compactor deletes older lines on its own; lowering the value later deletes
older lines for good.

Nothing else deletes lines. The store's deletion API is off
(`deletion_mode: disabled` in `loki/loki-audit.yaml`; the endpoint answers
403), and `make nuke` leaves the three audit volumes (`loki-audit-data`,
`grafana-audit-data`, `audit-verifier-state`) alone. Removing them takes a
deliberate `docker volume rm`.

## Access

- **Readers** reach `grafana-audit` at `https://<EDGE_HOST>/audit/`. The
  gateway admits only members of its `audit` group; `admins` does not grant
  it (edge-plane `docs/identity-contract.md`). Grafana signs them in from the
  gateway's `X-Auth-User` header as **Viewers**: they can search with the
  "Audit trail" dashboard and with Explore, and can save nothing. Snapshots,
  public dashboards and the server-side query history are off, so neither
  trail data nor reviewers' searches are kept in Grafana beyond the store's
  retention.
- **Only the gateway gets in.** `grafana-audit` listens on loopback only;
  its guard, `grafana-audit-guard`, answers every request without the
  gateway's `AUDIT_EDGE_SECRET` with 403, so no other container on
  `edge-net` can pose as a reader. There is no local admin, no login form
  and no basic auth: everything is provisioned from files
  ([grafana-access.md](grafana-access.md#the-edge-guards)).
- **Reading is recorded.** Every request to `/audit/` is in the gateway's
  access log, which is in the trail.
- **Readers cannot write.** The store has no authentication of its own, so
  nothing but the collector may reach it. It sits on the internal `audit`
  network with `alloy`, its only writer, and `loki-audit-read`. Readers —
  `grafana-audit` and `audit-verifier`, on the internal `audit-read`
  network — get only that read proxy, which passes `GET`/`HEAD` on Loki's
  read API (queries, labels, series, index stats and volume, detected
  labels and fields, patterns, query formatting, build info, `/ready`) and
  answers 403 to everything else: pushing lines, deleting, flushing, tailing,
  admin endpoints. Loki receives the path the proxy checked — normalized,
  with `..` segments and percent-encoding resolved — never the client's raw
  request line, so no encoded path can be checked as a query and arrive as
  something else. Grafana's datasource proxy therefore cannot be used to
  write either.
- **Operators** see the trail's health — the verifier's metrics and alerts,
  which carry counts and problem kinds only — never its lines. The ops
  `grafana` has no datasource for the store and is on neither of its
  networks.

The dashboard's filters (service, user, action) are plain text matches on the
line: `docint` matches both the service's own events and gateway lines for
`/docint/`. Explore takes any LogQL query against the same store.

## Integrity

### The verifier

`audit-verifier` runs `audit/verifier.py` (stdlib Python in the pinned
`python` image, read-only, as `nobody`). Every `AUDIT_VERIFY_INTERVAL`
seconds (default 300) it reads, through `loki-audit-read`, the chained lines
that arrived since its last run and checks them with the library's own
`verify_chain`, continuing from
the chain heads it keeps in `audit-verifier-state`. On its first start it
reads the whole retention window. Each run also re-reads the 15 minutes
before its cursor, skipping events it has already checked, so lines that
reach the store late are still checked.

### Source binding

A hash chain proves that lines were not altered, not who wrote them. So
before any chain check the verifier binds every line to where it came from,
by the `compose_project` and `container` labels Alloy takes from the Docker
API, which a container cannot set for itself:

- **A new chain is bound to its project.** The event's `service` must equal,
  ignoring case, the line's `compose_project`. An event that does not, or a
  line that arrived without the label, is a `foreign_source` problem —
  otherwise any container could print a complete, internally consistent
  chain in another service's name. An emitter's `service` is therefore its
  compose project name (`chorus`, `docint`, `nextext`, `translator`).
- **Every chain is bound to one container, by its hash links.** A chain
  belongs to the container of the line that verifiably continues it: its
  start event, or the event right after the last one the verifier checked,
  naming that event's hash. Which line happens to arrive first decides
  nothing. The binding is kept with the chain heads in the verifier's
  state, so it survives restarts. A line for a bound chain from any other
  container — even one of the same project, such as an app's frontend next
  to its backend — is a `source_mismatch` problem. Otherwise such a
  container could print lines carrying a known chain's instance id and make
  the legitimate chain look tampered with. A legitimate chain never changes
  container: recreating a container starts a new process, and so a new
  chain.

Until a chain is bound, its lines are checked as usual while they come from
a single container. When lines of an unbound chain checked together come
from several containers, exactly one of them must continue the chain
verifiably: that one is bound, the others are `source_mismatch`. A start
event can be written without knowing anything about the chain, so it counts
only when every other container's lines for that chain hash-link back to
it, as copies or branches of the chain it starts. If no container continues
the chain, or more than one does — a fabricated start next to a chain that
was already running, for instance — the verifier reports `ambiguous_source`,
binds nothing and checks none of those lines, so the chain's next lines are
checked as if they were missing.

`foreign_source` and `source_mismatch` lines are kept out of the chains
entirely; the legitimate chain goes on verifying. Within a chain, the audit
library also sets aside an event that names another service than the chain
(`service_mismatch`).

What source binding cannot cover: whoever has root on the host, or write
access to the audit store itself, can still fabricate lines — with any label
— for the current day, until that day's anchor is signed off the host.
Anchors close that window (below). Nothing else can write to the store:
only the collector reaches it, and readers get the read proxy
([Access](#access)).

### Silent chains

A chain that has written nothing for `AUDIT_SILENCE_S` seconds (default 300,
five heartbeats) without a `shutdown`, while other chains went on, is
reported once as `unterminated` and then counted as closed. A crashed process
therefore yields one finding; an outage of the collector or the store, which
silences every chain at once, yields none.

### Problem kinds

| Kind | Meaning |
|---|---|
| `bad_hash` | A line's content does not match its hash: altered |
| `broken_link` | A line does not name the previous line's hash |
| `seq_gap` | Sequence numbers jump: lines missing |
| `seq_repeat` | A sequence number appears twice with different content |
| `no_start` | A chain appears mid-way without its start |
| `service_mismatch` | An event names another service than the rest of its chain; it is left out of the chain |
| `schema` | A marked line is not a well-formed event |
| `foreign_source` | An event names another service than the compose project it was sent from, or arrived without one; it is not chain-checked |
| `source_mismatch` | A line for a chain bound to another container; it is not chain-checked |
| `ambiguous_source` | Lines of a chain not yet bound came from several containers, and not exactly one of them continued it verifiably; nothing is bound and none of them is chain-checked, so the chain's next lines are checked as if they were missing |
| `unterminated` | A chain stopped without a shutdown event |

Problems go to the verifier's log as kind, service, instance and sequence
number only, never event content.

### Metrics and alerts

The verifier serves Prometheus metrics on `audit-verifier:9105`:
`audit_events_verified_total`, `audit_chain_problems_total{kind}`,
`audit_chains_open`, `audit_unchained_lines_total{project}`,
`audit_verifier_last_success_timestamp_seconds`,
`audit_verifier_errors_total` and `audit_verifier_interval_seconds`. Its
counters are kept in its state, so they survive restarts.

Three rules in `prometheus/rules.yml` (group `audit`) fire on them, without a
notification channel like every other rule here:

| Alert | Fires when |
|---|---|
| `AuditChainProblem` | A problem was found in the last 15 minutes; its `kind` label is one of the [problem kinds](#problem-kinds) |
| `AuditVerifierStale` | No run completed for more than three intervals |
| `AuditVerifierDown` | Prometheus cannot reach the verifier for 5 minutes |

### Anchors

The chains reveal altered and removed lines. Lines added after the fact, and
any change to the unchained producers, are revealed by the daily anchor:
`make audit-anchor DAY=<YYYY-MM-DD>` writes `audit-exports/anchor-<day>.json`
with the number of lines per compose project, the SHA-256 over all of that
day's lines in a fixed order, and every chain's last position at the end of
the day. The digest is exactly the SHA-256 of that day's full export, so a
later `make audit-export FROM=<day> TO=<next day>` can be compared with it.

Sign each anchor with a key that is kept off the host and move both files to
offline storage the same day. A day can be anchored once it has been over
for 15 minutes.

## Commands

```bash
make audit-verify FROM=2026-01-01 TO=2026-02-01      # check the chains of a range; exits 1 on any problem
make audit-export FROM=2026-01-01T00:00:00Z TO=2026-01-02T00:00:00Z
make audit-export FROM=… TO=… SERVICE=docint         # one compose project (edge-plane: the gateway)
make audit-export FROM=… TO=… REF=<text>             # only lines containing <text>
make audit-anchor DAY=2026-01-01                     # the anchor of one UTC day
```

Times are ISO 8601; a date means midnight UTC, and `TO` is exclusive. The
commands run as one-off containers of the `audit-verifier` service and write
to `audit-exports/` (git-ignored), as the invoking user.

An export is JSONL, one record per stored line, ordered by time:
`{"labels": {...}, "line": "<the line as stored>", "ts": "<RFC 3339, ns>"}`,
with a `.sha256` next to it in `sha256sum` format. The `REF` text itself does
not appear in the file name, only a short hash of it. An export contains
personal data: hand it only to whoever is entitled to it.

**Signing.** With `AUDIT_SIGNING_KEY` set to the path of an SSH private key
(set it only while signing), `audit-export` and `audit-anchor` write
`<file>.sig` next to the file (`ssh-keygen -Y sign -n audit`). Without it they
print the command to sign elsewhere. To check a file:

```bash
ssh-keygen -Y verify -f allowed_signers -I <signer identity> -n audit \
  -s anchor-2026-01-01.json.sig < anchor-2026-01-01.json
```

On a host with `userns-remap`, `audit-exports/` must be writable by the
remapped user ID (deploy `docs/runbooks/userns-remap.md`).

## Limits

- **Whoever has root on the host or write access to the store** can still
  fabricate, change or remove lines — chains and labels included — for the
  current day, until that day's anchor is signed and kept off the host.
  Anchors make such changes detectable afterwards; they do not prevent them.
  Take them every day.
- **The gateway and Open WebUI are not chained.** For their lines the anchors
  are the only integrity control.
- **New chains are bound to a project, not a container.** Any container of
  a project can start chains of its own in that project's name; the
  container binding protects chains that already exist. A chain that was
  already running when a range is checked from scratch (`make audit-verify`,
  the verifier's first start) is checked, but bound to no container until a
  later line continues it verifiably — for the running verifier, on its next
  run. Until then, another container writing lines for that chain can keep
  it from being checked (`ambiguous_source`). It can get the chain bound to
  itself instead only with a line that continues the chain's last checked
  event, which takes that event's hash, or with lines checked without any
  from the chain's own container, which show as chain defects first.
- **Late lines.** Loki answers queries about the last three hours from
  memory and older ones from flushed chunks. A line that reaches the store
  more than three hours after it was written (after a long collector outage)
  becomes visible about half an hour later, once flushed. The running
  verifier re-reads only 15 minutes back, so it misses such lines: a chain
  it was following shows a gap, a chain that arrives entirely late is not
  checked at all. `make audit-verify` over that window afterwards checks
  them.
- **The edge secret.** Whoever holds `AUDIT_EDGE_SECRET` — the gateway's
  host and this host's operators — can pose as any reader. No other
  container can.
- **The last chain standing.** A chain is reported `unterminated` only once
  another chain has moved on; if the only emitting process dies, that waits
  until something else writes.
