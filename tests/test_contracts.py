from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import yaml

from engineering_policy.bundle import Bundle
from engineering_policy.cli import build_parser
from engineering_policy.constants import TRUSTED_SCHEMA_SETS_SHA256
from engineering_policy.operations import initialize
from engineering_policy.validation import validate_adapters, validate_migrations, validate_policy


def test_public_cli_surface_is_exact() -> None:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    assert set(subparsers.choices) == {"init", "render", "check", "doctor", "update"}
    init = subparsers.choices["init"]
    assert {flag for action in init._actions for flag in action.option_strings} == {
        "-h",
        "--help",
        "--repo",
        "--version",
        "--adapters",
        "--guidance-mode",
        "--sync-mode",
        "--codeowners-mode",
    }
    assert "--adopt-existing" not in init.format_help()
    doctor = subparsers.choices["doctor"]
    client = next(action for action in doctor._actions if action.dest == "client")
    assert client.choices == ("codex", "claude", "all")
    assert client.default == "all"


def test_repository_formats_parse(project_root: Path) -> None:
    for path in project_root.rglob("*.toml"):
        if ".git" not in path.parts:
            tomllib.loads(path.read_text(encoding="utf-8"))
    for path in project_root.rglob("*.json"):
        if ".git" not in path.parts:
            json.loads(path.read_text(encoding="utf-8"))
    for path in project_root.rglob("*.yml"):
        if ".git" not in path.parts:
            yaml.safe_load(path.read_text(encoding="utf-8"))
    for path in project_root.rglob("*.yaml"):
        if ".git" not in path.parts:
            yaml.safe_load(path.read_text(encoding="utf-8"))


def test_reviewed_v2_schema_sources_match_embedded_trusted_digests(project_root: Path) -> None:
    schema_root = project_root / "policy/trusted-schemas/v2"
    expected = TRUSTED_SCHEMA_SETS_SHA256[2]
    assert {f"schemas/{path.name}" for path in schema_root.glob("*.json")} == set(expected)
    for name, digest in expected.items():
        assert hashlib.sha256((schema_root / Path(name).name).read_bytes()).hexdigest() == digest


def test_all_actions_are_sha_pinned_and_github_hosted(project_root: Path) -> None:
    workflows = list((project_root / ".github/workflows").glob("*.yml"))
    assert workflows
    pattern = re.compile(r"^\s+(?:-\s+)?uses: [^@\s]+@([a-f0-9]{40})(?:\s+#.*)?$", re.MULTILINE)
    for workflow in workflows:
        content = workflow.read_text(encoding="utf-8")
        uses = [line for line in content.splitlines() if "uses:" in line]
        assert len(pattern.findall(content)) == len(uses)
        assert "runs-on: self-hosted" not in content


