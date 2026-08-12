#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import yaml
from build_policyctl import _normalize_newlines
from build_policyctl import build as build_policyctl

from engineering_policy.bundle import Bundle, sha256_bytes
from engineering_policy.constants import PROTOCOL_VERSION, SCHEMA_VERSION, SOURCE_REPOSITORY
from engineering_policy.repository import atomic_write_path
from engineering_policy.semver import Version

_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_SUPPORTED_PYTHON = ("3.11", "3.12", "3.13", "3.14")
_RECOVERY_SOURCE_COMMIT = "eb79ceb19b1e82ac5f504170ab56abc20e4f484a"
_RECOVERY_BUNDLE_SHA256 = "181bc71c4087640685d7bfd8cc575e90afa68a876dc6e50c8a66a0d4f3496ca2"
_RECOVERY_UPDATER_SHA256 = "7c2e39091d855a66e8f30fde232a447e916b1b04a8d5ad826fd6a7dd052f3376"


def build_release(
    root: Path,
    output_dir: Path,
    *,
    version: str | None = None,
    source_commit: str | None = None,
) -> tuple[Path, Path, Path, Path]:
    source_policy = (root / "policy/spec/policy.yaml").read_bytes()
    policy = yaml.safe_load(source_policy)
    release_version = str(Version.parse(version or policy["policy_version"]))
    commit = source_commit or _source_commit(root)
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ValueError("source commit must be a full lowercase Git SHA")
    output_dir.mkdir(parents=True, exist_ok=True)
    policyctl = build_policyctl(
        output_dir / "policyctl.pyz",
        root,
        version=release_version,
    )
    files: dict[str, tuple[bytes, str]] = {}
    _collect(files, root / "policy/spec", "spec")
    _collect(files, root / "policy/schemas", "schemas")
    _collect(files, root / "policy/prompts", "prompts")
    _collect(files, root / "policy/migrations", "migrations")
    _collect(files, root / "policy/adapters", "adapters")
    files["spec/policy.yaml"] = (_versioned_policy(source_policy, release_version), "0644")
    files["LICENSE"] = (_normalize_newlines((root / "LICENSE").read_bytes()), "0644")
    files["NOTICE"] = (_normalize_newlines((root / "NOTICE").read_bytes()), "0644")
    files["bin/policyctl.pyz"] = (policyctl.read_bytes(), "0755")
    channel = "stable" if Version.parse(release_version).stable else "prerelease"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "repository": SOURCE_REPOSITORY,
        "version": release_version,
        "channel": channel,
        "files": [
            {"path": name, "sha256": sha256_bytes(content), "mode": mode}
            for name, (content, mode) in sorted(files.items())
        ],
    }
    bundle = output_dir / f"engineering-policy-{release_version}.zip"
    archive_entries = dict(files)
    archive_entries["manifest.json"] = (
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        "0644",
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (content, mode) in sorted(archive_entries.items()):
            _write_zip_entry(archive, name, content, mode)
    atomic_write_path(bundle, stream.getvalue())
    Bundle.load(bundle)
    if Bundle.load(bundle).files["bin/policyctl.pyz"] != policyctl.read_bytes():
        raise ValueError("embedded updater differs from standalone updater")

    release_manifest = output_dir / "release-manifest.json"
    release_data = {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "repository": SOURCE_REPOSITORY,
        "version": release_version,
        "tag": f"v{release_version}",
        "channel": channel,
        "source_commit": commit,
        "asset_digests": {
            bundle.name: sha256_bytes(bundle.read_bytes()),
            policyctl.name: sha256_bytes(policyctl.read_bytes()),
        },
        "supported_adapters": ["codex", "claude"],
        "adapter_validation": policy["adapter_validation"],
        "supported_python": list(_SUPPORTED_PYTHON),
        "recovery": {
            "original_source_commit": _RECOVERY_SOURCE_COMMIT,
            "original_source_is_ancestor": False,
            "unpublished_rc1_bundle_sha256": _RECOVERY_BUNDLE_SHA256,
            "unpublished_rc1_updater_sha256": _RECOVERY_UPDATER_SHA256,
            "unpublished_rc1_was_tagged": False,
            "failed_unpublished_candidates": ["v1.0.0-rc.2"],
        },
    }
    atomic_write_path(
        release_manifest,
        (json.dumps(release_data, indent=2, sort_keys=True) + "\n").encode(),
    )
    checksums = output_dir / "SHA256SUMS"
    checksum_targets = sorted((bundle, policyctl, release_manifest), key=lambda path: path.name)
    atomic_write_path(
        checksums,
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in checksum_targets
        ).encode(),
    )
    return policyctl, bundle, release_manifest, checksums


def _source_commit(root: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise ValueError("Git is required to resolve the source commit")
    result = subprocess.run(  # noqa: S603 - Git is resolved from PATH and arguments are fixed
        [git, "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _versioned_policy(content: bytes, version: str) -> bytes:
    normalized = _normalize_newlines(content)
    rendered, count = re.subn(
        rb"(?m)^policy_version: .+$",
        f"policy_version: {version}".encode(),
        normalized,
        count=1,
    )
    if count != 1:
        raise ValueError("policy version marker is missing or ambiguous")
    return rendered


def _collect(files: dict[str, tuple[bytes, str]], source: Path, prefix: str) -> None:
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        if path.is_symlink() or "__pycache__" in path.parts:
            raise ValueError(f"policy source must be regular and deterministic: {path}")
        relative = path.relative_to(source).as_posix()
        files[f"{prefix}/{relative}"] = (_normalize_newlines(path.read_bytes()), "0644")


def _write_zip_entry(archive: zipfile.ZipFile, name: str, content: bytes, mode: str) -> None:
    info = zipfile.ZipInfo(name, _FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (int(mode, 8) & 0xFFFF) << 16
    archive.writestr(info, content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--version")
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifacts = build_release(
        root,
        args.output_dir.absolute(),
        version=args.version,
        source_commit=args.source_commit,
    )
    for artifact in artifacts:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
