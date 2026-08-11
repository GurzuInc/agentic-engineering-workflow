from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from engineering_policy.bundle import Bundle, sha256_bytes
from engineering_policy.constants import (
    BUNDLE_ASSET_TEMPLATE,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SOURCE_REPOSITORY,
    SUPPORTED_ADAPTERS,
    SUPPORTED_CODEOWNERS_MODES,
    SUPPORTED_GUIDANCE_MODES,
    SUPPORTED_SYNC_MODES,
)
from engineering_policy.errors import PolicyError
from engineering_policy.repository import (
    atomic_write,
    enumerate_regular_files,
    is_managed_output,
    read_regular_file,
    remove_managed_file,
    validate_relative_path,
)
from engineering_policy.semver import Version
from engineering_policy.validation import (
    load_json_bytes,
    validate_adapters,
    validate_migrations,
    validate_overlay,
    validate_policy,
    validate_trusted_schema,
    validate_with_schema,
)

_LOCK_PATH = PurePosixPath(".engineering-policy/policy.lock.json")
_SNAPSHOT_ROOT = PurePosixPath(".engineering-policy/spec")
_UPDATER_PATH = PurePosixPath(".engineering-policy/bin/policyctl.pyz")
_MAX_LOCK_SIZE = 1024 * 1024
_MAX_TRUSTED_SCHEMA_SIZE = 1024 * 1024
_MAX_UPDATER_SIZE = 32 * 1024 * 1024
_MAX_SNAPSHOT_FILE_SIZE = 8 * 1024 * 1024
_MAX_SNAPSHOT_TOTAL_SIZE = 64 * 1024 * 1024
_MAX_MANAGED_FILE_SIZE = 64 * 1024 * 1024


@dataclass(frozen=True)
class PreparedFile:
    content: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class VerifiedState:
    lock: dict
    snapshot_files: dict[str, bytes]
    expected_outputs: dict[str, PreparedFile]
    policy: dict


def normalize_adapters(raw: Iterable[str]) -> tuple[str, ...]:
    adapters = tuple(dict.fromkeys(raw))
    if not adapters:
        raise PolicyError("at least one adapter is required")
    unknown = sorted(set(adapters) - set(SUPPORTED_ADAPTERS))
    if unknown:
        raise PolicyError(f"unsupported adapters: {', '.join(unknown)}")
    return adapters


def prepare_bundle(bundle: Bundle, adapters: tuple[str, ...]) -> dict[str, PreparedFile]:
    adapters = normalize_adapters(adapters)
    prepared: dict[str, PreparedFile] = {}
    for source, content in sorted(bundle.files.items()):
        source_path = PurePosixPath(source)
        if source == "bin/policyctl.pyz":
            _add_prepared(
                prepared,
                ".engineering-policy/bin/policyctl.pyz",
                content,
                mode=0o755,
            )
            continue
        snapshot_source = (
            PurePosixPath(*source_path.parts[1:]) if source_path.parts[0] == "spec" else source_path
        )
        snapshot_path = PurePosixPath(".engineering-policy/spec") / snapshot_source
        _add_prepared(prepared, snapshot_path.as_posix(), content)
        if len(source_path.parts) >= 3 and source_path.parts[0] == "adapters":
            adapter = source_path.parts[1]
            if adapter in adapters:
                output_path = PurePosixPath(*source_path.parts[2:])
                rendered = _render_template(content, bundle.version, source)
                _add_prepared(prepared, output_path.as_posix(), rendered)
    for adapter in adapters:
        prefix = f"adapters/{adapter}/"
        if not any(name.startswith(prefix) for name in bundle.files):
            raise PolicyError(f"bundle does not contain requested adapter: {adapter}")
    _validate_prepared(prepared, adapters)
    return prepared


def expected_from_snapshot(repo: Path, lock: dict) -> dict[str, PreparedFile]:
    return _expected_from_snapshot_files(
        _snapshot_file_map(repo, _locked_snapshot_names(lock)), lock
    )


def _expected_from_snapshot_files(
    snapshot_files: dict[str, bytes], lock: dict
) -> dict[str, PreparedFile]:
    adapters = normalize_adapters(lock["adapters"])
    version = lock["version"]
    prepared: dict[str, PreparedFile] = {}
    for adapter in adapters:
        prefix = f"adapters/{adapter}/"
        sources = sorted(name for name in snapshot_files if name.startswith(prefix))
        if not sources:
            raise PolicyError(f"snapshot is missing adapter: {adapter}")
        for source in sources:
            relative = PurePosixPath(source.removeprefix(prefix))
            content = _render_template(snapshot_files[source], version, source)
            _add_prepared(prepared, relative.as_posix(), content)
    return prepared


