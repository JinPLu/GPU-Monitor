#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
dist_app_entry="${project_root}/dist/ServerPilot.app"
user_applications_dir="${GPU_BROKER_USER_APPLICATIONS_DIR:-${HOME}/Applications}"
final_app_bundle="${user_applications_dir}/ServerPilot.app"
staging_root="$(mktemp -d /tmp/gpu-broker-macos-app.XXXXXX)"
trap 'rm -rf "${staging_root}"' EXIT
app_bundle="${staging_root}/ServerPilot.app"
macos_dir="${app_bundle}/Contents/MacOS"
resources_dir="${app_bundle}/Contents/Resources"
runtime_dir="${resources_dir}/BrokerRuntime"
root_app_entry="${project_root}/ServerPilot.app"
legacy_root_app_entry="${project_root}/GPU Broker.app"
legacy_user_app_bundle="${user_applications_dir}/GPU Broker.app"
core_dir="${script_dir}/GPUBrokerCore"
core_sources=("${core_dir}/Sources/GPUBrokerCore"/*.swift(N))
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
uv_bin="${GPU_BROKER_UV:-$(command -v uv || true)}"
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
  --name gpu-broker \
  --paths "${project_root}/src" \
  --collect-submodules uvicorn \
  --add-data "${project_root}/src/gpu_broker/migrations:gpu_broker/migrations" \
  --add-data "${project_root}/src/gpu_broker/web:gpu_broker/web" \
  --distpath "${backend_dist_dir}" \
  --workpath "${backend_work_dir}" \
  --specpath "${backend_spec_dir}" \
  "${script_dir}/backend_main.py"
cp "${backend_dist_dir}/gpu-broker" "${runtime_dir}/gpu-broker"
chmod 755 "${runtime_dir}/gpu-broker"
"${runtime_dir}/gpu-broker" --help >/dev/null

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
mkdir -p "${project_root}/dist" "${user_applications_dir}"
if [[ ! -e "${final_app_bundle}" && -d "${legacy_user_app_bundle}" ]]; then
  legacy_bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${legacy_user_app_bundle}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "${legacy_bundle_id}" == "local.gpu-broker.desktop" ]]; then
    mv "${legacy_user_app_bundle}" "${final_app_bundle}"
  fi
fi
if [[ -e "${final_app_bundle}" || -L "${final_app_bundle}" ]]; then
  existing_bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${final_app_bundle}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "${existing_bundle_id}" != "local.gpu-broker.desktop" ]]; then
    print -u2 "Refusing to replace an unrelated app: ${final_app_bundle}"
    exit 1
  fi
  rm -rf "${final_app_bundle}"
fi
ditto --norsrc "${app_bundle}" "${final_app_bundle}"
xattr -cr "${final_app_bundle}"
codesign --force --deep --sign - "${final_app_bundle}"
codesign --verify --deep --strict "${final_app_bundle}"

if [[ -e "${dist_app_entry}" || -L "${dist_app_entry}" ]]; then
  if [[ -L "${dist_app_entry}" ]]; then
    unlink "${dist_app_entry}"
  else
    existing_bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${dist_app_entry}/Contents/Info.plist" 2>/dev/null || true)"
    if [[ "${existing_bundle_id}" != "local.gpu-broker.desktop" ]]; then
      print -u2 "Refusing to replace unrelated dist entry: ${dist_app_entry}"
      exit 1
    fi
    rm -rf "${dist_app_entry}"
  fi
fi
ln -s "${final_app_bundle}" "${dist_app_entry}"

if [[ -L "${root_app_entry}" ]]; then
  unlink "${root_app_entry}"
elif [[ -e "${root_app_entry}" ]]; then
  print -u2 "Refusing to replace non-symlink path: ${root_app_entry}"
  exit 1
fi

ln -s "dist/ServerPilot.app" "${root_app_entry}"

if [[ -L "${legacy_root_app_entry}" && "$(readlink "${legacy_root_app_entry}")" == "dist/GPU Broker.app" ]]; then
  unlink "${legacy_root_app_entry}"
fi

echo "Built ${final_app_bundle}"
echo "Dist entry ${dist_app_entry}"
echo "Project entry ${root_app_entry}"
