#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
app_bundle="${1:-${project_root}/ServerPilot.app}"
result_dir="${2:-${project_root}/build/native-ui-acceptance}"
executable="${app_bundle}/Contents/MacOS/ServerPilot"
fixture_dir="${script_dir}/Fixtures"
validator="/Users/jinplu/.codex/skills/build-gpu-broker-native-ui/scripts/validate_snapshot_contract.py"

if [[ ! -x "${executable}" ]]; then
  print -u2 "Missing executable app: ${executable}"
  exit 2
fi
if [[ ! -x "${validator}" ]]; then
  print -u2 "Missing fixture contract validator: ${validator}"
  exit 2
fi

mkdir -p "${result_dir}/screenshots" "${result_dir}/logs" "${result_dir}/ax"
: > "${result_dir}/commands.log"

run_capture() {
  local name=$1 fixture=$2 section=$3 viewport=$4
  shift 4
  local screenshot="${result_dir}/screenshots/${name}.png"
  local command_log="${result_dir}/logs/${name}.log"
  print -r -- "fixture=${fixture} section=${section} viewport=${viewport} args=$*" >> "${result_dir}/commands.log"
  env \
    SERVERPILOT_DESKTOP_FIXTURE="${fixture}" \
    SERVERPILOT_DESKTOP_SECTION="${section}" \
    SERVERPILOT_DESKTOP_VIEWPORT="${viewport}" \
    SERVERPILOT_DESKTOP_SCREENSHOT="${screenshot}" \
    SERVERPILOT_DESKTOP_EXIT_AFTER_SCREENSHOT=1 \
    SERVERPILOT_CLI=/usr/bin/false \
    "${executable}" "$@" > "${command_log}" 2>&1
  [[ -s "${screenshot}" ]] || {
    print -u2 "Screenshot was not produced: ${screenshot}"
    exit 3
  }
}

for viewport in 1024x640 1280x800 1440x820; do
  run_capture "servers-${viewport}" resource-ownership server-pool "${viewport}"
  run_capture "usage-${viewport}" resource-ownership resource-usage "${viewport}"
  run_capture "settings-${viewport}" resource-ownership settings "${viewport}"
done
run_capture empty-1024x640 0 server-pool 1024x640
run_capture error-1024x640 error server-pool 1024x640
run_capture forced-light-1280x800 resource-ownership server-pool 1280x800 -NSRequiresAquaSystemAppearance YES
run_capture system-dark-request-1280x800 resource-ownership server-pool 1280x800 -AppleInterfaceStyle Dark
run_capture high-contrast-1280x800 resource-ownership server-pool 1280x800 -AppleIncreaseContrast YES
run_capture reduce-motion-1280x800 resource-ownership server-pool 1280x800 -AppleReduceMotion YES
run_capture system-dark-high-contrast-request-1280x800 resource-ownership server-pool 1280x800 \
  -AppleInterfaceStyle Dark -AppleIncreaseContrast YES

"${validator}" --golden "${fixture_dir}/contract-healthy-snapshot.json" \
  > "${result_dir}/fixture-contract-validation.json"

{
  xcode-select -p
  xcodebuild -version
  xcrun --find xctest
} > "${result_dir}/logs/xctest-toolchain.log" 2>&1 || true

collect_ax() {
  local name=$1 fixture=$2 section=$3
  local app_log="${result_dir}/logs/ax-${name}.app.log"
  env \
    SERVERPILOT_DESKTOP_FIXTURE="${fixture}" \
    SERVERPILOT_DESKTOP_SECTION="${section}" \
    SERVERPILOT_DESKTOP_VIEWPORT=1280x800 \
    SERVERPILOT_CLI=/usr/bin/false \
    "${executable}" > "${app_log}" 2>&1 &
  local app_pid=$!
  local ready=false
  for _attempt in {1..50}; do
    if /usr/bin/osascript -e 'on run argv' \
      -e 'tell application "System Events" to return exists (first process whose unix id is (item 1 of argv as integer))' \
      -e 'end run' \
      "${app_pid}" \
      2>/dev/null | grep -q true; then
      ready=true
      break
    fi
    sleep 0.1
  done
  if [[ "${ready}" != true ]]; then
    kill "${app_pid}" 2>/dev/null || true
    wait "${app_pid}" 2>/dev/null || true
    print -u2 "Accessibility process did not appear for ${name}"
    return 4
  fi
  sleep 0.8

  local ax_status=0
  /usr/bin/osascript - "${app_pid}" > "${result_dir}/ax/${name}.txt" 2> "${result_dir}/ax/${name}.err" <<'APPLESCRIPT' || ax_status=$?
on run argv
set targetPID to item 1 of argv as integer
tell application "System Events"
  tell (first process whose unix id is targetPID)
    set frontmost to true
    set elements to entire contents of window 1
    set output to "element_count=" & (count of elements) & linefeed
    repeat with itemRef in elements
      try
        set roleValue to (role of itemRef) as text
      on error
        set roleValue to "?"
      end try
      try
        set identifierValue to (value of attribute "AXIdentifier" of itemRef) as text
      on error
        set identifierValue to "?"
      end try
      try
        set titleValue to (value of attribute "AXTitle" of itemRef) as text
      on error
        set titleValue to "?"
      end try
      try
        set helpValue to (value of attribute "AXHelp" of itemRef) as text
      on error
        set helpValue to "?"
      end try
      try
        set valueValue to (value of attribute "AXValue" of itemRef) as text
      on error
        set valueValue to "?"
      end try
      try
        set enabledValue to (value of attribute "AXEnabled" of itemRef) as text
      on error
        set enabledValue to "?"
      end try
      if titleValue is not "?" or helpValue is not "?" or valueValue is not "?" then
        set output to output & roleValue & tab & identifierValue & tab & titleValue & tab & helpValue & tab & valueValue & tab & enabledValue & linefeed
      end if
    end repeat
    return output
  end tell
end tell
end run
APPLESCRIPT
  kill "${app_pid}" 2>/dev/null || true
  wait "${app_pid}" 2>/dev/null || true
  return "${ax_status}"
}

