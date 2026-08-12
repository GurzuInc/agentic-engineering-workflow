from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

import pytest
import yaml
from conftest import commit_all

from engineering_policy.bundle import Bundle
from engineering_policy.errors import PolicyError
from engineering_policy.operations import apply_update, initialize
from engineering_policy.release import _verify_release
from engineering_policy.rendering import check_repository, load_lock
from engineering_policy.repository import atomic_write


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "/absolute.txt",
        "adapters/codex/.github/workflows/pwn.yml",
        "adapters/codex/.github/CODEOWNERS",
        "adapters/codex/engineering-policy.project.yaml",
        "outside-allowlist.txt",
        ".git/config",
    ],
)
def test_malicious_candidate_paths_are_rejected(
    release_bundle: Path, mutate_bundle, path: str
) -> None:
    malicious = mutate_bundle(release_bundle, additions={path: (b"malicious\n", 0o644)})
    with pytest.raises(PolicyError):
        Bundle.load(malicious)


def test_symlink_member_is_rejected(release_bundle: Path, mutate_bundle) -> None:
    malicious = mutate_bundle(
        release_bundle,
        additions={"adapters/codex/link": (b"../../outside", 0o120777)},
    )
    with pytest.raises(PolicyError, match="symlink"):
        Bundle.load(malicious)


def test_case_colliding_members_are_rejected(release_bundle: Path, mutate_bundle) -> None:
    malicious = mutate_bundle(
        release_bundle,
        additions={"SPEC/POLICY.YAML": (b"collision\n", 0o644)},
    )
    with pytest.raises(PolicyError, match="case-colliding"):
        Bundle.load(malicious)


def test_checksum_mismatch_is_rejected(release_bundle: Path, mutate_bundle) -> None:
    malicious = mutate_bundle(
        release_bundle,
        replacements={
            "adapters/codex/.agents/skills/project-engineering-workflow/SKILL.md": b"tampered\n"
        },
        refresh_checksums=False,
    )
    with pytest.raises(PolicyError, match="checksum mismatch"):
        Bundle.load(malicious)


def test_archive_mode_must_match_manifest(release_bundle: Path, mutate_bundle) -> None:
    malicious = mutate_bundle(
        release_bundle,
        mode_replacements={
            "adapters/codex/.agents/skills/project-engineering-workflow/SKILL.md": 0o755
        },
    )
    with pytest.raises(PolicyError, match="archive mode differs"):
        Bundle.load(malicious)


def test_candidate_cannot_loosen_trusted_protocol_schema(
    release_bundle: Path, mutate_bundle
) -> None:
    malicious = mutate_bundle(
        release_bundle,
        replacements={
            "schemas/policy.schema.json": b'{"$schema":"https://json-schema.org/draft/2020-12/schema"}\n'
        },
    )
    with pytest.raises(PolicyError, match="replace trusted protocol schema"):
        Bundle.load(malicious)


def test_bridge_accepts_only_the_complete_reviewed_v2_schema_set(v2_bundle: Path) -> None:
    bundle = Bundle.load(v2_bundle)
    assert bundle.manifest["schema_version"] == 2
    assert bundle.manifest["protocol_version"] == 2
    policy = yaml.safe_load(bundle.files["spec/policy.yaml"])
    assert policy["client_minimums"] == {"codex": "0.147.0"}
    assert policy["adapter_validation"] == {"codex": "validated", "claude": "pending"}


@pytest.mark.parametrize(
    ("minimums", "validation"),
    [
        ({"codex": "0.147.0", "claude": "1.2.3"}, "pending"),
        ({"codex": "0.147.0"}, "validated"),
    ],
)
def test_bridge_rejects_inconsistent_claude_minimum_and_validation_status(
    v2_bundle: Path, mutate_bundle, minimums: dict[str, str], validation: str
) -> None:
    with zipfile.ZipFile(v2_bundle) as archive:
        policy = yaml.safe_load(archive.read("spec/policy.yaml"))
    policy["client_minimums"] = minimums
    policy["adapter_validation"]["claude"] = validation
    malicious = mutate_bundle(
        v2_bundle,
        replacements={"spec/policy.yaml": yaml.safe_dump(policy, sort_keys=False).encode()},
        version="2.0.0",
        channel="stable",
        manifest_updates={"schema_version": 2, "protocol_version": 2},
    )
    with pytest.raises(PolicyError, match="schema violation"):
        Bundle.load(malicious)


