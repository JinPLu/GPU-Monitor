#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
app_bundle="${project_root}/dist/GPU Broker.app"
macos_dir="${app_bundle}/Contents/MacOS"
resources_dir="${app_bundle}/Contents/Resources"
root_app_entry="${project_root}/GPU Broker.app"
swift_sources=("${script_dir}"/*.swift(N))

mkdir -p "${macos_dir}" "${resources_dir}"
cp "${script_dir}/Info.plist" "${app_bundle}/Contents/Info.plist"
cp "${script_dir}/assets/GPU Broker.icns" "${resources_dir}/GPU Broker.icns"
cp "${script_dir}/assets/ai-compute-studio-v1.png" "${resources_dir}/ai-compute-studio-v1.png"
plutil -lint "${app_bundle}/Contents/Info.plist" >/dev/null
xcrun --sdk macosx swiftc \
  -framework AppKit \
  -framework SwiftUI \
  "${swift_sources[@]}" \
  -o "${macos_dir}/GPU Broker"
touch "${app_bundle}"

if [[ -L "${root_app_entry}" ]]; then
  unlink "${root_app_entry}"
elif [[ -e "${root_app_entry}" ]]; then
  print -u2 "Refusing to replace non-symlink path: ${root_app_entry}"
  exit 1
fi

ln -s "dist/GPU Broker.app" "${root_app_entry}"

for legacy_app in "${project_root}"/GPU\ Broker\ <->.app(N); do
  if [[ -L "${legacy_app}" && "$(readlink "${legacy_app}")" == "dist/GPU Broker.app" ]]; then
    unlink "${legacy_app}"
  fi
done

echo "Built ${app_bundle}"
echo "Project entry ${root_app_entry}"