def create_lock(
    bundle: Bundle,
    adapters: tuple[str, ...],
    prepared: dict[str, PreparedFile],
    *,
    guidance_mode: str,
    sync_mode: str,
    codeowners_mode: str,
) -> dict:
    version = Version.parse(bundle.version)
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "source_repository": SOURCE_REPOSITORY,
        "version": bundle.version,
        "channel": bundle.channel,
        "constraint": version.constraint,
        "adapters": list(adapters),
        "guidance_mode": guidance_mode,
        "sync_mode": sync_mode,
        "codeowners_mode": codeowners_mode,
        "bundle": {
            "asset": bundle.path.name,
            "sha256": bundle.digest,
        },
        "files": {path: sha256_bytes(file.content) for path, file in sorted(prepared.items())},
    }


def load_lock(repo: Path) -> dict:
    try:
        lock_content = _read_required_file(repo, _LOCK_PATH, "policy lock", _MAX_LOCK_SIZE)
    except PolicyError as exc:
        if " is missing:" in str(exc):
            raise PolicyError("policy lock is missing; run policyctl init") from exc
        raise
    lock = load_json_bytes(lock_content, str(repo / _LOCK_PATH))
    schema_relative = PurePosixPath(".engineering-policy/spec/schemas/lock.schema.json")
    schema_content = _read_required_file(
        repo, schema_relative, "lock schema", _MAX_TRUSTED_SCHEMA_SIZE
    )
    validate_trusted_schema(schema_content, "schemas/lock.schema.json")
    schema = load_json_bytes(schema_content, str(repo / schema_relative))
    validate_with_schema(lock, schema, "policy lock")
    _validate_lock_semantics(lock)
    return lock


def write_prepared(
    repo: Path, prepared: dict[str, PreparedFile], previous_lock: dict | None
) -> None:
    previous = set(previous_lock.get("files", {})) if previous_lock else set()
    for obsolete in sorted(previous - set(prepared), reverse=True):
        path = validate_relative_path(obsolete)
        if not is_managed_output(path):
            raise PolicyError(f"prior lock contains path outside managed allowlist: {path}")
        remove_managed_file(repo, path)
    for raw, file in sorted(prepared.items()):
        path = validate_relative_path(raw)
        atomic_write(repo, path, file.content, file.mode)


