#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "Run this installer as root." >&2
    exit 2
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cluster_config=
node_config=
secrets_dir=
start_services=0
build_engine=1
allow_examples=0

usage() {
    echo "Usage: $0 --cluster FILE --node FILE [--secrets-dir DIR] [--start] [--skip-engine-build] [--allow-examples]" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --cluster)
            cluster_config=${2:?--cluster requires a file}
            shift 2
            ;;
        --node)
            node_config=${2:?--node requires a file}
            shift 2
            ;;
        --secrets-dir)
            secrets_dir=${2:?--secrets-dir requires a directory}
            shift 2
            ;;
        --start)
            start_services=1
            shift
            ;;
        --skip-engine-build)
            build_engine=0
            shift
            ;;
        --allow-examples)
            allow_examples=1
            shift
            ;;
        *)
            usage
            ;;
    esac
done

[[ -n "$cluster_config" && -f "$cluster_config" ]] || usage
[[ -n "$node_config" && -f "$node_config" ]] || usage
if ((start_services && allow_examples)); then
    echo "Refusing to start services from example configuration." >&2
    exit 2
fi

render_dir=$(mktemp -d -t tronner-render.XXXXXXXX)
cleanup() {
    rm -rf -- "$render_dir"
}
trap cleanup EXIT

render_args=(
    --cluster "$cluster_config"
    --node "$node_config"
    --output "$render_dir"
)
if ((!allow_examples)); then
    render_args+=(--production)
fi
python3 "$repository_dir/deploy/render_node.py" "${render_args[@]}"

mapfile -t required_secrets < <(
    python3 - "$render_dir/manifest.json" <<'PY'
import json,sys
for name in json.load(open(sys.argv[1], encoding="utf-8"))["requiredSecretFiles"]:
    print(name)
PY
)
firebase_enabled=$(
    python3 - "$render_dir/manifest.json" <<'PY'
import json,sys
print(int(bool(json.load(open(sys.argv[1], encoding="utf-8"))["firebaseEnabled"])))
PY
)
if ((${#required_secrets[@]} || firebase_enabled)); then
    [[ -n "$secrets_dir" && -d "$secrets_dir" ]] || {
        echo "This node requires --secrets-dir." >&2
        exit 2
    }
fi
for name in "${required_secrets[@]}"; do
    [[ -f "$secrets_dir/$name" && ! -L "$secrets_dir/$name" ]] || {
        echo "Missing regular secret file: $secrets_dir/$name" >&2
        exit 2
    }
done
if ((firebase_enabled)); then
    [[ -f "$secrets_dir/firebase-service-account.json" && ! -L "$secrets_dir/firebase-service-account.json" ]] || {
        echo "Missing regular Firebase service-account file." >&2
        exit 2
    }
fi

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    if [[ ${ID:-} != ubuntu ]]; then
        echo "This installer currently supports Ubuntu; found ${ID:-unknown}." >&2
        exit 2
    fi
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    autoconf automake build-essential ca-certificates git libtool libxml2-dev \
    libzthread-dev pkg-config python3 python3-cryptography rsync ufw

if ! id armagetron >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/armagetronad --create-home \
        --shell /usr/sbin/nologin armagetron
fi

if ((build_engine)); then
    TRONNER_BUILD_JOBS=${TRONNER_BUILD_JOBS:-1} "$repository_dir/deploy/build_engine.sh"
else
    test -x /opt/armagetronad/bin/armagetronad-dedicated || {
        echo "--skip-engine-build requires an installed dedicated-server binary." >&2
        exit 2
    }
fi

install -d -o root -g root -m 0755 /opt/TronnerRacing
rsync -a --delete --exclude __pycache__ "$repository_dir/controller/" /opt/TronnerRacing/
chown -R root:root /opt/TronnerRacing
chmod 0755 /opt/TronnerRacing/TronnerRacing.py

game_config_dir=/opt/armagetronad/etc/games/armagetronad-dedicated
install -d -o root -g armagetron -m 0755 "$game_config_dir"
install -m 0644 "$repository_dir/config/tronner-racing.cfg" "$game_config_dir/tronner-racing.cfg"
install -m 0644 "$render_dir/server.cfg" "$game_config_dir/server.cfg"
ln -sfn "$game_config_dir" /etc/armagetronad-dedicated

install -d -o root -g armagetron -m 0750 /etc/tronner-racing
install -m 0640 -o root -g armagetron "$render_dir/controller.json" /etc/tronner-racing/config.json
install -m 0644 -o root -g root "$repository_dir/config/helpful_messages.txt" /etc/tronner-racing/helpful_messages.txt

if ((firebase_enabled)); then
    install -m 0640 -o root -g armagetron \
        "$secrets_dir/firebase-service-account.json" \
        /etc/tronner-racing/firebase-service-account.json
fi

install -d -o armagetron -g armagetron -m 0750 \
    /var/lib/armagetronad /var/lib/tronner-racing
install -d -o armagetron -g armagetron -m 0755 \
    /var/lib/armagetronad/resource/automatic
touch /var/lib/armagetronad/console.in /var/lib/armagetronad/ladderlog.txt
chown armagetron:armagetron /var/lib/armagetronad/console.in /var/lib/armagetronad/ladderlog.txt

for unit in armagetronad.service tronner-racing.service; do
    install -m 0644 "$repository_dir/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable armagetronad.service tronner-racing.service

if ((start_services)); then
    systemctl restart armagetronad.service
    systemctl restart tronner-racing.service
fi

echo "Installation complete; public master-list advertising remains controlled by node.json."
echo "Review docs/installation.md before opening any firewall port or starting a production node."
