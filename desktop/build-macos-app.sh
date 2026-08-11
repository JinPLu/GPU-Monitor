#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
final_app_bundle="${project_root}/ServerPilot.app"
staging_root="$(mktemp -d /tmp/serverpilot-macos-app.XXXXXX)"
trap 'rm -rf "${staging_root}"' EXIT
app_bundle="${staging_root}/ServerPilot.app"
macos_dir="${app_bundle}/Contents/MacOS"
resources_dir="${app_bundle}/Contents/Resources"
runtime_dir="${resources_dir}/ServerPilotRuntime"
core_dir="${script_dir}/ServerPilotCore"
core_sources=("${core_dir}/Sources/ServerPilotCore"/*.swift(N))
swift_sources=("${core_sources[@]}" "${script_dir}"/*.swift(N))
deployment_target="14.0"
target_arch="$(uname -m)"

case "${target_arch}" in
  arm64|x86_64) ;;
  *)
    print -u2 "Unsupported macOS architecture: ${target_arch}"
    exit 1
    ;;
esac
target_triple="${target_arch}-apple-macosx${deployment_target}"
uv_bin="${SERVERPILOT_UV:-$(command -v uv || true)}"
backend_build_root="${project_root}/build/macos-backend"
backend_dist_dir="${backend_build_root}/dist"
backend_work_dir="${backend_build_root}/work"
backend_spec_dir="${backend_build_root}/spec"

if [[ "${1:-}" == "test" ]]; then
  cd "${core_dir}"
  swift test
  exit 0
fi

if [[ -z "${uv_bin}" || ! -x "${uv_bin}" ]]; then
  print -u2 "uv is required to assemble the self-contained app backend."
  exit 1
fi

mkdir -p "${macos_dir}" "${resources_dir}" "${runtime_dir}/configs"
cp "${script_dir}/Info.plist" "${app_bundle}/Contents/Info.plist"
cp "${script_dir}/assets/ServerPilot.icns" "${resources_dir}/ServerPilot.icns"
cp "${project_root}/configs/inventory.yaml" "${runtime_dir}/configs/inventory.yaml"
if [[ -d "${script_dir}/Fixtures" ]]; then
  mkdir -p "${resources_dir}/Fixtures"
  cp "${script_dir}/Fixtures"/*.json(N) "${resources_dir}/Fixtures/"
fi
plutil -lint "${app_bundle}/Contents/Info.plist" >/dev/null

mkdir -p "${backend_dist_dir}" "${backend_work_dir}" "${backend_spec_dir}"
"${uv_bin}" run --with 'pyinstaller>=6,<7' pyinstaller \
  --noconfirm \
  --onefile \
  --name serverpilot \
  --paths "${project_root}/src" \
  --collect-submodules uvicorn \
  --add-data "${project_root}/src/serverpilot/migrations:serverpilot/migrations" \
  --add-data "${project_root}/src/serverpilot/web:serverpilot/web" \
  --distpath "${backend_dist_dir}" \
  --workpath "${backend_work_dir}" \
  --specpath "${backend_spec_dir}" \
  "${script_dir}/backend_main.py"
cp "${backend_dist_dir}/serverpilot" "${runtime_dir}/serverpilot"
chmod 755 "${runtime_dir}/serverpilot"
"${runtime_dir}/serverpilot" --help >/dev/null

xcrun --sdk macosx swiftc \
  -target "${target_triple}" \
  -parse-as-library \
  -D DESKTOP_FIXTURES \
  -framework AppKit \
  -framework SwiftUI \
  "${swift_sources[@]}" \
  -o "${macos_dir}/ServerPilot"
xattr -cr "${app_bundle}"
xattr -d com.apple.FinderInfo "${app_bundle}" 2>/dev/null || true
xattr -d 'com.apple.fileprovider.fpfs#P' "${app_bundle}" 2>/dev/null || true
codesign --force --deep --sign - "${app_bundle}"
codesign --verify --deep --strict "${app_bundle}"
mkdir -p "${project_root}"
if [[ -e "${final_app_bundle}" || -L "${final_app_bundle}" ]]; then
  existing_bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${final_app_bundle}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "${existing_bundle_id}" != "local.serverpilot.desktop" &&
        "${existing_bundle_id}" != "local.gpu-broker.desktop" ]]; then
    print -u2 "Refusing to replace an unrelated app: ${final_app_bundle}"
    exit 1
  fi
  rm -rf "${final_app_bundle}"
fi
ditto --norsrc "${app_bundle}" "${final_app_bundle}"
xattr -cr "${final_app_bundle}"
xattr -rd com.apple.FinderInfo "${final_app_bundle}" 2>/dev/null || true
xattr -rd 'com.apple.fileprovider.fpfs#P' "${final_app_bundle}" 2>/dev/null || true
# Finder can immediately reattach FileProvider metadata to a bundle stored in
# the project folder. Verify an ephemeral, metadata-free copy of the canonical
# root bundle so signature checking remains deterministic without leaving a
# second app behind.
verification_bundle="${staging_root}/verified/ServerPilot.app"
mkdir -p "${verification_bundle:h}"
COPYFILE_DISABLE=1 ditto --norsrc "${final_app_bundle}" "${verification_bundle}"
xattr -cr "${verification_bundle}"
codesign --verify --deep --strict "${verification_bundle}"

remove_legacy_app() {
  local entry="$1"
  [[ -e "${entry}" || -L "${entry}" ]] || return 0
  if [[ -L "${entry}" ]]; then
    unlink "${entry}"
    return 0
  fi
  local bundle_id
  bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${entry}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "${bundle_id}" != "local.serverpilot.desktop" &&
        "${bundle_id}" != "local.gpu-broker.desktop" ]]; then
    print -u2 "Refusing to remove unrelated app: ${entry}"
    exit 1
  fi
  rm -rf "${entry}"
}

# Before the root-bundle rule, builds installed a second copy in ~/Applications
# and exposed aliases under dist/. Remove only verified ServerPilot bundles or
# symlinks after the new canonical bundle has passed signature verification.
remove_legacy_app "${HOME}/Applications/ServerPilot.app"
legacy_dist_entries=("${project_root}/dist"/ServerPilot*.app(N))
for legacy_entry in "${legacy_dist_entries[@]}"; do
  remove_legacy_app "${legacy_entry}"
done

echo "Built ${final_app_bundle}"