def write_lock(repo: Path, lock: dict) -> None:
    content = (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode()
    atomic_write(repo, PurePosixPath(".engineering-policy/policy.lock.json"), content)


def check_repository(repo: Path) -> list[str]:
    issues: list[str] = []
    try:
        state = _load_verified_state(repo)
    except PolicyError as exc:
        return [str(exc)]
    lock = state.lock
    try:
        overlay = validate_overlay(repo, state.policy)
        integration = overlay["integration"]
        for key in ("guidance_mode", "sync_mode", "codeowners_mode"):
            if integration[key] != lock[key]:
                issues.append(f"project overlay {key} differs from the policy lock")
    except PolicyError as exc:
        issues.append(str(exc))
    for required in ("engineering-policy.project.md",):
        try:
            _require_regular_file(repo, validate_relative_path(required), required)
        except PolicyError:
            issues.append(f"required consumer policy file is missing or unsafe: {required}")
    for raw, expected_digest in lock["files"].items():
        relative = validate_relative_path(raw)
        try:
            content = _read_existing_regular_file(repo, relative)
        except PolicyError:
            issues.append(f"managed file is missing or unsafe: {raw}")
            continue
        if content is None:
            issues.append(f"managed file is missing or unsafe: {raw}")
            continue
        actual = sha256_bytes(content)
        if actual != expected_digest:
            issues.append(f"managed file drift: {raw}")
        elif _existing_mode(repo, relative) != _required_mode(raw):
            issues.append(f"managed file mode drift: {raw}")
    for raw, file in state.expected_outputs.items():
        relative = validate_relative_path(raw)
        try:
            content = _read_existing_regular_file(repo, relative)
        except PolicyError:
            content = None
        if content != file.content:
            issues.append(f"deterministic render drift: {raw}")
    tracked_managed = set(lock["files"])
    try:
        for extra in sorted(_enumerate_managed_files(repo) - tracked_managed):
            issues.append(f"untracked file in managed namespace: {extra}")
    except PolicyError as exc:
        issues.append(str(exc))
    return sorted(set(issues))


def render_current(repo: Path) -> list[str]:
    state = _load_verified_state(repo)
    extras = sorted(_enumerate_managed_files(repo) - set(state.lock["files"]))
    if extras:
        raise PolicyError(f"unexpected file in managed namespace: {', '.join(extras)}")
    changes: dict[str, PreparedFile] = {}
    for raw, file in sorted(state.expected_outputs.items()):
        relative = validate_relative_path(raw)
        content = _read_existing_regular_file(repo, relative)
        if content != file.content or _existing_mode(repo, relative) != file.mode:
            changes[raw] = file
    for raw in sorted(state.lock["files"]):
        relative = validate_relative_path(raw)
        mode = _required_mode(raw)
        if _existing_mode(repo, relative) != mode:
            content = _read_existing_regular_file(repo, relative)
            if content is None:
                raise PolicyError(f"managed file is missing or unsafe: {raw}")
            changes[raw] = PreparedFile(content, mode)
    for raw, file in sorted(changes.items()):
        atomic_write(repo, validate_relative_path(raw), file.content, file.mode)
    return sorted(changes)


def _required_mode(raw: str) -> int:
    return 0o755 if raw == _UPDATER_PATH.as_posix() else 0o644


def _existing_mode(repo: Path, relative: PurePosixPath) -> int | None:
    path = repo.joinpath(*relative.parts)
    try:
        return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return None


def _snapshot_file_map(repo: Path, expected_names: set[str]) -> dict[str, bytes]:
    actual_names = enumerate_regular_files(repo, _SNAPSHOT_ROOT, label="policy snapshot")
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        raise PolicyError(
            f"policy snapshot file set mismatch; missing={missing}, unexpected={unexpected}"
        )
    files: dict[str, bytes] = {}
    total = 0
    for name in sorted(actual_names):
        relative = _SNAPSHOT_ROOT / validate_relative_path(name)
        content = _read_required_file(
            repo,
            relative,
            "policy snapshot file",
            _MAX_SNAPSHOT_FILE_SIZE,
        )
        total += len(content)
        if total > _MAX_SNAPSHOT_TOTAL_SIZE:
            raise PolicyError("policy snapshot exceeds the allowed aggregate size")
        files[name] = content
    return files


def _validate_lock_semantics(lock: dict) -> None:
    version = Version.parse(lock["version"])
    if lock["version"] != str(version):
        raise PolicyError("policy lock version is not canonical")
    if (lock["channel"] == "stable") != version.stable:
        raise PolicyError("policy lock channel does not match its version")
    if lock["constraint"] != version.constraint:
        raise PolicyError("policy lock constraint does not match its version")
    expected_asset = BUNDLE_ASSET_TEMPLATE.format(version=version)
    if lock["bundle"]["asset"] != expected_asset:
        raise PolicyError("policy lock bundle asset does not match its version")
    normalize_adapters(lock["adapters"])
    if lock["guidance_mode"] not in SUPPORTED_GUIDANCE_MODES:
        raise PolicyError("unsupported guidance mode in policy lock")
    if lock["sync_mode"] not in SUPPORTED_SYNC_MODES:
        raise PolicyError("unsupported sync mode in policy lock")
    if lock["codeowners_mode"] not in SUPPORTED_CODEOWNERS_MODES:
        raise PolicyError("unsupported CODEOWNERS mode in policy lock")
    for raw in lock["files"]:
        path = validate_relative_path(raw)
        if not is_managed_output(path) or path == _LOCK_PATH:
            raise PolicyError(f"policy lock contains path outside managed allowlist: {raw}")


def _load_verified_state(repo: Path) -> VerifiedState:
    lock = load_lock(repo)
    snapshot_files = _snapshot_file_map(repo, _locked_snapshot_names(lock))
    expected_outputs = _expected_from_snapshot_files(snapshot_files, lock)
    expected_files = {
        _UPDATER_PATH.as_posix(),
        *(f"{_SNAPSHOT_ROOT.as_posix()}/{name}" for name in snapshot_files),
        *expected_outputs,
    }
    actual_files = set(lock["files"])
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        unexpected = sorted(actual_files - expected_files)
        raise PolicyError(
            f"policy lock file set mismatch; missing={missing}, unexpected={unexpected}"
        )

    for name, content in snapshot_files.items():
        raw = f"{_SNAPSHOT_ROOT.as_posix()}/{name}"
        if lock["files"][raw] != sha256_bytes(content):
            raise PolicyError(f"policy snapshot checksum mismatch: {raw}")

    updater = _read_required_file(repo, _UPDATER_PATH, "trusted updater", _MAX_UPDATER_SIZE)
    if lock["files"][_UPDATER_PATH.as_posix()] != sha256_bytes(updater):
        raise PolicyError("trusted updater checksum mismatch")

    for raw, file in expected_outputs.items():
        if lock["files"][raw] != sha256_bytes(file.content):
            raise PolicyError(f"policy lock render checksum mismatch: {raw}")

    policy = validate_policy(snapshot_files)
    if policy["policy_version"] != lock["version"]:
        raise PolicyError("policy snapshot version differs from the policy lock")
    validate_migrations(snapshot_files)
    validate_adapters(snapshot_files)
    return VerifiedState(lock, snapshot_files, expected_outputs, policy)


def _render_template(content: bytes, version: str, label: str) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"adapter template is not UTF-8: {label}") from exc
    return text.replace("{{POLICY_VERSION}}", version).encode()


