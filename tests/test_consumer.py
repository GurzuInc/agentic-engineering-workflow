from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from conftest import commit_all

from engineering_policy.bundle import Bundle
from engineering_policy.cli import main
from engineering_policy.errors import PolicyError
from engineering_policy.operations import apply_update, initialize
from engineering_policy.rendering import check_repository, normalize_adapters, render_current


def _read_lock(repo: Path) -> dict:
    return json.loads((repo / ".engineering-policy/policy.lock.json").read_text())


def _write_lock(repo: Path, lock: dict) -> None:
    (repo / ".engineering-policy/policy.lock.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_init_preserves_guidance_and_unmanaged_repository_files(
    git_repo: Path, valid_bundle: Bundle
) -> None:
    agents = git_repo / "AGENTS.md"
    agents.write_bytes(b"# Repository-owned guidance\n")
    codeowners = git_repo / ".github/CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_bytes(b"/src/ @product-team\n")
    commit_all(git_repo, "chore: add repository policy")
    agents_digest = hashlib.sha256(agents.read_bytes()).hexdigest()
    owners_digest = hashlib.sha256(codeowners.read_bytes()).hexdigest()

    changed = initialize(git_repo, valid_bundle, ("codex",))

    assert hashlib.sha256(agents.read_bytes()).hexdigest() == agents_digest
    assert hashlib.sha256(codeowners.read_bytes()).hexdigest() == owners_digest
    assert ".agents/skills/project-engineering-workflow/SKILL.md" in changed
    assert not (git_repo / ".github/workflows/engineering-policy-sync.yml").exists()
    assert not (git_repo / ".claude").exists()
    assert not (git_repo / "CLAUDE.md").exists()
    lock = _read_lock(git_repo)
    assert lock["adapters"] == ["codex"]
    assert lock["guidance_mode"] == "preserve"
    assert lock["sync_mode"] == "manual"
    assert lock["codeowners_mode"] == "unmanaged"
    overlay = yaml.safe_load((git_repo / "engineering-policy.project.yaml").read_text())
    assert overlay["integration"] == {
        "guidance_mode": "preserve",
        "sync_mode": "manual",
        "codeowners_mode": "unmanaged",
    }
    assert check_repository(git_repo) == []


def test_supported_cli_init_composes_verified_release_and_preserves_guidance(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path, valid_bundle: Bundle
) -> None:
    agents = git_repo / "AGENTS.md"
    agents.write_bytes(b"# Repository-owned guidance\n")
    commit_all(git_repo, "chore: add repository guidance")
    before = hashlib.sha256(agents.read_bytes()).hexdigest()

    @contextmanager
    def verified_bundle(_client, version):
        assert str(version) == "1.0.0-rc.2"
        yield valid_bundle

    monkeypatch.setattr("engineering_policy.cli.ReleaseClient.verified_bundle", verified_bundle)
    result = main(
        [
            "init",
            "--repo",
            str(git_repo),
            "--version",
            "1.0.0-rc.2",
            "--adapters",
            "codex",
            "--guidance-mode",
            "preserve",
            "--sync-mode",
            "manual",
            "--codeowners-mode",
            "unmanaged",
        ]
    )
    assert result == 0
    assert hashlib.sha256(agents.read_bytes()).hexdigest() == before
    assert check_repository(git_repo) == []


def test_claude_is_rejected_as_unsupported() -> None:
    with pytest.raises(PolicyError, match="unsupported adapters: claude"):
        normalize_adapters(("claude",))


def test_init_rejects_managed_codex_collision(git_repo: Path, valid_bundle: Bundle) -> None:
    config = git_repo / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text("model = 'repository-model'\n", encoding="utf-8")
    commit_all(git_repo, "chore: add Codex configuration")
    with pytest.raises(PolicyError, match=".codex"):
        initialize(git_repo, valid_bundle, ("codex",))
    assert config.read_text() == "model = 'repository-model'\n"
    assert not (git_repo / ".engineering-policy").exists()


def test_init_refuses_dirty_worktree(git_repo: Path, valid_bundle: Bundle) -> None:
    (git_repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="worktree must be clean"):
        initialize(git_repo, valid_bundle, ("codex",))


