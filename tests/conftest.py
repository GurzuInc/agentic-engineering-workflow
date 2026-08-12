from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from engineering_policy.bundle import Bundle

_V1_TAG = "v1.0.2"
_V1_SOURCE_COMMIT = "07e4190e65191eba762a79d5ede0be0cadecf060"
_V1_BUNDLE_SHA256 = "fa9651309a637a87ab04aa549751d40ca41c0b8ab8cafe3fb0b1db4c15784d4c"
_V1_UPDATER_SHA256 = "303306fcf78442725e0ccc6b27972f46361d998238e9419e672f52cb8cacd583"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def release_bundle(tmp_path_factory: pytest.TempPathFactory, project_root: Path) -> Path:
    output = tmp_path_factory.mktemp("release")
    subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/build_release.py"),
            "--output-dir",
            str(output),
            "--version",
            "2.0.0-rc.1",
            "--source-commit",
            "0123456789abcdef0123456789abcdef01234567",
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return output / "engineering-policy-2.0.0-rc.1.zip"


@pytest.fixture
def valid_bundle(release_bundle: Path) -> Bundle:
    return Bundle.load(release_bundle)


@pytest.fixture(scope="session")
def v1_release_assets(tmp_path_factory: pytest.TempPathFactory, project_root: Path) -> Path:
    fixture_root = tmp_path_factory.mktemp("v1-release")
    source = fixture_root / "source"
    source.mkdir()
    source_archive = fixture_root / "source.tar"
    resolved = subprocess.check_output(
        ["git", "rev-parse", f"{_V1_TAG}^{{commit}}"],
        cwd=project_root,
        text=True,
    ).strip()
    assert resolved == _V1_SOURCE_COMMIT
    subprocess.run(
        ["git", "archive", "--format=tar", "--output", str(source_archive), _V1_TAG],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["tar", "-xf", str(source_archive), "-C", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    output = fixture_root / "dist"
    subprocess.run(
        [
            sys.executable,
            str(source / "scripts/build_release.py"),
            "--output-dir",
            str(output),
            "--version",
            "1.0.2",
            "--source-commit",
            _V1_SOURCE_COMMIT,
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(source / "src"),
        },
    )
    bundle = output / "engineering-policy-1.0.2.zip"
    updater = output / "policyctl.pyz"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == _V1_BUNDLE_SHA256
    assert hashlib.sha256(updater.read_bytes()).hexdigest() == _V1_UPDATER_SHA256
    with zipfile.ZipFile(bundle) as archive:
        assert hashlib.sha256(archive.read("bin/policyctl.pyz")).hexdigest() == _V1_UPDATER_SHA256
    return output


@pytest.fixture(scope="session")
def v1_bundle(v1_release_assets: Path) -> Path:
    return v1_release_assets / "engineering-policy-1.0.2.zip"


@pytest.fixture
def v2_bundle(release_bundle: Path, mutate_bundle, project_root: Path) -> Path:
    return release_bundle


@pytest.fixture
def v2_stable_bundle(release_bundle: Path, mutate_bundle) -> Path:
    return mutate_bundle(release_bundle, version="2.0.0", channel="stable")


@pytest.fixture
def git_repo(tmp_path: Path) -> Iterator[Path]:
    repo = tmp_path / "consumer"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Policy Tests")
    _git(repo, "config", "user.email", "policy-tests@example.invalid")
    (repo / "README.md").write_text("# Consumer\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "chore: initialize test consumer")
    yield repo


@pytest.fixture
def mutate_bundle(tmp_path: Path) -> Callable[..., Path]:
    counter = 0

    def mutate(
        source: Path,
        *,
        replacements: dict[str, bytes] | None = None,
        additions: dict[str, tuple[bytes, int]] | None = None,
        removals: set[str] | None = None,
        mode_replacements: dict[str, int] | None = None,
        version: str | None = None,
        channel: str | None = None,
        manifest_updates: dict[str, object] | None = None,
        refresh_checksums: bool = True,
    ) -> Path:
        nonlocal counter
        counter += 1
        candidate_version = version or "2.0.0-rc.1"
        candidate_dir = tmp_path / f"candidate-{counter}"
        candidate_dir.mkdir()
        destination = candidate_dir / f"engineering-policy-{candidate_version}.zip"
        with zipfile.ZipFile(source) as archive:
            entries = {
                info.filename: (archive.read(info), info.external_attr)
                for info in archive.infolist()
            }
        manifest = json.loads(entries.pop("manifest.json")[0])
        for name, content in (replacements or {}).items():
            if name not in entries:
                raise AssertionError(f"replacement is not present: {name}")
            entries[name] = (content, entries[name][1])
        for name, (content, mode) in (additions or {}).items():
            entries[name] = (content, (mode & 0xFFFF) << 16)
            manifest["files"].append(
                {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "mode": f"{mode:04o}"}
            )
        for name in removals or set():
            if name not in entries:
                raise AssertionError(f"removal is not present: {name}")
            del entries[name]
            manifest["files"] = [item for item in manifest["files"] if item["path"] != name]
        for name, mode in (mode_replacements or {}).items():
            if name not in entries:
                raise AssertionError(f"mode replacement is not present: {name}")
            content, attributes = entries[name]
            entries[name] = (content, (attributes & ~(0o777 << 16)) | ((mode & 0o777) << 16))
        if version is not None:
            manifest["version"] = version
            policy = yaml.safe_load(entries["spec/policy.yaml"][0])
            policy["policy_version"] = version
            entries["spec/policy.yaml"] = (
                yaml.safe_dump(policy, sort_keys=False).encode(),
                entries["spec/policy.yaml"][1],
            )
        if channel is not None:
            manifest["channel"] = channel
        if manifest_updates:
            manifest.update(manifest_updates)
        if refresh_checksums:
            by_path = {item["path"]: item for item in manifest["files"]}
            for name, (content, _attributes) in entries.items():
                if name in by_path:
                    by_path[name]["sha256"] = hashlib.sha256(content).hexdigest()
        entries["manifest.json"] = (
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
            (0o644 & 0xFFFF) << 16,
        )
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, (content, attributes) in entries.items():
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = attributes
                archive.writestr(info, content)
        return destination

    return mutate


def commit_all(repo: Path, message: str = "chore: commit policy snapshot") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