def test_bridge_accepts_validated_claude_only_with_an_exact_minimum(
    v2_bundle: Path, mutate_bundle
) -> None:
    with zipfile.ZipFile(v2_bundle) as archive:
        policy = yaml.safe_load(archive.read("spec/policy.yaml"))
    policy["client_minimums"]["claude"] = "1.2.3"
    policy["adapter_validation"]["claude"] = "validated"
    candidate = mutate_bundle(
        v2_bundle,
        replacements={"spec/policy.yaml": yaml.safe_dump(policy, sort_keys=False).encode()},
        version="2.0.0",
        channel="stable",
        manifest_updates={"schema_version": 2, "protocol_version": 2},
    )
    Bundle.load(candidate)


@pytest.mark.parametrize(
    ("client", "minimum", "validation"),
    [
        ("codex", "latest", "pending"),
        ("codex", "0.147", "pending"),
        ("codex", "0.148.0", "pending"),
        ("claude", "*", "validated"),
        ("claude", ">=1.2.3", "validated"),
        ("claude", "01.2.3", "validated"),
        ("claude", "1.2.3-01", "validated"),
    ],
)
def test_bridge_rejects_nonexact_client_minimums(
    v2_bundle: Path,
    mutate_bundle,
    client: str,
    minimum: str,
    validation: str,
) -> None:
    with zipfile.ZipFile(v2_bundle) as archive:
        policy = yaml.safe_load(archive.read("spec/policy.yaml"))
    policy["client_minimums"][client] = minimum
    policy["adapter_validation"]["claude"] = validation
    malicious = mutate_bundle(
        v2_bundle,
        replacements={"spec/policy.yaml": yaml.safe_dump(policy, sort_keys=False).encode()},
        version="2.0.0",
        channel="stable",
        manifest_updates={"schema_version": 2, "protocol_version": 2},
    )
    with pytest.raises(PolicyError, match="schema violation"):
        Bundle.load(malicious)


def test_bridge_rejects_mixed_v1_and_v2_schema_sets(
    v2_bundle: Path, mutate_bundle, project_root: Path
) -> None:
    malicious = mutate_bundle(
        v2_bundle,
        replacements={
            "schemas/policy.schema.json": (
                project_root / "policy/schemas/policy.schema.json"
            ).read_bytes()
        },
    )
    with pytest.raises(PolicyError, match="replace trusted protocol schema"):
        Bundle.load(malicious)


def test_bridge_rejects_unexpected_schema_in_otherwise_valid_v2_set(
    v2_bundle: Path, mutate_bundle
) -> None:
    malicious = mutate_bundle(
        v2_bundle,
        additions={"schemas/unreviewed.schema.json": (b"{}\n", 0o644)},
    )
    with pytest.raises(PolicyError, match="trusted schema set mismatch"):
        Bundle.load(malicious)