collect_ax servers resource-ownership server-pool
collect_ax usage resource-ownership resource-usage
collect_ax settings resource-ownership settings
collect_ax empty 0 server-pool
collect_ax error error server-pool

env \
  SERVERPILOT_DESKTOP_FIXTURE=resource-ownership \
  SERVERPILOT_DESKTOP_SECTION=server-pool \
  SERVERPILOT_DESKTOP_VIEWPORT=1280x800 \
  SERVERPILOT_CLI=/usr/bin/false \
  "${executable}" -AppleKeyboardUIMode 3 > "${result_dir}/logs/keyboard.app.log" 2>&1 &
keyboard_pid=$!
for _attempt in {1..50}; do
  /usr/bin/osascript -e 'on run argv' \
    -e 'tell application "System Events" to return exists (first process whose unix id is (item 1 of argv as integer))' \
    -e 'end run' \
    "${keyboard_pid}" \
    2>/dev/null | grep -q true && break
  sleep 0.1
done
sleep 0.8
/usr/bin/osascript - "${keyboard_pid}" > "${result_dir}/ax/keyboard-focus.txt" 2> "${result_dir}/ax/keyboard-focus.err" <<'APPLESCRIPT'
on run argv
set targetPID to item 1 of argv as integer
tell application "System Events"
  tell (first process whose unix id is targetPID)
    set frontmost to true
    set output to ""
    repeat with stepIndex from 1 to 12
      key code 48
      delay 0.04
      set focusedSummary to "none"
      try
        set itemRef to value of attribute "AXFocusedUIElement"
        set focusedRole to (role of itemRef) as text
        try
          set focusedHelp to (value of attribute "AXHelp" of itemRef) as text
        on error
          set focusedHelp to "?"
        end try
        try
          set focusedValue to (value of attribute "AXValue" of itemRef) as text
        on error
          set focusedValue to "?"
        end try
        set focusedSummary to focusedRole & tab & focusedHelp & tab & focusedValue
      end try
      set output to output & stepIndex & tab & focusedSummary & linefeed
    end repeat
    keystroke "r" using command down
    delay 0.1
    return output & "command-r=sent" & linefeed
  end tell
end tell
end run
APPLESCRIPT
kill "${keyboard_pid}" 2>/dev/null || true
wait "${keyboard_pid}" 2>/dev/null || true

python3 - "${project_root}" "${app_bundle}" "${result_dir}" <<'PY'
import json
import pathlib
import re
import struct
import sys

project_root, app_bundle, result_dir = map(pathlib.Path, sys.argv[1:])
screenshots = []
pixels_by_name = {}
for path in sorted((result_dir / "screenshots").glob("*.png")):
    data = path.read_bytes()
    pixels_by_name[path.stem] = data
    size = struct.unpack(">II", data[16:24]) if data.startswith(b"\x89PNG\r\n\x1a\n") else None
    screenshots.append({
        "name": path.stem,
        "path": str(path),
        "pixels": list(size) if size else None,
    })

expected_names = {
    *(f"{surface}-{viewport}" for surface in ("servers", "usage", "settings") for viewport in ("1024x640", "1280x800", "1440x820")),
    "empty-1024x640",
    "error-1024x640",
    "forced-light-1280x800",
    "system-dark-request-1280x800",
    "high-contrast-1280x800",
    "reduce-motion-1280x800",
    "system-dark-high-contrast-request-1280x800",
}
by_name = {item["name"]: item for item in screenshots}
missing = sorted(expected_names - by_name.keys())
dimension_errors = []
for name, item in by_name.items():
    match = re.search(r"(1024x640|1280x800|1440x820)$", name)
    if not match:
        continue
    expected = [int(value) for value in match.group(1).split("x")]
    if item["pixels"] != expected:
        dimension_errors.append({"name": name, "expected": expected, "actual": item["pixels"]})

