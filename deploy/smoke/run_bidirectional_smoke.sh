#!/usr/bin/env bash
set -euo pipefail

build_dir=${TRONNER_ENGINE_BUILD_DIR:-${ENGINE_BUILD_DIR:-}}
engine_binary=${TRONNER_ENGINE_BINARY:-${build_dir:+$build_dir/src/armagetronad_main}}
engine_data_dir=${TRONNER_ENGINE_DATA_DIR:-}
engine_config_dir=${TRONNER_ENGINE_CONFIG_DIR:-}
probe_dir=${SMOKE_PROBE_DIR:-${build_dir:+$build_dir/tronner-smoke-probes}}
server_port=${BIDIRECTIONAL_SMOKE_PORT:-4546}
smoke_timeout_seconds=${SMOKE_TIMEOUT_SECONDS:-90}
display_server_tags=${DISPLAY_SERVER_TAGS:-0}
work_dir=$(mktemp -d -t tronner-bidirectional.XXXXXXXX)
smoke_log=$work_dir/server.log
client_log=$work_dir/client.log
import_socket=$work_dir/engine-import.sock
console_input=$work_dir/console.in
server_job=
client_job=
hold_job=

cleanup()
{
  local status=$?
  [[ -z "$client_job" ]] || kill "$client_job" 2>/dev/null || true
  [[ -z "$hold_job" ]] || kill "$hold_job" 2>/dev/null || true
  [[ -z "$server_job" ]] || kill "$server_job" 2>/dev/null || true
  [[ -z "$client_job" ]] || wait "$client_job" 2>/dev/null || true
  [[ -z "$hold_job" ]] || wait "$hold_job" 2>/dev/null || true
  [[ -z "$server_job" ]] || wait "$server_job" 2>/dev/null || true
  if ((status)); then
    for log in "$work_dir"/*.log; do
      [[ ! -f "$log" ]] || {
        printf '\n--- %s ---\n' "$log" >&2
        tail -n 120 "$log" >&2 || true
      }
    done
  fi
  if [[ "$work_dir" == /tmp/tronner-bidirectional.* ]]; then
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
test -x "$probe_dir/server_info_probe"
export TRONNER_ENGINE_DATA_DIR="$engine_data_dir"
mkdir -p "$work_dir/config" "$work_dir/userdata" "$work_dir/var"
: >"$console_input"
: >"$smoke_log"
: >"$client_log"
cat >"$work_dir/config/federation-bidirectional-smoke.cfg" <<EOF
SERVER_PORT $server_port
SERVER_IP ANY
TALK_TO_MASTER 0
SERVER_NAME Tronner Federation Bidirectional Smoke
MAX_CLIENTS 8
MIN_PLAYERS 1
NUM_AIS 1
FEDERATION_EXPORT_ENABLED 0
FEDERATION_EXPORT_INTERVAL -1
FEDERATION_IMPORT_ENABLED 1
FEDERATION_IMPORT_SOCKET $import_socket
FEDERATION_GHOST_LABEL B
FEDERATION_GHOST_TIMEOUT 30
FEDERATION_GHOST_LIMIT 8
EOF

timeout "${smoke_timeout_seconds}s" "$engine_binary" \
  --daemon \
  --input "$console_input" \
  --path-no-absolutecheck \
  --configdir "$engine_config_dir" \
  --userconfigdir "$work_dir/config" \
  --datadir "$engine_data_dir" \
  --userdatadir "$work_dir/userdata" \
  --vardir "$work_dir/var" \
  --extraconfig federation-bidirectional-smoke.cfg \
  >"$smoke_log" 2>&1 &
server_job=$!

for _ in {1..100}; do
  [[ -S "$import_socket" ]] && break
  sleep 0.05
done
[[ -S "$import_socket" ]]

"$probe_dir/network_hold" "127.0.0.1:$server_port" >"$work_dir/hold.log" 2>&1 &
hold_job=$!
"$probe_dir/player_name_probe" "127.0.0.1:$server_port" >"$client_log" 2>&1 &
client_job=$!
for _ in {1..300}; do
  grep -q "Go (round" "$smoke_log" 2>/dev/null && break
  sleep 0.05
done
grep -q "Go (round" "$smoke_log"

for _ in {1..100}; do
  grep -q "federation_prob" "$work_dir/var/online_players.txt" 2>/dev/null && break
  sleep 0.05
done
grep -q "federation_prob" "$work_dir/var/online_players.txt"
printf '%s\n' "OP federation_prob 19" >>"$console_input"
for _ in {1..100}; do
  grep -q "federation_prob@L_OP" "$smoke_log" 2>/dev/null && break
  sleep 0.05
done
grep -q "federation_prob@L_OP" "$smoke_log"

if [[ "$display_server_tags" == 1 ]]; then
  printf '%s\n' \
    "FEDERATION_DISPLAY_SERVER_TAGS federation_prob 1" >>"$console_input"
  sleep 0.2
fi

python3 - "$import_socket" <<'PY'
import socket
import sys
import time

target = sys.argv[1]
identity = "616c696365"
display = "416c696365"
colored = "3078666630303030416c696365"
authenticated = "416c69636540666f72756d73"
now = time.time_ns()
lines = [
    f"GHOST_V2 PRESENCE {identity} {display} {colored} {authenticated} 15 2 0 0.125 {now} 1",
    f"GHOST_V1 COLOR {identity} {now + 1} 0.2 0.4 0.6",
    f"GHOST_V2 STATE {identity} {display} {colored} {authenticated} 15 2 0 0.125 {now + 2} 10.0 5 7 1 0 30 1",
    f"GHOST_V1 FLAGS {identity} {now + 3} 1",
    f"GHOST_V1 CHAT {identity} 68656c6c6f2066726f6d20736d6f6b65",
]
sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
for line in lines:
    sock.sendto(line.encode("ascii"), target)
for tick in range(60):
    observed = time.time_ns()
    line = (
        "GHOST_V2 STATE 616c696365 416c696365 3078666630303030416c696365 "
        "416c69636540666f72756d73 "
        f"15 2 0 0.125 {observed} {10.0 + tick * 0.05} 5 7 1 0 0.1 1"
    )
    sock.sendto(line.encode("ascii"), target)
    time.sleep(0.05)
PY

server_info_status=0
server_info=$("$probe_dir/server_info_probe" 127.0.0.1 "$server_port") || server_info_status=$?
printf '%s\n' "$server_info"
[[ "$server_info_status" -eq 0 ]]
grep -F "combined=Alice0xffffff (Alice@forums), Federation Prob0xffffff (federation_prob@L_OP)" \
  <<<"$server_info"

# Refresh the imported motion sample while the real probe remains connected,
# keeping the disposable arena stable for the engine-state assertions.
python3 - "$import_socket" <<'PY'
import socket
import sys
import time

target = sys.argv[1]
now = time.time_ns()
line = (
    "GHOST_V2 STATE 616c696365 416c696365 3078666630303030416c696365 "
    "416c69636540666f72756d73 "
    f"15 2 0 0.125 {now} 13.5 5 7 1 0 0.1 1"
)
sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
sock.sendto(line.encode("ascii"), target)
PY
sleep 1
printf '%s\n' FEDERATION_IMPORT_STATS PLAYERS >>"$console_input"
sleep 1
for _ in {1..100}; do
  grep -Eq "^[^ ]+ 1 125 [^ ]+ [^ ]+ Alice$" \
    "$work_dir/var/online_players.txt" 2>/dev/null && break
  sleep 0.1
done
grep -F "Federation import: 1 remote players, 1 visible cycles, 0 trails, socket ready." "$smoke_log"
grep -F "Alice" "$smoke_log"
grep -F "Federation idle indicators: 1." "$smoke_log"
grep -F "Federation cycle colors: 1 exact." "$smoke_log"

client_status=0
wait "$client_job" || client_status=$?
client_job=
[[ "$client_status" -eq 0 ]]
if [[ "$display_server_tags" == 1 ]]; then
  grep -E "human=0 .*ai=0 .*ghost=1 .*name=\[B\] Alice" "$client_log"
  grep -E "ghost=1 .*colored=.*0x66ccff\[B\].*0xff0000Alice" "$client_log"
else
  grep -E "human=0 .*ai=0 .*ghost=1 .*name=Alice" "$client_log"
  grep -E "ghost=1 .*colored=.*0xff0000Alice" "$client_log"
  ! grep -Fq "[B] Alice" "$client_log"
fi
grep -E "^[^ ]+ 1 125 [^ ]+ [^ ]+ Alice$" \
  "$work_dir/var/online_players.txt"

# A native death export has zero position/direction/speed. It must destroy the
# visual proxy instead of being rejected by the live-state direction guard.
python3 - "$import_socket" <<'PY'
import socket
import sys
import time

target = sys.argv[1]
now = time.time_ns()
line = (
    "GHOST_V2 STATE 616c696365 416c696365 3078666630303030416c696365 "
    "416c69636540666f72756d73 "
    f"15 2 0 0.125 {now} 11.0 0 0 0 0 0 0"
)
sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
sock.sendto(line.encode("ascii"), target)
PY
sleep 1
printf '%s\n' FEDERATION_IMPORT_STATS >>"$console_input"
sleep 1
grep -F "Federation import: 1 remote players, 0 visible cycles, 0 trails, socket ready." "$smoke_log"

printf '%s\n' "Bidirectional engine, per-viewer tag, client-visible name, color, idle, local-ping, and death smoke passed."
