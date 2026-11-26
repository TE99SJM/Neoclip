#!/usr/bin/env bash
# Run from a fetched test checkout. Promote the pinned current release first.
set -euo pipefail

main_dir="$HOME/NeoClip-Web"
test_dir="$HOME/neoclip-test-server"
repo="$test_dir/repo"
production_ref=070d2115a0b1a1c771630f3ab6e1ec0114f30b0b
test_ref="${1:?Pass the tested commit SHA}"
source_path=NeoClip-code-before-cleanup-20261028-164840/NeoClip-Web/neoclip-web

test -f "$main_dir/compose.yaml"
test -f "$test_dir/compose.json"
git -C "$repo" cat-file -e "$test_ref:$source_path/app/continuous_proxy.py"

# Stop before changing files if either app has an active render.
python3 - <<'PY'
import json
import urllib.request
for port in (8000, 8001):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/render/status', timeout=10) as response:
        status = json.load(response)['status']
    if status not in ('idle', 'done', 'cancelled', 'error'):
        raise SystemExit(f'Port {port} is rendering. Wait for it to finish, then run this again.')
PY

git -C "$repo" fetch origin neoclip-240-stable
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
git -C "$repo" archive "$production_ref" "$source_path/app" | tar -x -C "$stage"

printf '%s\n' 'Installing the current test snapshot on the main server...'
python3 - "$stage/$source_path/app" "$main_dir/app" <<'PY'
import shutil
import sys
shutil.copytree(sys.argv[1], sys.argv[2], dirs_exist_ok=True)
PY
docker compose --project-directory "$main_dir" -f "$main_dir/compose.yaml" up -d \
    --build --wait --wait-timeout 120 neoclip
python3 - <<'PY'
import json
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/api/state', timeout=10) as response:
    state = json.load(response)
assert state['build'] == '2.1.4-240', state.get('build')
print('MAIN:', state['build'])
PY

printf '%s\n' 'Installing continuous proxy encoding on the test server...'
git -C "$repo" switch --detach "$test_ref"
docker compose --project-directory "$test_dir" -f "$test_dir/compose.json" up -d \
    --force-recreate --wait --wait-timeout 120 neoclip-test
python3 - <<'PY'
import json
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8001/api/state', timeout=10) as response:
    state = json.load(response)
assert state['build'] == '2.1.5-240-test', state.get('build')
print('TEST:', state['build'])
print('Reload both web apps. Test previews will build in the new continuous format.')
PY