ui_test_source = project_root / "desktop/UITests/FixtureModeUITests.swift"
declared_tests = re.findall(r"\bfunc\s+(test[A-Za-z0-9_]+)\s*\(", ui_test_source.read_text(encoding="utf-8"))
ax_files = sorted(str(path) for path in (result_dir / "ax").glob("*.txt"))
ax_text = {
    name: (result_dir / "ax" / f"{name}.txt").read_text(encoding="utf-8", errors="replace")
    for name in ("servers", "usage", "settings", "empty", "error")
}
semantic_checks = {
    "servers_navigation_and_sort_ax": all(
        token in ax_text["servers"]
        for token in ("服务器", "使用情况", "设置", "测试数据不能刷新", "按GPU 利用排序")
    ),
    "usage_project_task_ax": all(
        token in ax_text["usage"]
        for token in ("使用情况", "vision-lab", "4 个任务", "当前使用")
    ),
    "settings_contract_ax": all(
        token in ax_text["settings"]
        for token in ("设置", "本机服务地址", "数据更新间隔", "版本")
    ),
    "empty_state_ax": "暂无资源" in ax_text["empty"],
    "connection_error_ax": any(
        token in ax_text["error"]
        for token in ("连接失败", "连接或更新超时", "更新中断")
    ),
    "no_internal_agent_identity_ax": not any(
        token in "\n".join(ax_text.values())
        for token in ("Agent", "agent-trainer", "__serverpilot_system__")
    ),
}
keyboard_text = (result_dir / "ax/keyboard-focus.txt").read_text(encoding="utf-8", errors="replace")
keyboard_rows = [line for line in keyboard_text.splitlines() if re.match(r"^\d+\t", line)]
keyboard_focus_summaries = {line.split("\t", 1)[1] for line in keyboard_rows if not line.endswith("\tnone")}
semantic_checks["keyboard_focus_traversal"] = len(keyboard_focus_summaries) >= 3 and "command-r=sent" in keyboard_text

appearance_checks = {
    "light_only_policy_ignores_system_dark_request": pixels_by_name.get("forced-light-1280x800") == pixels_by_name.get("system-dark-request-1280x800"),
    "high_contrast_injection_changed_static_pixels": pixels_by_name.get("high-contrast-1280x800") != pixels_by_name.get("forced-light-1280x800"),
    "reduce_motion_static_screenshot_is_not_behavioral_proof": None,
}
payload = {
    "artifact_integrity_ok": not missing and not dimension_errors,
    "evidence_scope": "fake desktop fixtures only",
    "app_bundle": str(app_bundle),
    "fixture_contract_validation": str(result_dir / "fixture-contract-validation.json"),
    "screenshots": screenshots,
    "missing_screenshots": missing,
    "dimension_errors": dimension_errors,
    "appearance_variants": {
        "light_policy": "the app must stay Aqua/light even when launched with -AppleInterfaceStyle Dark",
        "forced_light_reference": "captured with -NSRequiresAquaSystemAppearance YES",
        "system_dark_request": "captured with -AppleInterfaceStyle Dark; this is a resistance test, not dark-mode support",
        "high_contrast": "requested with -AppleIncreaseContrast YES",
        "reduce_motion": "requested with -AppleReduceMotion YES",
    },
    "appearance_checks": appearance_checks,
    "accessibility_dumps": ax_files,
    "semantic_checks": semantic_checks,
    "acceptance_gaps": [
        "No XCTest/XCUITest execution or xcresult is possible without full Xcode.",
        "A static screenshot cannot prove Reduce Motion behavior.",
        "High Contrast is not accepted unless the recorded pixel comparison changes or Accessibility Inspector evidence is added.",
        "Confirmation dialogs are mutation-gated and are not exercised by the read-only fixture provider.",
        "Detail/inspector interaction remains declared in XCUITest but is not executed without Xcode.",
    ],
    "declared_xcui_test_count": len(declared_tests),
    "declared_xcui_tests": declared_tests,
    "xctest_execution": {
        "executed": 0,
        "reason": "Full Xcode/xcodebuild and xctest are unavailable; see logs/xctest-toolchain.log",
    },
}
(result_dir / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

print(json.dumps({"artifact_integrity_ok": payload["artifact_integrity_ok"], "screenshots": len(screenshots), "declared_xcui_tests": len(declared_tests), "missing": missing, "dimension_errors": dimension_errors, "semantic_checks": semantic_checks, "appearance_checks": appearance_checks}, ensure_ascii=False))
raise SystemExit(0 if payload["artifact_integrity_ok"] else 5)
PY
