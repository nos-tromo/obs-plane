# Grafana access

How Grafana is reached in each shape — through the edge gateway on
production, through the dev overlay or an SSH tunnel otherwise — and the
edge guards that make the gateway path safe.

## Through the edge gateway (production)

Grafana's primary browser path is now the edge gateway:
`https://${EDGE_HOST}/grafana/`, routed by `edge-plane`'s Caddy +
Authelia forward-auth. `GF_SERVER_ROOT_URL` / `GF_SERVER_SERVE_FROM_SUB_PATH`
tell Grafana it is served under `/grafana/`; `GF_AUTH_PROXY_*` configure
Grafana's auth.proxy so a request carrying the trusted `X-Auth-User`
header auto-logs-in and auto-provisions that user, at the org role
`GF_USERS_AUTO_ASSIGN_ORG_ROLE` sets (`${GRAFANA_VIEWER_ROLE:-Viewer}`).
All of it is set on the `grafana` service in `docker/compose.yaml`. This
revises the v1 decision (below) that treated the tunnel as the only
path — that path remains, as the admin/fallback route.

`grafana` joins `edge-net` for exactly this reason; the audit-trail viewer
`grafana-audit` (below) is obs-plane's only other service on that seam.
Neither Grafana listens there itself: each listens on `127.0.0.1:3001` only,
and an nginx guard sharing its network namespace answers on `:3000` — see
[the edge guards](#the-edge-guards). The gateway side of the contract — the
`/grafana/*` route, the header injection and the edge secret — is
documented in [edge-plane's README](../../edge-plane/README.md).

**Not every authenticated user reaches Grafana.** `/grafana` is the one
route edge-plane restricts to Authelia's `admins` group; every other
routed app is open to any provisioned account. The gate is a
match-then-explicit-deny rule pair in edge-plane's
`authelia/configuration.yml`, documented in
[identity-contract.md](../../edge-plane/docs/identity-contract.md#access-control-the-admins-group-grafana-gate).
It is authorization, not authentication, and obs-plane sees only the
`X-Auth-User` that got past it — so `${GRAFANA_VIEWER_ROLE:-Viewer}` is
the role an admin lands on *inside* Grafana, not who is let through the
door.

## The audit-trail viewer (`grafana-audit`)

A second Grafana, at `https://${EDGE_HOST}/audit/`, reads the audit trail
and nothing else ([audit.md](audit.md#access)). It is the same construct as
`grafana` above — `edge-net` alias `grafana-audit`, served under a sub-path,
signed in from `X-Auth-User` — with these differences, all set on the
`grafana-audit` service in `docker/compose.yaml`:

- edge-plane admits only its `audit` group to `/audit`; `admins` is not
  enough.
- Everyone signs in as **Viewer** (no `GRAFANA_VIEWER_ROLE` knob) with
  Explore allowed (`GF_USERS_VIEWERS_CAN_EDIT`): readers can search but save
  nothing. Snapshots, public dashboards and server-side query history are
  off.
- Its only datasource is the audit store, read through `loki-audit-read`,
  which passes Loki's read API and nothing else. The ops `grafana` is on
  neither of the audit store's networks, so no datasource added there could
  reach the trail.
- It has **no local admin, no login form and no basic auth**
  (`GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION`, `GF_AUTH_DISABLE_LOGIN_FORM`,
  `GF_AUTH_BASIC_ENABLED`): its datasource and dashboard are provisioned
  from files, and readers only ever arrive through the gateway. An account
  that happens to be called `admin` therefore signs in as a Viewer like
  everyone else. The dev overlay publishes no port for it.
- Its guard is fail-closed (below).

## The edge guards

`X-Auth-User` is trustworthy only if it comes from edge-plane's Caddy,
which strips any client-supplied copy and injects its own after Authelia
has authenticated the request. Grafana cannot tell who sent the header,
and `edge-net` is shared with every app frontend and Open WebUI, so
membership in it is no proof of being the gateway. Each Grafana therefore
listens on `127.0.0.1:3001` only and accepts the proxy header only from
there (`GF_AUTH_PROXY_WHITELIST=127.0.0.1`); an nginx guard in the same
network namespace owns `:3000` and decides, by the `X-Edge-Secret` header
the gateway adds, whether identity headers get through:

| Guard | Secret | Without the right secret |
|---|---|---|
| `grafana-audit-guard` | `AUDIT_EDGE_SECRET`, required | **403** for every request. No access log: the gateway's access log, which is in the audit trail, already records each read |
| `grafana-guard` | `GRAFANA_EDGE_SECRET`, optional | The identity headers are dropped, so Grafana shows its login form, the admin's fallback. Unset, every request passes them on, as before the guard. Logs path, never query string |

Each secret must equal the variable of the same name in edge-plane's `.env`
(generate it there with `make secret`); the guard refuses to start with a
value shorter than 32 characters or with characters outside
`A–Z a–z 0–9 . _ ~ + / = -`. Both guards strip `X-Edge-Secret` before
Grafana sees it and pass Grafana Live's websocket upgrade through. Configs:
`nginx/grafana-guard.conf.template`, `nginx/grafana-audit-guard.conf.template`.

The residual trust is in whoever holds the secrets — the gateway host and
the operators of this host — not in every member of `edge-net`.

## Without the gateway: dev overlay and SSH tunnel

The production shape (`make up`, `docker/compose.yaml` alone) still
publishes **no host ports** — same convention as every other federation
member. Without the gateway (or for admin access, since the ops login form
stays enabled), reach the ops Grafana either with `make up-dev` (layers
`docker/compose.override.yaml`, which publishes `${GRAFANA_HOST_PORT:-3001}`
on the host for the guard's `:3000`) or via an SSH tunnel to the container
port instead of publishing it at all. With `GRAFANA_EDGE_SECRET` set, a
browser that reaches the guard this way sends no secret, so it gets the
login form. `compose.override.yaml` also relaxes the
node-exporter root-filesystem mount from `rslave` to a plain bind for
local (e.g. macOS/Docker Desktop) development — production keeps
`rslave` so post-boot mounts stay visible to `node_filesystem_*` metrics.
