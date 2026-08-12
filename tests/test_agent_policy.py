from __future__ import annotations

import stat
from pathlib import Path

import pytest

from serverpilot.mcp_server import mcp
from scripts.install_agent_policy import MARKERS, install, main, merge, render
from scripts.install_agent_policy import POLICY


def _plain_policy_text(text: str) -> str:
    return " ".join(text.replace("`", "").split())


def test_policy_render_is_marked_for_each_platform() -> None:
    for platform in ("codex", "claude", "cursor"):
        start, end = MARKERS[platform]
        output = render(platform, "# shared policy")
        assert output.startswith(start)
        assert output.endswith(f"{end}\n")
        assert "# shared policy" in output


def test_policy_merge_replaces_only_its_owned_block() -> None:
    old = "before\n\n<!-- SERVERPILOT_GLOBAL_START -->\nold\n<!-- SERVERPILOT_GLOBAL_END -->\n\nafter\n"
    merged = merge(old, render("codex", "new"))
    assert merged == "before\n\n<!-- SERVERPILOT_GLOBAL_START -->\nnew\n<!-- SERVERPILOT_GLOBAL_END -->\nafter\n"


def test_policy_merge_migrates_legacy_gpu_broker_block_in_place() -> None:
    old = "before\n\n<!-- GPU_BROKER_GLOBAL_START -->\nlegacy\n<!-- GPU_BROKER_GLOBAL_END -->\n\nafter\n"
    merged = merge(old, render("codex", "new"))
    assert merged == "before\n\n<!-- SERVERPILOT_GLOBAL_START -->\nnew\n<!-- SERVERPILOT_GLOBAL_END -->\nafter\n"


def test_policy_merge_is_idempotent() -> None:
    block = render("codex", "new")
    once = merge("before\nafter\n", block)
    assert merge(once, block) == once


def test_policy_merge_into_empty_file_is_just_the_owned_block() -> None:
    block = render("codex", "new")
    assert merge("", block) == block


@pytest.mark.parametrize(
    "existing, message",
    [
        ("<!-- SERVERPILOT_GLOBAL_START -->\nmissing end", "incomplete"),
        ("<!-- SERVERPILOT_GLOBAL_END -->\nmissing start", "incomplete"),
        (
            "<!-- SERVERPILOT_GLOBAL_END -->\n<!-- SERVERPILOT_GLOBAL_START -->",
            "malformed",
        ),
        (
            "<!-- SERVERPILOT_GLOBAL_START -->\none\n<!-- SERVERPILOT_GLOBAL_END -->\n"
            "<!-- SERVERPILOT_GLOBAL_START -->\ntwo\n<!-- SERVERPILOT_GLOBAL_END -->",
            "duplicated",
        ),
    ],
)
def test_policy_merge_rejects_invalid_markers(existing: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        merge(existing, render("codex", "new"))


def test_policy_merge_rejects_mixed_legacy_and_current_blocks() -> None:
    existing = (
        "<!-- GPU_BROKER_GLOBAL_START -->\nlegacy\n<!-- GPU_BROKER_GLOBAL_END -->\n"
        "<!-- SERVERPILOT_GLOBAL_START -->\ncurrent\n<!-- SERVERPILOT_GLOBAL_END -->\n"
    )
    with pytest.raises(ValueError, match="duplicated"):
        merge(existing, render("codex", "new"))


def test_cli_requires_exactly_one_action() -> None:
    with pytest.raises(SystemExit) as missing:
        main(["codex"])
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as conflicting:
        main(["codex", "--print", "--install"])
    assert conflicting.value.code == 2


def test_print_is_labeled_and_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    assert main(["all", "--print"]) == 0

    output = capsys.readouterr().out
    assert "[codex] rendered policy" in output
    assert "[claude] rendered policy" in output
    assert "[cursor] rendered policy" in output
    assert not (tmp_path / "codex-home" / "AGENTS.md").exists()
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


def test_cursor_print_is_paste_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["cursor", "--print"]) == 0
    output = capsys.readouterr().out
    assert output.startswith(MARKERS["cursor"][0])


def test_global_policy_describes_the_no_setup_routine_gpu_path() -> None:
    adapter = _plain_policy_text(POLICY.read_text(encoding="utf-8")).lower()
    for boundary in (
        "use the local serverpilot mcp",
        "gpu_status",
        "include_busy=true",
        "gpu_apply",
        "gpu_count",
        "no_capacity",
        "gpus[]",
        "cuda_visible_devices",
        "workspace_path",
        "gpu_release",
        "codex_thread_id",
        "codex://threads/<uuid>",
        "ssh",
        "sqlite",
        "inventory",
        "nvidia-smi",
    ):
        assert boundary in adapter

    assert len(adapter.split()) < 180
    for removed_routine_step in (
        "gpu_bind_observed_workload",
        "gpu_renew_lease",
        "gpu_coordination",
        "idempotency_key",
        "heartbeat",
    ):
        assert removed_routine_step not in adapter

    mcp_instructions = _plain_policy_text(mcp.instructions).lower()
    for runtime_contract in (
        "三个工具",
        "gpu_status",
        "gpu_apply",
        "cuda_visible_devices",
        "workspace_path",
        "gpu_release",
        "include_busy=true",
        "agent_url",
        "codex_thread_id",
        "无容量直接失败，不排队",
    ):
        assert runtime_contract in mcp_instructions
    assert len(mcp.instructions) < 512
    for removed_routine_step in (
        "gpu_bind_observed_workload",
        "gpu_renew_lease",
        "gpu_coordination",
        "idempotency_key",
        "approval_ref",
    ):
        assert removed_routine_step not in mcp_instructions


def test_global_policy_keeps_scheduler_detail_out_of_routine_mcp_help() -> None:
    global_policy = _plain_policy_text(POLICY.read_text(encoding="utf-8")).lower()
    assert "advanced compatibility tools are outside the routine path" in global_policy

    mcp_instructions = _plain_policy_text(mcp.instructions).lower()
    assert "scheduler" not in mcp_instructions
    assert "advanced" not in mcp_instructions


def test_install_refuses_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    target = tmp_path / "actual.md"
    target.write_text("keep me\n", encoding="utf-8")
    (codex_home / "AGENTS.md").symlink_to(target)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(ValueError, match="refusing to replace symlink"):
        install("codex", "new policy")

    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert (codex_home / "AGENTS.md").is_symlink()


def test_install_preserves_existing_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy_path = codex_home / "AGENTS.md"
    policy_path.write_text("existing\n", encoding="utf-8")
    policy_path.chmod(0o640)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    install("codex", "new policy")

    assert stat.S_IMODE(policy_path.stat().st_mode) == 0o640


def test_install_all_labels_results_and_explains_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    assert main(["all", "--install"]) == 0

    output = capsys.readouterr().out
    assert "[codex] installed:" in output
    assert "[claude] installed:" in output
    assert "[cursor] not installed; use --print cursor" in output
    assert (tmp_path / "codex-home" / "AGENTS.md").is_file()
    assert (tmp_path / ".claude" / "CLAUDE.md").is_file()


def test_cursor_install_is_rejected_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(SystemExit) as error:
        main(["cursor", "--install"])

    assert error.value.code == 2
    assert "[cursor] install is manual" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
