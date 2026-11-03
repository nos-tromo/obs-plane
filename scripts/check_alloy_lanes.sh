#!/usr/bin/env bash
# Check which lane each fixture line lands in (docs/audit.md). Runs
# alloy/config.alloy in the pinned Alloy image with its two loki.write
# receivers swapped for the loki.echo components of
# alloy/lanes-test/harness.alloy, feeds alloy/lanes-test/lines/* through it,
# and compares every echoed line (lane, container, final text) with
# alloy/lanes-test/expected.txt. Needs Docker and python3.
# After an intended routing change: scripts/check_alloy_lanes.sh --update
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

dir=alloy/lanes-test
image=$(grep -o 'docker\.io/grafana/alloy:[^"]*' docker/compose.yaml | head -1)
want=$(cat "$dir"/lines/*.txt | wc -l | tr -d ' ')
name="alloy-lanes-check-$$"
config=$(sed -e 's/loki\.write\.audit\.receiver/loki.echo.audit.receiver/' \
  -e 's/loki\.write\.default\.receiver/loki.echo.ops.receiver/' alloy/config.alloy
  cat "$dir/harness.alloy")
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

# shellcheck disable=SC2016  # $CONFIG is expanded by the container's shell
docker run -d --name "$name" -e CONFIG="$config" -v "$ROOT/$dir:/h:ro" --entrypoint sh "$image" \
  -c 'printf "%s\n" "$CONFIG" > /tmp/harness.alloy && exec alloy run /tmp/harness.alloy --disable-reporting --storage.path=/tmp/data' \
  >/dev/null

# Wait until the echo output has stopped growing for three seconds.
got=0
last=-1
stable=0
for _ in $(seq 1 60); do
  got=$(docker logs "$name" 2>&1 | grep -c 'received log entry' || true)
  if ((got > 0 && got == last)); then
    stable=$((stable + 1))
    ((stable >= 3)) && break
  else
    stable=0
  fi
  last=$got
  sleep 1
done

parser=$(
  cat <<'PY'
import json, re, sys
rows = []
for line in sys.stdin:
    if "received log entry" not in line:
        continue
    echo = re.search(r"component_id=loki\.echo\.(\w+)", line).group(1)
    entry = json.loads('"' + re.search(r' entry="((?:[^"\\]|\\.)*)"', line).group(1) + '"')
    labels = json.loads('"' + re.search(r' labels="((?:[^"\\]|\\.)*)"', line).group(1) + '"')
    container = re.search(r'container="([^"]*)"', labels).group(1)
    lane = re.search(r'lane="([^"]*)"', labels)
    rows.append(f"{echo}\t{lane.group(1) if lane else '-'}\t{container}\t{entry}")
print("\n".join(sorted(rows)))
PY
)
actual=$(docker logs "$name" 2>&1 | python3 -c "$parser")

if [[ "${1:-}" == "--update" ]]; then
  printf '%s\n' "$actual" >"$dir/expected.txt"
  echo "wrote $dir/expected.txt ($got lines)"
  exit 0
fi
if diff -u "$dir/expected.txt" <(printf '%s\n' "$actual"); then
  echo "lanes OK: $got of $want fixture lines routed as expected, the rest dropped"
else
  echo "lane routing differs from $dir/expected.txt (see diff above)" >&2
  exit 1
fi