def test_explicit_major_update_uses_bridge_without_executing_candidate_updater(
    git_repo: Path, valid_bundle: Bundle, v2_bundle: Path
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    commit_all(git_repo)
    candidate = Bundle.load(v2_bundle)
    with pytest.raises(PolicyError, match="automatic updates cannot cross"):
        apply_update(git_repo, candidate, explicit_version=False)
    apply_update(git_repo, candidate, explicit_version=True)
    assert load_lock(git_repo)["protocol_version"] == 2
    assert check_repository(git_repo) == []


def test_broken_migration_chain_is_rejected(release_bundle: Path, mutate_bundle) -> None:
    with zipfile.ZipFile(release_bundle) as archive:
        migration = json.loads(archive.read("migrations/0001-initial.json"))
    migration["from_protocol"] = 42
    malicious = mutate_bundle(
        release_bundle,
        replacements={
            "migrations/0001-initial.json": (
                json.dumps(migration, indent=2, sort_keys=True) + "\n"
            ).encode()
        },
    )
    with pytest.raises(PolicyError, match="migration chain is broken"):
        Bundle.load(malicious)


def test_adapter_cannot_gain_implicit_invocation_or_tools(
    release_bundle: Path, mutate_bundle
) -> None:
    codex_skill = (
        b"---\n"
        b"name: project-engineering-workflow\n"
        b"description: workflow\n"
        b"allowed-tools: shell\n"
        b"---\n"
    )
    malicious = mutate_bundle(
        release_bundle,
        replacements={
            "adapters/codex/.agents/skills/project-engineering-workflow/SKILL.md": codex_skill
        },
    )
    with pytest.raises(PolicyError, match="frontmatter may contain only"):
        Bundle.load(malicious)


def test_claude_adapter_cannot_enable_implicit_model_invocation(
    release_bundle: Path, mutate_bundle
) -> None:
    skill = (
        b"---\n"
        b"name: project-engineering-workflow\n"
        b"description: workflow\n"
        b"disable-model-invocation: false\n"
        b"---\n"
    )
    malicious = mutate_bundle(
        release_bundle,
        replacements={
            "adapters/claude/.claude/skills/project-engineering-workflow/SKILL.md": skill
        },
    )
    with pytest.raises(PolicyError, match="disable implicit"):
        Bundle.load(malicious)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tools", "Read, Grep, Glob, Write"),
        ("model", "claude-opus"),
        ("permissionMode", "acceptEdits"),
    ],
)
def test_claude_reviewer_must_remain_read_only(
    release_bundle: Path, mutate_bundle, field: str, value: str
) -> None:
    settings = {
        "tools": "Read, Grep, Glob",
        "model": "inherit",
        "permissionMode": "plan",
    }
    settings[field] = value
    reviewer = (
        "---\n"
        "name: project-contract-reviewer\n"
        "description: review\n"
        f"tools: {settings['tools']}\n"
        f"model: {settings['model']}\n"
        f"permissionMode: {settings['permissionMode']}\n"
        "---\n"
    ).encode()
    malicious = mutate_bundle(
        release_bundle,
        replacements={"adapters/claude/.claude/agents/project-contract-reviewer.md": reviewer},
    )
    with pytest.raises(PolicyError, match="not read-only"):
        Bundle.load(malicious)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"repository": "attacker/policy"}, "repository identity"),
        ({"protocol_version": 99}, "manual re-bootstrap"),
        ({"schema_version": 99}, "schema is unsupported"),
        ({"channel": "stable"}, "channel does not match"),
    ],
)
def test_manifest_identity_and_protocol_fail_closed(
    release_bundle: Path, mutate_bundle, updates: dict, message: str
) -> None:
    malicious = mutate_bundle(release_bundle, manifest_updates=updates)
    with pytest.raises(PolicyError, match=message):
        Bundle.load(malicious)


@pytest.mark.parametrize(
    ("version", "protocol", "schema"),
    [
        ("1.0.1", 2, 2),
        ("2.0.0", 1, 1),
    ],
)
def test_bundle_version_major_must_match_protocol(
    release_bundle: Path,
    mutate_bundle,
    project_root: Path,
    version: str,
    protocol: int,
    schema: int,
) -> None:
    replacements = {}
    if protocol == 2:
        policy = yaml.safe_load((project_root / "policy/spec/policy.yaml").read_text())
        policy.update(
            {
                "schema_version": 2,
                "protocol_version": 2,
                "policy_version": version,
                "client_minimums": {"codex": "0.147.0"},
                "adapter_validation": {"codex": "validated", "claude": "pending"},
            }
        )
        migration_0001 = json.loads(
            (project_root / "policy/migrations/0001-initial.json").read_text()
        )
        migration_0001["schema_version"] = 2
        replacements = {
            "spec/policy.yaml": yaml.safe_dump(policy, sort_keys=False).encode(),
            "migrations/0001-initial.json": (
                json.dumps(migration_0001, indent=2, sort_keys=True) + "\n"
            ).encode(),
            **{
                f"schemas/{path.name}": path.read_bytes()
                for path in sorted((project_root / "policy/trusted-schemas/v2").glob("*.json"))
            },
        }
    malicious = mutate_bundle(
        release_bundle,
        replacements=replacements,
        version=version,
        channel="stable",
        manifest_updates={"schema_version": schema, "protocol_version": protocol},
    )
    with pytest.raises(PolicyError, match="version major does not match"):
        Bundle.load(malicious)