def _locked_snapshot_names(lock: dict) -> set[str]:
    prefix = f"{_SNAPSHOT_ROOT.as_posix()}/"
    return {raw.removeprefix(prefix) for raw in lock["files"] if raw.startswith(prefix)}


def _read_required_file(
    repo: Path,
    relative: PurePosixPath,
    label: str,
    maximum: int,
) -> bytes:
    content = read_regular_file(repo, relative, label=label, maximum=maximum)
    if content is None:  # pragma: no cover - required reads never allow a missing file
        raise PolicyError(f"{label} is missing: {relative}")
    return content


def _add_prepared(
    prepared: dict[str, PreparedFile], raw: str, content: bytes, mode: int = 0o644
) -> None:
    path = validate_relative_path(raw)
    if not is_managed_output(path):
        raise PolicyError(f"rendered path is outside managed allowlist: {raw}")
    key = path.as_posix()
    if key in prepared:
        raise PolicyError(f"render produced duplicate path: {key}")
    prepared[key] = PreparedFile(content=content, mode=mode)


def _validate_prepared(prepared: dict[str, PreparedFile], adapters: tuple[str, ...]) -> None:
    required = {
        ".engineering-policy/spec/policy.yaml",
        ".engineering-policy/bin/policyctl.pyz",
    }
    if "codex" in adapters:
        required.update(
            {
                ".codex/config.toml",
                ".agents/skills/project-engineering-workflow/SKILL.md",
                ".agents/skills/project-engineering-workflow/agents/openai.yaml",
            }
        )
    missing = required - prepared.keys()
    if missing:
        raise PolicyError(f"render is missing required outputs: {', '.join(sorted(missing))}")


def _path_without_symlinks(repo: Path, relative: PurePosixPath, label: str) -> Path:
    current = repo
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PolicyError(f"{label} has a symlinked path component: {relative}")
    return current


def _require_regular_file(repo: Path, relative: PurePosixPath, label: str) -> Path:
    path = _path_without_symlinks(repo, relative, label)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        if relative == _LOCK_PATH:
            raise PolicyError("policy lock is missing; run policyctl init") from exc
        raise PolicyError(f"{label} is missing: {relative}") from exc
    if not stat.S_ISREG(mode):
        raise PolicyError(f"{label} is not a regular file: {relative}")
    return path


def _read_existing_regular_file(repo: Path, relative: PurePosixPath) -> bytes | None:
    try:
        return read_regular_file(
            repo,
            relative,
            label="managed file",
            maximum=_MAX_MANAGED_FILE_SIZE,
            missing_ok=True,
        )
    except PolicyError as exc:
        if "exceeds the allowed size" in str(exc):
            return None
        raise


def _enumerate_managed_files(repo: Path) -> set[str]:
    found: set[str] = set()
    roots = [
        PurePosixPath(".engineering-policy/spec"),
        PurePosixPath(".engineering-policy/bin"),
        PurePosixPath(".codex"),
        PurePosixPath(".agents/skills/project-engineering-workflow"),
    ]
    for relative_root in roots:
        root = _path_without_symlinks(repo, relative_root, "managed namespace")
        if not root.exists():
            continue
        if not root.is_dir():
            found.add(relative_root.as_posix())
            continue
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(directory)
            for name in sorted(dirnames):
                path = current / name
                if path.is_symlink():
                    found.add(path.relative_to(repo).as_posix())
            for name in sorted(filenames):
                path = current / name
                found.add(path.relative_to(repo).as_posix())
    return found