def test_release_permissions_and_draft_first_contract(project_root: Path) -> None:
    workflow = yaml.safe_load((project_root / ".github/workflows/release.yml").read_text())
    publish = workflow["jobs"]["publish"]
    assert publish["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }
    steps = [step.get("name", "") for step in publish["steps"]]
    assert (
        steps.index("Create draft and upload every asset")
        < steps.index("Attest every release asset")
        < steps.index("Publish the immutable release")
    )
    text = (project_root / ".github/workflows/release.yml").read_text()
    for asset in (
        "engineering-policy-*.zip",
        "policyctl.pyz",
        "release-manifest.json",
        "SHA256SUMS",
    ):
        assert asset in text
    assert "--draft" in text
    assert "--draft=false" in text
    assert "IMMUTABLE_RELEASES_ENABLED" in text


def test_release_requires_exact_rc_promotion_and_immutable_preflight_before_draft(
    project_root: Path,
) -> None:
    workflow = yaml.safe_load((project_root / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    by_name = {step.get("name"): step for step in steps if step.get("name")}
    names = [step.get("name", "") for step in steps]
    assert (
        names.index("Require stable promotion evidence")
        < names.index("Require recorded immutable-release preflight")
        < names.index("Create draft and upload every asset")
    )
    stable = by_name["Require stable promotion evidence"]
    assert stable["if"] == "${{ !contains(github.ref_name, '-') }}"
    script = stable["run"]
    assert 'test "$RC_SMOKE_RESULT" = "$rc_tag:pass:$GITHUB_SHA"' in script
    assert 'git rev-parse "refs/tags/$rc_tag^{commit}"' in script
    assert 'gh release verify "$rc_tag"' in script
    assert 'releases/tags/$rc_tag" --jq .immutable' in script
    assert "find rc-assets -maxdepth 1 -type f" in script
    assert "sha256sum --check SHA256SUMS" in script
    assert "jq -r .source_commit" in script
    assert 'gh attestation verify "$asset"' in script
    assert stable["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "RC_SMOKE_RESULT": "${{ vars.RC_SMOKE_RESULT }}",
    }
    preflight = by_name["Require recorded immutable-release preflight"]
    assert preflight["env"] == {
        "IMMUTABLE_RELEASES_ENABLED": "${{ vars.IMMUTABLE_RELEASES_ENABLED }}"
    }
    assert preflight["run"] == 'test "$IMMUTABLE_RELEASES_ENABLED" = true'
    assert "gh api" not in preflight["run"]


def test_ci_and_release_require_two_identical_builds(project_root: Path) -> None:
    for name in ("ci.yml", "release.yml"):
        text = (project_root / ".github/workflows" / name).read_text()
        assert "dist/one" in text
        assert "dist/two" in text
        assert "diff -r --no-dereference" in text


def test_ci_requires_cross_python_release_identity(project_root: Path) -> None:
    workflow = yaml.safe_load((project_root / ".github/workflows/ci.yml").read_text())
    assert workflow["jobs"]["cross-python-determinism"]["needs"] == "test"
    text = (project_root / ".github/workflows/ci.yml").read_text()
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert f"deterministic-python-{version}" in text


def test_bundle_contract_contains_codex_and_claude_adapters(
    project_root: Path, release_bundle: Path
) -> None:
    bundle = Bundle.load(release_bundle)
    validate_policy(bundle.files)
    validate_migrations(bundle.files)
    validate_adapters(bundle.files)
    names = set(bundle.files)
    assert any(name.startswith("adapters/codex/") for name in names)
    assert any(name.startswith("adapters/claude/") for name in names)
    assert not any("codeowners" in name.casefold() for name in names)
    assert not any(".github/workflows" in name.casefold() for name in names)
    assert "adapters/codex/.agents/skills/project-engineering-workflow/SKILL.md" in names
    assert "adapters/claude/.claude/skills/project-engineering-workflow/SKILL.md" in names
    assert "adapters/claude/CLAUDE.md" not in names


def test_release_policy_prompts_and_adapters_are_consumer_product_neutral(
    project_root: Path, release_bundle: Path
) -> None:
    banned = ("publisher-lms", "publisher lms", "f00")
    bundle = Bundle.load(release_bundle)
    checked = {
        name: content
        for name, content in bundle.files.items()
        if name == "spec/policy.yaml" or name.startswith("prompts/") or name.startswith("adapters/")
    }
    assert checked
    for name, content in checked.items():
        text = content.decode("utf-8").casefold()
        assert not any(term in text for term in banned), name


def test_release_build_is_deterministic(project_root: Path, tmp_path: Path) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    outputs = [tmp_path / "one", tmp_path / "two"]
    for output in outputs:
        subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts/build_release.py"),
                "--output-dir",
                str(output),
                "--version",
                "2.0.0-rc.1",
                "--source-commit",
                commit,
            ],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
    names = {
        "engineering-policy-2.0.0-rc.1.zip",
        "policyctl.pyz",
        "release-manifest.json",
        "SHA256SUMS",
    }
    assert {path.name for path in outputs[0].iterdir()} == names
    for name in names:
        assert (outputs[0] / name).read_bytes() == (outputs[1] / name).read_bytes()


def test_release_manifest_and_checksums_are_exact(release_bundle: Path, project_root: Path) -> None:
    output = release_bundle.parent
    manifest = json.loads((output / "release-manifest.json").read_text())
    assert manifest["version"] == "2.0.0-rc.1"
    assert manifest["tag"] == "v2.0.0-rc.1"
    assert manifest["source_commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert manifest["supported_adapters"] == ["codex", "claude"]
    assert manifest["adapter_validation"] == {"codex": "validated", "claude": "pending"}
    assert manifest["supported_python"] == ["3.11", "3.12", "3.13", "3.14"]
    assert manifest["recovery"]["original_source_commit"] == (
        "eb79ceb19b1e82ac5f504170ab56abc20e4f484a"
    )
    assert manifest["recovery"]["original_source_is_ancestor"] is False
    assert manifest["recovery"]["unpublished_rc1_was_tagged"] is False
    assert manifest["recovery"]["failed_unpublished_candidates"] == ["v1.0.0-rc.2"]
    expected = {}
    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        expected[name] = digest
    assert set(expected) == {
        "engineering-policy-2.0.0-rc.1.zip",
        "policyctl.pyz",
        "release-manifest.json",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert manifest["asset_digests"] == {
        "engineering-policy-2.0.0-rc.1.zip": expected["engineering-policy-2.0.0-rc.1.zip"],
        "policyctl.pyz": expected["policyctl.pyz"],
    }
    assert (project_root / "RECOVERY.md").is_file()


def test_archives_have_fixed_metadata_and_identical_updaters(release_bundle: Path) -> None:
    with zipfile.ZipFile(release_bundle) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert (
            archive.read("bin/policyctl.pyz")
            == (release_bundle.parent / "policyctl.pyz").read_bytes()
        )
        assert archive.read("LICENSE")
        assert archive.read("NOTICE")
    with zipfile.ZipFile(release_bundle.parent / "policyctl.pyz") as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


def test_notice_contains_complete_pinned_dependency_licenses(project_root: Path) -> None:
    notice = (project_root / "NOTICE").read_text(encoding="utf-8")
    for distribution_name, expected_version in (
        ("PyYAML", "6.0.3"),
        ("fastjsonschema", "2.22.1"),
    ):
        distribution = metadata.distribution(distribution_name)
        assert distribution.version == expected_version
        license_files = [
            file for file in distribution.files or () if str(file).endswith("licenses/LICENSE")
        ]
        assert len(license_files) == 1
        license_text = Path(distribution.locate_file(license_files[0])).read_text(encoding="utf-8")
        assert f"{distribution_name} {expected_version}" in notice
        assert license_text.rstrip() in notice


def test_built_zipapp_is_self_contained(release_bundle: Path, tmp_path: Path) -> None:
    bundle = Bundle.load(release_bundle)
    policyctl = tmp_path / "policyctl.pyz"
    policyctl.write_bytes(bundle.files["bin/policyctl.pyz"])
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(policyctl), "--help"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": ""},
    )
    assert "policyctl" in result.stdout


def test_built_updater_checks_an_initialized_repository(
    git_repo: Path, valid_bundle: Bundle, release_bundle: Path
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(release_bundle.parent / "policyctl.pyz"),
            "check",
            "--repo",
            str(git_repo),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": str(Path(git).parent)},
    )
    assert result.stdout.startswith("OK policy snapshot is conformant")