def test_manifest_and_policy_versions_must_agree(release_bundle: Path, mutate_bundle) -> None:
    malicious = mutate_bundle(
        release_bundle,
        manifest_updates={"version": "1.0.0-rc.4"},
    )
    with pytest.raises(PolicyError, match="policy version differs"):
        Bundle.load(malicious)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"version": []}, "invalid semantic version"),
        ({"channel": []}, "channel is invalid"),
    ],
)
def test_malformed_manifest_values_fail_with_policy_error(
    release_bundle: Path, mutate_bundle, updates: dict, message: str
) -> None:
    malicious = mutate_bundle(release_bundle, manifest_updates=updates)
    with pytest.raises(PolicyError, match=message):
        Bundle.load(malicious)


@pytest.mark.parametrize(
    "path",
    [
        "adapters/codex/cafe\u0301.md",
        "adapters/codex/CON.txt",
        "adapters/codex/name:stream",
        "adapters/codex/trailing.",
        "adapters/codex/trailing ",
        "adapters/codex/control\x1f.md",
    ],
)
def test_candidate_paths_must_be_portable(release_bundle: Path, mutate_bundle, path: str) -> None:
    malicious = mutate_bundle(release_bundle, additions={path: (b"unsafe\n", 0o644)})
    with pytest.raises(PolicyError, match="unsafe path|path is unsafe"):
        Bundle.load(malicious)


def test_invalid_release_attestation_blocks_before_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = [tmp_path / name for name in ("bundle.zip", "policyctl.pyz", "manifest", "sums")]
    for artifact in artifacts:
        artifact.write_bytes(b"candidate")
    monkeypatch.setattr("engineering_policy.release.shutil.which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        "engineering_policy.release.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "invalid attestation"),
    )
    with pytest.raises(PolicyError, match="invalid attestation"):
        _verify_release("v1.0.0-rc.3", *artifacts)


def test_release_verification_uses_all_provenance_gates_and_strips_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = [tmp_path / name for name in ("bundle.zip", "policyctl.pyz", "manifest", "sums")]
    for artifact in artifacts:
        artifact.write_bytes(b"candidate")
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, "verified", "")

    monkeypatch.setattr("engineering_policy.release.shutil.which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr("engineering_policy.release.subprocess.run", record)
    monkeypatch.setenv("GH_TOKEN", "write-capable")
    monkeypatch.setenv("GITHUB_TOKEN", "write-capable")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "enterprise-write-capable")
    monkeypatch.setenv("GITHUB_ENTERPRISE_TOKEN", "enterprise-actions-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/ambient/gh-config")
    _verify_release("v1.0.0-rc.3", *artifacts)
    commands = [" ".join(call[0]) for call in calls]
    assert any("release verify v1.0.0-rc.3" in command for command in commands)
    assert sum("release verify-asset" in command for command in commands) == 4
    assert sum("attestation verify" in command for command in commands) == 4
    assert sum("--source-ref refs/tags/v1.0.0-rc.3" in command for command in commands) == 4
    assert all("GH_TOKEN" not in environment for _command, environment in calls)
    assert all("GITHUB_TOKEN" not in environment for _command, environment in calls)
    assert all("GH_ENTERPRISE_TOKEN" not in environment for _command, environment in calls)
    assert all("GITHUB_ENTERPRISE_TOKEN" not in environment for _command, environment in calls)
    assert all(environment["GH_HOST"] == "github.com" for _command, environment in calls)
    assert all(
        environment["GH_CONFIG_DIR"] != "/ambient/gh-config" for _command, environment in calls
    )


