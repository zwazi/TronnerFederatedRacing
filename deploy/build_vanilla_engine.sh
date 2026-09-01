#!/usr/bin/env bash
set -euo pipefail

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export TRONNER_ENGINE_VARIANT=vanilla
exec "$repository_dir/deploy/build_engine.sh"
