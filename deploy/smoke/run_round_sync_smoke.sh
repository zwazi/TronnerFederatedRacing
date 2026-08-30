#!/usr/bin/env bash
set -euo pipefail

build_dir=${TRONNER_ENGINE_BUILD_DIR:-${ENGINE_BUILD_DIR:-}}
engine_binary=${TRONNER_ENGINE_BINARY:-${build_dir:+$build_dir/src/armagetronad_main}}
engine_data_dir=${TRONNER_ENGINE_DATA_DIR:-}
engine_config_dir=${TRONNER_ENGINE_CONFIG_DIR:-}
probe_dir=${SMOKE_PROBE_DIR:-${build_dir:+$build_dir/tronner-smoke-probes}}
server_one_port=${ROUND_SYNC_PORT_ONE:-4556}
server_two_port=${ROUND_SYNC_PORT_TWO:-4557}
smoke_timeout_seconds=${SMOKE_TIMEOUT_SECONDS:-60}
work_dir=$(mktemp -d -t tronner-round-sync.XXXXXXXX)
server_one_job=
server_two_job=
hold_one_job=
hold_two_job=
client_one_job=
client_two_job=

cleanup()
{
  local status=$?
  for job in "$client_one_job" "$client_two_job" "$hold_one_job" "$hold_two_job" "$server_one_job" "$server_two_job"; do
    [[ -z "$job" ]] || kill "$job" 2>/dev/null || true
  done
  for job in "$client_one_job" "$client_two_job" "$hold_one_job" "$hold_two_job" "$server_one_job" "$server_two_job"; do
    [[ -z "$job" ]] || wait "$job" 2>/dev/null || true
  done
  if ((status)); then
    for log in "$work_dir"/*/*.log; do
      [[ ! -f "$log" ]] || {
        printf '\n--- %s ---\n' "$log" >&2
        tail -n 100 "$log" >&2 || true
      }
    done
  fi
  if [[ "$work_dir" == /tmp/tronner-round-sync.* ]]; then
    rm -rf -- "$work_dir"
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT

test -x "$engine_binary"
test -d "$engine_data_dir"
test -d "$engine_config_dir"
test -x "$probe_dir/network_hold"
test -x "$probe_dir/player_name_probe"
export TRONNER_ENGINE_DATA_DIR="$engine_data_dir"

prepare_node()
{
  local node=$1
  local port=$2
  mkdir -p "$work_dir/$node/config" "$work_dir/$node/userdata" "$work_dir/$node/var"
  : >"$work_dir/$node/config/user.cfg"
  : >"$work_dir/$node/console.in"
  : >"$work_dir/$node/server.log"
  printf '%s\n' \
    "SERVER_PORT $port" \
    "SERVER_IP ANY" \
    "TALK_TO_MASTER 0" \
    "SERVER_NAME Tronner Round Sync Smoke $node" \
    "MAX_CLIENTS 4" \
    "MIN_PLAYERS 1" \
    "NUM_AIS 1" \
    "FEDERATION_EXPORT_ENABLED 0" \
    "FEDERATION_IMPORT_ENABLED 0" \
    "FEDERATION_ROUND_SYNC 1" \
    "FEDERATION_ROUND_SYNC_TIMEOUT 5" \
    "FEDERATION_ROUND_RELEASE_AT 0" \
    "LADDERLOG_WRITE_FEDERATION_ROUND_READY 1" \
    "LADDERLOG_WRITE_FEDERATION_ROUND_RELEASED 1" \
    "LADDERLOG_WRITE_ROUND_STARTED 1" \
    >"$work_dir/$node/config/round-sync-smoke.cfg"
}

start_node()
{
  local node=$1
  timeout "${smoke_timeout_seconds}s" "$engine_binary" \
    --daemon \
    --input "$work_dir/$node/console.in" \
    --path-no-absolutecheck \
    --configdir "$engine_config_dir" \
    --userconfigdir "$work_dir/$node/config" \
    --datadir "$engine_data_dir" \
    --userdatadir "$work_dir/$node/userdata" \
    --vardir "$work_dir/$node/var" \
    --extraconfig round-sync-smoke.cfg \
    >"$work_dir/$node/server.log" 2>&1 &
  printf '%s' "$!"
}

wait_for_line()
{
  local file=$1
  local pattern=$2
  for _ in {1..600}; do
    grep -q "$pattern" "$file" 2>/dev/null && return 0
    sleep 0.05
  done
  printf 'Timed out waiting for %s in %s\n' "$pattern" "$file" >&2
  tail -n 80 "$file" >&2 || true
  return 1
}

prepare_node one "$server_one_port"
prepare_node two "$server_two_port"
server_one_job=$(start_node one)
server_two_job=$(start_node two)

"$probe_dir/network_hold" "127.0.0.1:$server_one_port" >"$work_dir/one/hold.log" 2>&1 &
hold_one_job=$!
"$probe_dir/network_hold" "127.0.0.1:$server_two_port" >"$work_dir/two/hold.log" 2>&1 &
hold_two_job=$!
"$probe_dir/player_name_probe" "127.0.0.1:$server_one_port" >"$work_dir/one/client.log" 2>&1 &
client_one_job=$!
"$probe_dir/player_name_probe" "127.0.0.1:$server_two_port" >"$work_dir/two/client.log" 2>&1 &
client_two_job=$!

wait_for_line "$work_dir/one/var/ladderlog.txt" '^FEDERATION_ROUND_READY '
wait_for_line "$work_dir/two/var/ladderlog.txt" '^FEDERATION_ROUND_READY '

release_at=$(python3 -c 'import time; print(f"{time.time() + 0.75:.6f}")')
printf 'FEDERATION_ROUND_RELEASE_AT %s\n' "$release_at" >>"$work_dir/one/console.in"
printf 'FEDERATION_ROUND_RELEASE_AT %s\n' "$release_at" >>"$work_dir/two/console.in"

wait_for_line "$work_dir/one/var/ladderlog.txt" '^FEDERATION_ROUND_RELEASED '
wait_for_line "$work_dir/two/var/ladderlog.txt" '^FEDERATION_ROUND_RELEASED '
wait_for_line "$work_dir/one/var/ladderlog.txt" '^ROUND_STARTED '
wait_for_line "$work_dir/two/var/ladderlog.txt" '^ROUND_STARTED '

python3 - \
  "$work_dir/one/var/ladderlog.txt" \
  "$work_dir/two/var/ladderlog.txt" \
  "$release_at" <<'PY'
import pathlib
import sys


def events(path):
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    ready = next(i for i, line in enumerate(lines) if line.startswith("FEDERATION_ROUND_READY "))
    released = next(i for i, line in enumerate(lines) if line.startswith("FEDERATION_ROUND_RELEASED "))
    started = next(i for i, line in enumerate(lines) if line.startswith("ROUND_STARTED "))
    parts = lines[released].split()
    return lines, ready, released, started, float(parts[-2]), int(parts[-1])


one = events(sys.argv[1])
two = events(sys.argv[2])
target = float(sys.argv[3])
print(f"release target={target:.6f}; one={one[4]:.6f}; two={two[4]:.6f}")
for label, event in (("one", one), ("two", two)):
    _, ready, released, started, actual, synchronized = event
    assert ready < released < started, f"{label}: incorrect READY/RELEASED/ROUND_STARTED order"
    assert synchronized == 1, f"{label}: engine fell through its safety timeout"
    assert actual >= target, f"{label}: released before the shared target"

skew = abs(one[4] - two[4])
assert skew <= 0.05, f"release skew {skew:.6f}s exceeds 50ms"
print(f"Two-engine round synchronization smoke passed; release skew {skew * 1000:.1f}ms.")
PY
