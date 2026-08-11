from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath

import yaml

from engineering_policy.bundle import Bundle
from engineering_policy.constants import (
    SUPPORTED_CODEOWNERS_MODES,
    SUPPORTED_GUIDANCE_MODES,
    SUPPORTED_SYNC_MODES,
)
from engineering_policy.errors import PolicyError
from engineering_policy.rendering import (
    check_repository,
    create_lock,
    load_lock,
    normalize_adapters,
    prepare_bundle,
    write_lock,
    write_prepared,
)
from engineering_policy.repository import (
    atomic_write_protected,
    require_clean_worktree,
    require_git_repository,
)
from engineering_policy.semver import Version

_PROJECT_YAML_PATH = PurePosixPath("engineering-policy.project.yaml")
_PROJECT_MD_PATH = PurePosixPath("engineering-policy.project.md")


def initialize(
    repo: Path,
    bundle: Bundle,
    adapters: tuple[str, ...],
    *,
    guidance_mode: str = "preserve",
    sync_mode: str = "manual",
    codeowners_mode: str = "unmanaged",
) -> list[str]:
    repo = require_git_repository(repo)
    require_clean_worktree(repo)
    adapters = normalize_adapters(adapters)
    _validate_modes(guidance_mode, sync_mode, codeowners_mode)
    prepared = prepare_bundle(bundle, adapters)
    managed_root = repo / ".engineering-policy"
    if managed_root.exists() or managed_root.is_symlink():
        raise PolicyError(".engineering-policy already exists; use policyctl update")
    _check_bootstrap_collisions(repo, prepared)
    project_markdown = _project_markdown(adapters)
    project_yaml = {
        "schema_version": 1,
        "repository": {
            "name": repo.name,
            "description": "Repository-owned engineering policy overlay.",
        },
        "integration": {
            "guidance_mode": guidance_mode,
            "sync_mode": sync_mode,
            "codeowners_mode": codeowners_mode,
        },
        "commands": [],
        "ownership": [],
        "additional_policies": [],
        "stricter_policies": [],
    }
    project_yaml_content = yaml.safe_dump(project_yaml, sort_keys=False).encode()
    lock = create_lock(
        bundle,
        adapters,
        prepared,
        guidance_mode=guidance_mode,
        sync_mode=sync_mode,
        codeowners_mode=codeowners_mode,
    )
    _verify_isolated(
        prepared,
        lock,
        project_yaml_content,
        {_PROJECT_MD_PATH: project_markdown.encode()},
    )

    atomic_write_protected(repo, _PROJECT_MD_PATH, project_markdown.encode())
    atomic_write_protected(repo, _PROJECT_YAML_PATH, project_yaml_content)
    write_prepared(repo, prepared, previous_lock=None)
    write_lock(repo, lock)
    issues = check_repository(repo)
    if issues:
        raise PolicyError("initialized snapshot failed conformance: " + "; ".join(issues))
    return sorted(
        [
            *prepared,
            str(_PROJECT_MD_PATH),
            str(_PROJECT_YAML_PATH),
        ]
    )


def apply_update(repo: Path, bundle: Bundle, *, explicit_version: bool) -> list[str]:
    repo = require_git_repository(repo)
    require_clean_worktree(repo)
    current_issues = check_repository(repo)
    if current_issues:
        raise PolicyError("current snapshot is not conformant: " + "; ".join(current_issues))
    previous_lock = load_lock(repo)
    current = Version.parse(previous_lock["version"])
    candidate = Version.parse(bundle.version)
    if not explicit_version and candidate.major != current.major:
        raise PolicyError("automatic updates cannot cross the pinned major version")
    if previous_lock["channel"] == "stable" and bundle.channel != "stable":
        raise PolicyError("stable consumers cannot update to a prerelease")
    adapters = normalize_adapters(previous_lock["adapters"])
    prepared = prepare_bundle(bundle, adapters)
    lock = create_lock(
        bundle,
        adapters,
        prepared,
        guidance_mode=previous_lock["guidance_mode"],
        sync_mode=previous_lock["sync_mode"],
        codeowners_mode=previous_lock["codeowners_mode"],
    )
    if not explicit_version:
        lock["constraint"] = previous_lock["constraint"]
    _verify_isolated(
        prepared,
        lock,
        (repo / _PROJECT_YAML_PATH).read_bytes(),
        {_PROJECT_MD_PATH: (repo / _PROJECT_MD_PATH).read_bytes()},
    )
    write_prepared(repo, prepared, previous_lock=previous_lock)
    write_lock(repo, lock)
    issues = check_repository(repo)
    if issues:
        raise PolicyError("candidate failed conformance after rendering: " + "; ".join(issues))
    changed = sorted(
        raw for raw, digest in lock["files"].items() if previous_lock["files"].get(raw) != digest
    )
    if previous_lock["version"] != lock["version"]:
        changed.append(".engineering-policy/policy.lock.json")
    return sorted(set(changed))


