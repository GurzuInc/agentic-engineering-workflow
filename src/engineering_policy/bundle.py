from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from engineering_policy.constants import CANDIDATE_EXACT_FILES, CANDIDATE_PREFIXES
from engineering_policy.errors import PolicyError
from engineering_policy.repository import validate_relative_path
from engineering_policy.validation import (
    validate_adapters,
    validate_manifest_shape,
    validate_migrations,
    validate_policy,
)

_MAX_MEMBER_SIZE = 25 * 1024 * 1024
_MAX_BUNDLE_SIZE = 100 * 1024 * 1024


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Bundle:
    path: Path
    manifest: dict
    files: dict[str, bytes]
    digest: str

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def channel(self) -> str:
        return self.manifest["channel"]

    @classmethod
    def load(cls, path: Path) -> Bundle:
        if not path.is_file():
            raise PolicyError(f"bundle does not exist: {path}")
        members: dict[str, bytes] = {}
        member_modes: dict[str, str] = {}
        try:
            with zipfile.ZipFile(path) as archive:
                seen: set[str] = set()
                seen_portable: set[str] = set()
                total_size = 0
                for info in archive.infolist():
                    member = _validate_member(info)
                    if member in seen:
                        raise PolicyError(f"bundle contains duplicate path: {member}")
                    portable_key = member.casefold()
                    if portable_key in seen_portable:
                        raise PolicyError(f"bundle contains case-colliding portable path: {member}")
                    seen.add(member)
                    seen_portable.add(portable_key)
                    total_size += info.file_size
                    if info.file_size > _MAX_MEMBER_SIZE or total_size > _MAX_BUNDLE_SIZE:
                        raise PolicyError("bundle exceeds the allowed uncompressed size")
                    if info.is_dir():
                        continue
                    members[member] = archive.read(info)
                    member_modes[member] = f"{(info.external_attr >> 16) & 0o777:04o}"
        except zipfile.BadZipFile as exc:
            raise PolicyError(f"invalid release bundle: {exc}") from exc
        if "manifest.json" not in members:
            raise PolicyError("bundle is missing manifest.json")
        try:
            manifest = json.loads(members.pop("manifest.json"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyError(f"invalid bundle manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise PolicyError("bundle manifest must be an object")
        validate_manifest_shape(manifest)
        expected: dict[str, dict] = {}
        for item in manifest["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "mode"}:
                raise PolicyError("bundle manifest file entry is invalid")
            candidate = _validate_candidate_path(item["path"])
            if candidate in expected:
                raise PolicyError(f"manifest contains duplicate path: {candidate}")
            digest = item["sha256"]
            mode = item["mode"]
            if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
                raise PolicyError(f"manifest contains invalid checksum for {candidate}")
            if not isinstance(mode, str) or mode not in ("0644", "0755"):
                raise PolicyError(f"manifest contains invalid mode for {candidate}")
            if mode == "0755" and candidate != "bin/policyctl.pyz":
                raise PolicyError(f"only policyctl.pyz may be executable: {candidate}")
            expected[candidate] = item
        required_files = {path.as_posix() for path in CANDIDATE_EXACT_FILES}
        missing_required = sorted(required_files - expected.keys())
        if missing_required:
            raise PolicyError(
                f"bundle is missing required release files: {', '.join(missing_required)}"
            )
        if set(members) != set(expected):
            missing = sorted(set(expected) - set(members))
            extra = sorted(set(members) - set(expected))
            raise PolicyError(
                f"bundle contents differ from manifest; missing={missing}, extra={extra}"
            )
        for name, item in expected.items():
            if member_modes[name] != item["mode"]:
                raise PolicyError(f"archive mode differs from manifest for {name}")
            actual = sha256_bytes(members[name])
            if actual != item["sha256"]:
                raise PolicyError(f"checksum mismatch for {name}")
        policy = validate_policy(members, expected_protocol=manifest["protocol_version"])
        if policy["schema_version"] != manifest["schema_version"]:
            raise PolicyError("policy schema differs from the bundle manifest")
        if policy["policy_version"] != manifest["version"]:
            raise PolicyError("policy version differs from the bundle manifest")
        validate_migrations(members, protocol_version=manifest["protocol_version"])
        validate_adapters(members)
        return cls(path=path, manifest=manifest, files=members, digest=sha256_file(path))


def _validate_member(info: zipfile.ZipInfo) -> str:
    name = info.filename
    try:
        path = validate_relative_path(name)
    except PolicyError:
        raise PolicyError(f"bundle contains unsafe path: {name!r}")
    if info.flag_bits & 0x1:
        raise PolicyError(f"bundle contains encrypted member: {name}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise PolicyError(f"bundle contains symlink: {name}")
    kind = stat.S_IFMT(mode)
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise PolicyError(f"bundle contains a special file: {name}")
    return path.as_posix()


def _validate_candidate_path(raw: object) -> str:
    if not isinstance(raw, str):
        raise PolicyError("manifest path must be a string")
    try:
        path = validate_relative_path(raw)
    except PolicyError:
        raise PolicyError(f"candidate path is unsafe: {raw!r}")
    lowered = {part.casefold() for part in path.parts}
    if ".git" in lowered or "codeowners" in lowered:
        raise PolicyError(f"candidate path is protected: {raw}")
    if (
        path.parts[:2] == (".github", "workflows")
        or "workflows" in lowered
        and ".github" in lowered
    ):
        raise PolicyError(f"candidate workflow modification is forbidden: {raw}")
    if path.name in {"engineering-policy.project.yaml", "engineering-policy.project.md"}:
        raise PolicyError(f"candidate cannot modify the project overlay: {raw}")
    allowed = path in CANDIDATE_EXACT_FILES or any(
        path.is_relative_to(prefix) for prefix in CANDIDATE_PREFIXES
    )
    if not allowed:
        raise PolicyError(f"candidate path is outside the declarative allowlist: {raw}")
    return path.as_posix()