def test_release_verification_accepts_only_explicit_read_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = [tmp_path / name for name in ("bundle.zip", "policyctl.pyz", "manifest", "sums")]
    for artifact in artifacts:
        artifact.write_bytes(b"candidate")
    environments = []

    def record(command, **kwargs):
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "verified", "")

    monkeypatch.setattr("engineering_policy.release.shutil.which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr("engineering_policy.release.subprocess.run", record)
    monkeypatch.setenv("GH_TOKEN", "write-token")
    monkeypatch.setenv("POLICYCTL_GITHUB_READ_TOKEN", "read-token")
    _verify_release("v1.0.0-rc.3", *artifacts)
    assert all(environment["GH_TOKEN"] == "read-token" for environment in environments)  # noqa: S105
    assert all("POLICYCTL_GITHUB_READ_TOKEN" not in environment for environment in environments)


def test_release_verification_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = [tmp_path / name for name in ("bundle.zip", "policyctl.pyz", "manifest", "sums")]
    for artifact in artifacts:
        artifact.write_bytes(b"candidate")
    monkeypatch.setattr("engineering_policy.release.shutil.which", lambda _name: "/usr/bin/gh")

    def time_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("engineering_policy.release.subprocess.run", time_out)
    with pytest.raises(PolicyError, match="verification timed out"):
        _verify_release("v1.0.0-rc.3", *artifacts)


def test_candidate_updater_is_copied_but_never_executed(
    git_repo: Path, valid_bundle: Bundle, release_bundle: Path, mutate_bundle, tmp_path: Path
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    commit_all(git_repo)
    marker = tmp_path / "candidate-executed"
    candidate_program = (
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
    ).encode()
    candidate_path = mutate_bundle(
        release_bundle,
        replacements={"bin/policyctl.pyz": candidate_program},
        version="1.0.0-rc.3",
        channel="prerelease",
    )
    apply_update(git_repo, Bundle.load(candidate_path), explicit_version=False)
    assert not marker.exists()
    assert (git_repo / ".engineering-policy/bin/policyctl.pyz").read_bytes() == candidate_program


def test_update_refuses_dirty_worktree(
    git_repo: Path, valid_bundle: Bundle, release_bundle: Path, mutate_bundle
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    commit_all(git_repo)
    (git_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    candidate = Bundle.load(
        mutate_bundle(
            release_bundle,
            version="1.0.0-rc.3",
            channel="prerelease",
        )
    )
    with pytest.raises(PolicyError, match="worktree must be clean"):
        apply_update(git_repo, candidate, explicit_version=False)


def test_managed_atomic_write_is_bound_to_open_parent_directory(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path, tmp_path: Path
) -> None:
    managed = git_repo / ".codex"
    managed.mkdir()
    (managed / "config.toml").write_text("old\n", encoding="utf-8")
    moved = git_repo / ".codex-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.toml").write_text("outside\n", encoding="utf-8")

    from engineering_policy import repository as repository_module

    original_replace = repository_module._atomic_replace_at

    def swap_before_replace(parent_fd, name, content, mode, label):
        managed.rename(moved)
        managed.symlink_to(outside, target_is_directory=True)
        return original_replace(parent_fd, name, content, mode, label)

    monkeypatch.setattr(repository_module, "_atomic_replace_at", swap_before_replace)
    atomic_write(git_repo, PurePosixPath(".codex/config.toml"), b"new\n")

    assert (outside / "config.toml").read_text(encoding="utf-8") == "outside\n"
    assert (moved / "config.toml").read_text(encoding="utf-8") == "new\n"


def test_release_build_rejects_symlinked_output_root(project_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "linked-output"
    output.symlink_to(outside, target_is_directory=True)
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/build_release.py"),
            "--output-dir",
            str(output),
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert list(outside.iterdir()) == []


def test_release_build_does_not_follow_checksum_sidecar_symlink(
    project_root: Path, tmp_path: Path
) -> None:
    output = tmp_path / "output"
    subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/build_release.py"),
            "--output-dir",
            str(output),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    sidecar = output / "SHA256SUMS"
    sidecar.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    sidecar.symlink_to(outside)

    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/build_release.py"),
            "--output-dir",
            str(output),
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert outside.read_text(encoding="utf-8") == "outside\n"