def test_check_detects_and_render_repairs_generated_drift(
    git_repo: Path, valid_bundle: Bundle
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    skill = git_repo / ".agents/skills/project-engineering-workflow/SKILL.md"
    original = skill.read_bytes()
    skill.write_text("drift\n", encoding="utf-8")
    assert any(str(skill.relative_to(git_repo)) in issue for issue in check_repository(git_repo))
    assert render_current(git_repo) == [".agents/skills/project-engineering-workflow/SKILL.md"]
    assert skill.read_bytes() == original
    assert check_repository(git_repo) == []


def test_check_detects_and_render_repairs_managed_mode_drift(
    git_repo: Path, valid_bundle: Bundle
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    updater = git_repo / ".engineering-policy/bin/policyctl.pyz"
    updater.chmod(0o644)
    assert check_repository(git_repo) == [
        "managed file mode drift: .engineering-policy/bin/policyctl.pyz"
    ]
    assert render_current(git_repo) == [".engineering-policy/bin/policyctl.pyz"]
    assert updater.stat().st_mode & 0o777 == 0o755
    assert check_repository(git_repo) == []


def test_check_rejects_overlay_mode_drift(git_repo: Path, valid_bundle: Bundle) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    overlay_path = git_repo / "engineering-policy.project.yaml"
    overlay = yaml.safe_load(overlay_path.read_text())
    overlay["integration"]["sync_mode"] = "automatic"
    overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
    assert any("schema violation" in issue for issue in check_repository(git_repo))


def test_update_preserves_modes_guidance_codeowners_and_overlay(
    git_repo: Path, valid_bundle: Bundle, release_bundle: Path, mutate_bundle
) -> None:
    agents = git_repo / "AGENTS.md"
    agents.write_bytes(b"# Owned\n")
    codeowners = git_repo / ".github/CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_bytes(b"* @team\n")
    commit_all(git_repo, "chore: add owned files")
    initialize(git_repo, valid_bundle, ("codex",))
    commit_all(git_repo)
    overlay = git_repo / "engineering-policy.project.yaml"
    original_overlay = overlay.read_text() + "# repository comment\n"
    overlay.write_text(original_overlay, encoding="utf-8")
    commit_all(git_repo, "chore: customize overlay")
    candidate = Bundle.load(
        mutate_bundle(release_bundle, version="1.1.0-rc.1", channel="prerelease")
    )

    apply_update(git_repo, candidate, explicit_version=False)

    assert agents.read_bytes() == b"# Owned\n"
    assert codeowners.read_bytes() == b"* @team\n"
    assert overlay.read_text() == original_overlay
    lock = _read_lock(git_repo)
    assert (lock["guidance_mode"], lock["sync_mode"], lock["codeowners_mode"]) == (
        "preserve",
        "manual",
        "unmanaged",
    )
    assert check_repository(git_repo) == []


def test_same_snapshot_update_is_idempotent(git_repo: Path, valid_bundle: Bundle) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    commit_all(git_repo)
    assert apply_update(git_repo, valid_bundle, explicit_version=True) == []
    assert check_repository(git_repo) == []


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("channel", "stable", "channel does not match"),
        ("constraint", "2.x", "constraint does not match"),
        ("guidance_mode", "replace", "schema violation"),
        ("sync_mode", "automatic", "schema violation"),
        ("codeowners_mode", "managed", "schema violation"),
    ],
)
def test_lock_identity_and_modes_fail_closed(
    git_repo: Path, valid_bundle: Bundle, key: str, value: str, message: str
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    lock = _read_lock(git_repo)
    lock[key] = value
    _write_lock(git_repo, lock)
    with pytest.raises(PolicyError, match=message):
        render_current(git_repo)


def test_lock_and_snapshot_policy_versions_must_agree(git_repo: Path, valid_bundle: Bundle) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    policy_path = git_repo / ".engineering-policy/spec/policy.yaml"
    content = policy_path.read_text().replace(
        "policy_version: 1.0.0-rc.2", "policy_version: 1.0.0-rc.3"
    )
    policy_path.write_text(content, encoding="utf-8")
    lock = _read_lock(git_repo)
    lock["files"][".engineering-policy/spec/policy.yaml"] = hashlib.sha256(
        content.encode()
    ).hexdigest()
    _write_lock(git_repo, lock)
    with pytest.raises(PolicyError, match="snapshot version differs"):
        render_current(git_repo)


@pytest.mark.parametrize(
    "unsafe_path",
    ["../outside", ".codex/../outside", ".github/workflows/unsafe.yml", "AGENTS.md"],
)
def test_lock_rejects_unsafe_or_unmanaged_paths(
    git_repo: Path, valid_bundle: Bundle, unsafe_path: str
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    lock = _read_lock(git_repo)
    lock["files"][unsafe_path] = "0" * 64
    _write_lock(git_repo, lock)
    with pytest.raises(PolicyError, match="unsafe consumer path|outside managed allowlist"):
        render_current(git_repo)


def test_unexpected_managed_file_blocks_check_and_render(
    git_repo: Path, valid_bundle: Bundle
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    unexpected = git_repo / ".codex/unexpected.toml"
    unexpected.write_text("unexpected\n", encoding="utf-8")
    assert any("untracked file" in issue for issue in check_repository(git_repo))
    with pytest.raises(PolicyError, match="unexpected"):
        render_current(git_repo)


@pytest.mark.parametrize("tamper", ["snapshot", "updater"])
def test_render_verifies_immutable_digests_before_repair(
    git_repo: Path, valid_bundle: Bundle, tamper: str
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    immutable = (
        git_repo / ".engineering-policy/spec/policy.yaml"
        if tamper == "snapshot"
        else git_repo / ".engineering-policy/bin/policyctl.pyz"
    )
    immutable.write_bytes(immutable.read_bytes() + b"tampered\n")
    with pytest.raises(PolicyError, match="checksum mismatch"):
        render_current(git_repo)


def test_snapshot_symlink_is_rejected(git_repo: Path, valid_bundle: Bundle, tmp_path: Path) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    source = git_repo / ".engineering-policy/spec"
    outside = tmp_path / "snapshot"
    source.rename(outside)
    source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PolicyError, match="symlinked path component"):
        render_current(git_repo)
