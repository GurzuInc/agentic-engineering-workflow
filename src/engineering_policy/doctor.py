from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from engineering_policy.errors import PolicyError
from engineering_policy.rendering import check_repository, load_lock
from engineering_policy.semver import Version
from engineering_policy.validation import load_yaml_bytes

_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str


def run_doctor(repo: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    try:
        lock = load_lock(repo)
    except PolicyError as exc:
        return [Diagnostic("error", "lock", str(exc))]
    for issue in check_repository(repo):
        diagnostics.append(Diagnostic("error", "drift", issue))
    policy_path = repo / ".engineering-policy/spec/policy.yaml"
    policy = load_yaml_bytes(policy_path.read_bytes(), str(policy_path))
    adapters = set(lock["adapters"])
    diagnostics.extend(_personal_skill_conflicts(adapters))
    if "codex" in adapters:
        diagnostics.extend(_client_diagnostic("codex", policy["client_minimums"]["codex"]))
        diagnostics.extend(_codex_trust(repo))
        diagnostics.extend(_codex_models(repo))
    if not diagnostics:
        diagnostics.append(
            Diagnostic("ok", "ready", "policy snapshot and configured clients are ready")
        )
    return diagnostics


def _personal_skill_conflicts(adapters: set[str]) -> list[Diagnostic]:
    home = Path.home()
    candidates: list[tuple[str, Path]] = []
    if "codex" in adapters:
        candidates.extend(
            [
                ("codex", home / ".agents/skills/project-engineering-workflow/SKILL.md"),
                ("codex", home / ".codex/skills/project-engineering-workflow/SKILL.md"),
            ]
        )
    return [
        Diagnostic(
            "error",
            "personal-skill-conflict",
            f"{client} personal skill shadows or duplicates the repository skill: {path}",
        )
        for client, path in candidates
        if path.exists()
    ]


def _client_diagnostic(command: str, minimum: str) -> list[Diagnostic]:
    executable = shutil.which(command)
    if executable is None:
        return [
            Diagnostic("error", "client-missing", f"configured client is not installed: {command}")
        ]
    result = subprocess.run(  # noqa: S603 - executable is resolved from a fixed client name
        [executable, "--version"], check=False, capture_output=True, text=True, timeout=15
    )
    match = _VERSION_PATTERN.search(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or match is None:
        return [Diagnostic("error", "client-version", f"cannot determine {command} version")]
    installed = Version.parse(match.group(1))
    required = Version.parse(minimum)
    if installed < required:
        return [
            Diagnostic(
                "error",
                "client-version",
                f"{command} {installed} is older than the supported minimum {required}",
            )
        ]
    return []


def _codex_trust(repo: Path) -> list[Diagnostic]:
    config = Path.home() / ".codex/config.toml"
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return [Diagnostic("error", "codex-trust", "Codex user config is missing or invalid")]
    project = data.get("projects", {}).get(str(repo.resolve()), {})
    if project.get("trust_level") != "trusted":
        return [
            Diagnostic(
                "error",
                "codex-trust",
                "repository is not explicitly trusted in the Codex user configuration",
            )
        ]
    return []


def _codex_models(repo: Path) -> list[Diagnostic]:
    configured: set[str] = set()
    for path in (repo / ".codex/agents").glob("*.toml"):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return [Diagnostic("error", "codex-agent", f"invalid Codex agent {path.name}: {exc}")]
        model = data.get("model")
        if isinstance(model, str):
            configured.add(model)
    cache = Path.home() / ".codex/models_cache.json"
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        available = {
            item["slug"]
            for item in payload.get("models", [])
            if isinstance(item, dict) and isinstance(item.get("slug"), str)
        }
    except (OSError, json.JSONDecodeError):
        return [
            Diagnostic(
                "error",
                "codex-models",
                "Codex model catalog is unavailable; configured reviewer models cannot be verified",
            )
        ]
    unavailable = sorted(configured - available)
    if unavailable:
        return [
            Diagnostic(
                "error",
                "codex-models",
                f"configured Codex models are unavailable: {', '.join(unavailable)}",
            )
        ]
    return []
