# Grafana access

How Grafana is reached in each shape — through the edge gateway on
production, through the dev overlay or an SSH tunnel otherwise — and the
trust assumption that makes the gateway path safe.

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

`grafana` joins `edge-net` for exactly this reason; it is obs-plane's only
service on that seam. The gateway side of the contract — the `/grafana/*`
route and the header injection — is documented in
[edge-plane's README](../../edge-plane/README.md).

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

## Trusted-zone note

`X-Auth-User` is only trustworthy because
edge-plane's Caddy unconditionally strips any client-supplied copy of it
and injects its own after Authelia authenticates the request — Grafana
itself does no verification of the header's origin. Any container joined
to `edge-net` could reach `grafana:3000` directly and send an arbitrary
`X-Auth-User` value, auto-provisioning or impersonating a user. This is
the same trust posture already accepted by the app frontends
(chorus/docint/Nextext) that consume the identical header contract; it
is not a new exposure introduced by this change. Re-evaluate this
acceptance if edge-net membership ever grows beyond the gateway, the four
app frontends, Open WebUI, and Grafana.

## Without the gateway: dev overlay and SSH tunnel

The production shape (`make up`, `docker/compose.yaml` alone) still
publishes **no host ports** — same convention as every other federation
member. Without the gateway (or for admin access, since the login form
stays enabled), reach Grafana either with `make up-dev` (layers
`docker/compose.override.yaml`, which publishes
`${GRAFANA_HOST_PORT:-3001}`) or via an SSH tunnel to the container port
instead of publishing it at all. `compose.override.yaml` also relaxes the
node-exporter root-filesystem mount from `rslave` to a plain bind for
local (e.g. macOS/Docker Desktop) development — production keeps
`rslave` so post-boot mounts stay visible to `node_filesystem_*` metrics.
