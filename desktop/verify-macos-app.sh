#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
app_bundle="${1:-${project_root}/ServerPilot.app}"
frontend="${app_bundle}/Contents/MacOS/ServerPilot"
runtime_root="${app_bundle}/Contents/Resources/ServerPilotRuntime"
backend="${runtime_root}/serverpilot"
inventory="${runtime_root}/configs/inventory.yaml"

for required in "${frontend}" "${backend}" "${inventory}"; do
  if [[ ! -e "${required}" ]]; then
    print -u2 "Missing standalone app resource: ${required}"
    exit 1
  fi
done
if [[ ! -x "${frontend}" || ! -x "${backend}" ]]; then
  print -u2 "Bundled frontend/backend is not executable"
  exit 1
fi

xattr -d com.apple.FinderInfo "${app_bundle}" 2>/dev/null || true
xattr -d 'com.apple.fileprovider.fpfs#P' "${app_bundle}" 2>/dev/null || true
(
  signature_check_root="$(mktemp -d /tmp/serverpilot-signature-check.XXXXXX)"
  trap 'rm -rf "${signature_check_root}"' EXIT
  signature_check_bundle="${signature_check_root}/ServerPilot.app"
  COPYFILE_DISABLE=1 ditto --norsrc "${app_bundle}" "${signature_check_bundle}"
  xattr -cr "${signature_check_bundle}"
  codesign --verify --deep --strict "${signature_check_bundle}"
)
"${backend}" --help >/dev/null

external_links="$(otool -L "${frontend}" | tail -n +2 | awk '{print $1}' | grep -Ev '^(/System/|/usr/lib/)' || true)"
if [[ -n "${external_links}" ]]; then
  print -u2 "Frontend links non-system libraries:"
  print -u2 -- "${external_links}"
  exit 1
fi

smoke_dir="$(mktemp -d /tmp/serverpilot-standalone-smoke.XXXXXX)"
smoke_pid=""
cleanup() {
  if [[ -n "${smoke_pid}" ]] && kill -0 "${smoke_pid}" 2>/dev/null; then
    kill "${smoke_pid}" 2>/dev/null || true
    wait "${smoke_pid}" 2>/dev/null || true
  fi
  rm -rf "${smoke_dir}"
}
trap cleanup EXIT

smoke_port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
(
  cd /tmp
  exec "${backend}" serve \
    --db "${smoke_dir}/serverpilot.sqlite3" \
    --inventory "${inventory}" \
    --host 127.0.0.1 \
    --port "${smoke_port}"
) >"${smoke_dir}/server.log" 2>&1 &
smoke_pid=$!

ready=false
for _attempt in {1..150}; do
  if curl --fail --silent "http://127.0.0.1:${smoke_port}/health/ready" >"${smoke_dir}/ready.json"; then
    ready=true
    break
  fi
  if ! kill -0 "${smoke_pid}" 2>/dev/null; then
    print -u2 "Bundled backend exited during standalone smoke test"
    sed -n '1,200p' "${smoke_dir}/server.log" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ "${ready}" != true ]]; then
  print -u2 "Bundled backend did not become ready"
  sed -n '1,200p' "${smoke_dir}/server.log" >&2
  exit 1
fi

curl --fail --silent \
  -H 'X-ServerPilot-Actor: standalone-smoke' \
  "http://127.0.0.1:${smoke_port}/api/v1/snapshot" \
  >"${smoke_dir}/snapshot.json"
python3 - "${smoke_dir}/snapshot.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["schema_version"] == "v1"
assert payload["data"]["summary"]["total_servers"] == 0
assert payload["data"]["summary"]["total_gpus"] == 0
PY

print "Standalone macOS app verification: PASS"
