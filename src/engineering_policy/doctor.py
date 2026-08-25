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
from engineering_policy.validation import load_yaml_bytes, validate_model_routing

_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str


def run_doctor(repo: Path, *, client: str = "all") -> list[Diagnostic]:
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
    selected = adapters if client == "all" else {client}
    unavailable = selected - adapters
    if unavailable:
        return [
            Diagnostic(
                "error",
                "client-not-enrolled",
                f"selected client is not enrolled: {', '.join(sorted(unavailable))}",
            )
        ]
    diagnostics.extend(_personal_skill_conflicts(selected))
    if "codex" in selected:
        diagnostics.extend(_client_diagnostic("codex", policy["client_minimums"]["codex"]))
        diagnostics.extend(_codex_trust(repo))
        diagnostics.extend(_codex_models(repo))
    if "claude" in selected:
        validation = policy["adapter_validation"]["claude"]
        minimum = policy["client_minimums"].get("claude")
        if validation == "validated":
            diagnostics.extend(_client_diagnostic("claude", minimum))
        else:
            version = _client_version("claude")
            if isinstance(version, Diagnostic):
                diagnostics.append(version)
            else:
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        "claude-validation-pending",
                        f"Claude {version} is installed; live client validation is pending",
                    )
                )
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
    if "claude" in adapters:
        candidates.append(("claude", home / ".claude/skills/project-engineering-workflow/SKILL.md"))
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
    version = _client_version(command)
    if isinstance(version, Diagnostic):
        return [version]
    required = Version.parse(minimum)
    if version < required:
        return [
            Diagnostic(
                "error",
                "client-version",
                f"{command} {version} is older than the supported minimum {required}",
            )
        ]
    return []


def _client_version(command: str) -> Version | Diagnostic:
    executable = shutil.which(command)
    if executable is None:
        return Diagnostic(
            "error", "client-missing", f"configured client is not installed: {command}"
        )
    result = subprocess.run(  # noqa: S603 - executable is resolved from a fixed client name
        [executable, "--version"], check=False, capture_output=True, text=True, timeout=15
    )
    match = _VERSION_PATTERN.search(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or match is None:
        return Diagnostic("error", "client-version", f"cannot determine {command} version")
    return Version.parse(match.group(1))


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
    routed: list[tuple[str, str]] = []
    for path in (repo / ".codex/agents").glob("*.toml"):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return [Diagnostic("error", "codex-agent", f"invalid Codex agent {path.name}: {exc}")]
        model = data.get("model")
        if isinstance(model, str):
            configured.add(model)
    routing_path = repo / ".engineering-policy/spec/codex-model-routing.yaml"
    if routing_path.exists():
        try:
            routing = validate_model_routing(
                {"spec/codex-model-routing.yaml": routing_path.read_bytes()}
            )
        except (OSError, PolicyError) as exc:
            return [Diagnostic("error", "codex-model-routing", str(exc))]
        planner = routing["routes"]["planner"]
        executor = routing["routes"]["executor"]
        configured.update((planner["model"], executor["model"]))
        routed.extend(
            (route["model"], route["reasoning_effort"])
            for route in (planner, executor, *routing["routes"]["reviewers"])
        )
    cache = Path.home() / ".codex/models_cache.json"
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [
            Diagnostic(
                "error",
                "codex-models",
                "Codex model catalog is unavailable; configured reviewer models cannot be verified",
            )
        ]
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return [Diagnostic("error", "codex-models", "Codex model catalog has an invalid shape")]
    available: set[str] = set()
    supported_efforts: dict[str, set[str]] = {}
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            return [Diagnostic("error", "codex-models", "Codex model catalog has an invalid model")]
        slug = item["slug"]
        available.add(slug)
        levels = item.get("supported_reasoning_levels", [])
        if not isinstance(levels, list):
            return [
                Diagnostic(
                    "error",
                    "codex-models",
                    f"Codex model catalog has invalid reasoning levels for {slug}",
                )
            ]
        supported_efforts[slug] = {
            level["effort"]
            for level in levels
            if isinstance(level, dict) and isinstance(level.get("effort"), str)
        }
    unavailable = sorted(configured - available)
    if unavailable:
        return [
            Diagnostic(
                "error",
                "codex-models",
                f"configured Codex models are unavailable: {', '.join(unavailable)}",
            )
        ]
    unsupported_efforts = sorted(
        f"{model}:{effort}"
        for model, effort in routed
        if effort not in supported_efforts.get(model, set())
    )
    if unsupported_efforts:
        return [
            Diagnostic(
                "error",
                "codex-models",
                "configured Codex reasoning efforts are unavailable: "
                + ", ".join(unsupported_efforts),
            )
        ]
    return []