def _validate_modes(guidance_mode: str, sync_mode: str, codeowners_mode: str) -> None:
    if guidance_mode not in SUPPORTED_GUIDANCE_MODES:
        raise PolicyError(f"unsupported guidance mode: {guidance_mode}")
    if sync_mode not in SUPPORTED_SYNC_MODES:
        raise PolicyError(f"unsupported sync mode: {sync_mode}")
    if codeowners_mode not in SUPPORTED_CODEOWNERS_MODES:
        raise PolicyError(f"unsupported CODEOWNERS mode: {codeowners_mode}")


def _check_bootstrap_collisions(repo: Path, prepared: dict) -> None:
    collisions: list[str] = []
    for raw in prepared:
        if raw.startswith(".engineering-policy/"):
            continue
        path = repo / raw
        if path.exists() or path.is_symlink():
            collisions.append(raw)
    for raw in (_PROJECT_YAML_PATH, _PROJECT_MD_PATH):
        path = repo.joinpath(*raw.parts)
        if path.exists() or path.is_symlink():
            collisions.append(raw.as_posix())
    for namespace in (
        PurePosixPath(".codex"),
        PurePosixPath(".agents/skills/project-engineering-workflow"),
    ):
        path = repo.joinpath(*namespace.parts)
        if path.exists() or path.is_symlink():
            collisions.append(namespace.as_posix())
    if collisions:
        raise PolicyError(
            "enrollment collision; preserve or relocate existing files before retrying: "
            + ", ".join(sorted(set(collisions)))
        )
    destinations = [
        *(PurePosixPath(raw) for raw in prepared),
        _PROJECT_YAML_PATH,
        _PROJECT_MD_PATH,
    ]
    for destination in destinations:
        current = repo
        for part in destination.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise PolicyError(f"enrollment destination has symlink ancestor: {destination}")
            if current.exists() and not current.is_dir():
                raise PolicyError(
                    f"enrollment destination has non-directory ancestor: {destination}"
                )
        leaf = repo.joinpath(*destination.parts)
        if leaf.is_symlink():
            raise PolicyError(f"enrollment destination is a symlink: {destination}")


def _project_markdown(adapters: tuple[str, ...]) -> str:
    configured = ", ".join(adapters)
    return (
        "# Project Engineering Policy Extensions\n\n"
        "Add repository-owned instructions here. These rules may strengthen but cannot "
        "weaken canonical policy IDs. Repository-owned AGENTS.md remains authoritative "
        "and byte-preserved.\n\n"
        f"Configured adapters: {configured}.\n"
    )


def _verify_isolated(
    prepared: dict,
    lock: dict,
    project_yaml: bytes,
    protected_files: dict[PurePosixPath, bytes],
) -> None:
    with tempfile.TemporaryDirectory(prefix="policyctl-render-") as temporary:
        root = Path(temporary).resolve()
        atomic_write_protected(root, _PROJECT_YAML_PATH, project_yaml)
        for path, content in protected_files.items():
            atomic_write_protected(root, path, content)
        write_prepared(root, prepared, previous_lock=None)
        write_lock(root, lock)
        issues = check_repository(root)
        if issues:
            raise PolicyError("candidate failed isolated conformance: " + "; ".join(issues))
