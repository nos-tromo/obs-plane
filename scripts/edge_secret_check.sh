#!/bin/sh
# Mounted into /docker-entrypoint.d/ of the Grafana guards (nginx/); the
# image's entrypoint runs it with `set -e` before nginx starts, so a refusal
# here stops the guard. EDGE_SECRET is the value the gateway sends as
# X-Edge-Secret; EDGE_SECRET_REQUIRED=1 refuses to start without one. A set
# secret must have at least 32 characters (edge-plane's `make secret` mints
# 64) and only characters that cannot change the meaning of the nginx
# config it is written into. The secret itself is never printed.
set -eu

secret=${EDGE_SECRET:-}
if [ -z "$secret" ]; then
  if [ "${EDGE_SECRET_REQUIRED:-0}" = 1 ]; then
    echo "edge secret check: the edge secret is required but empty" >&2
    exit 1
  fi
  exit 0
fi
if [ "${#secret}" -lt 32 ]; then
  echo "edge secret check: the edge secret must be at least 32 characters" >&2
  exit 1
fi
case $secret in
  *[!A-Za-z0-9._~+/=-]*)
    echo "edge secret check: the edge secret may contain only letters, digits and . _ ~ + / = -" >&2
    exit 1
    ;;
esac
