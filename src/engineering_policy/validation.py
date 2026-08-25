from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import fastjsonschema
import yaml

from engineering_policy.constants import (
    SOURCE_REPOSITORY,
    SUPPORTED_SCHEMA_PROTOCOLS,
    TRUSTED_SCHEMA_SETS_SHA256,
)
from engineering_policy.errors import PolicyError
from engineering_policy.semver import Version

_CODEX_ROUTING_PATH = "spec/codex-model-routing.yaml"
_CODEX_ROUTING_SNAPSHOT_PATH = "codex-model-routing.yaml"
_CODEX_ROUTING_MIN_VERSION = (2, 1)
_CODEX_REVIEWERS = (
    "project_contract_reviewer",
    "project_test_reviewer",
    "project_security_reviewer",
)


def load_yaml_bytes(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise PolicyError(f"invalid YAML in {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must contain a YAML object")
    return value


def load_json_bytes(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"invalid JSON in {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must contain a JSON object")
    return value


def validate_with_schema(instance: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    try:
        fastjsonschema.compile(schema)(instance)
    except fastjsonschema.JsonSchemaException as exc:
        raise PolicyError(f"{label} schema violation: {exc.message}") from exc


def validate_policy(
    files: dict[str, bytes], *, expected_protocol: int | None = None
) -> dict[str, Any]:
    policy_path = "spec/policy.yaml" if "spec/policy.yaml" in files else "policy.yaml"
    required = {policy_path, "schemas/policy.schema.json"}
    missing = required - files.keys()
    if missing:
        raise PolicyError(f"bundle is missing policy inputs: {', '.join(sorted(missing))}")
    policy = load_yaml_bytes(files[policy_path], policy_path)
    protocol_version = policy.get("protocol_version")
    schema_version = policy.get("schema_version")
    _validate_supported_schema_protocol(schema_version, protocol_version, label="policy")
    if expected_protocol is not None and protocol_version != expected_protocol:
        raise PolicyError("policy protocol differs from the bundle manifest")
    validate_trusted_schemas(files, protocol_version=protocol_version)
    schema = load_json_bytes(files["schemas/policy.schema.json"], "policy schema")
    validate_with_schema(policy, schema, "policy")
    if policy["canonical_repository"] != SOURCE_REPOSITORY:
        raise PolicyError("policy repository identity is not canonical")
    version = Version.parse(policy["policy_version"])
    if policy["policy_version"] != str(version):
        raise PolicyError("policy version is not canonical")
    routing_present = _routing_path(files) is not None
    if routing_present:
        validate_model_routing(files)
    elif (version.major, version.minor) >= _CODEX_ROUTING_MIN_VERSION:
        raise PolicyError("bundle is missing the required Codex model-routing spec")
    canonical_ids = [item["id"] for item in policy["canonical_policies"]]
    if len(canonical_ids) != len(set(canonical_ids)):
        raise PolicyError("canonical policy IDs must be unique")
    role_ids = [item["id"] for item in policy["roles"]]
    if len(role_ids) != len(set(role_ids)):
        raise PolicyError("role IDs must be unique")
    for role in policy["roles"]:
        prompt_path = role["prompt"]
        if prompt_path not in files:
            raise PolicyError(f"role prompt is missing: {prompt_path}")
        if role["id"] != "coordinator" and role["permission"] != "read-only":
            raise PolicyError(f"review role must be read-only: {role['id']}")
    return policy


def validate_migrations(files: dict[str, bytes], *, protocol_version: int | None = None) -> None:
    if protocol_version is None:
        policy_path = "spec/policy.yaml" if "spec/policy.yaml" in files else "policy.yaml"
        if policy_path not in files:
            raise PolicyError("bundle is missing policy inputs")
        protocol_version = load_yaml_bytes(files[policy_path], policy_path).get("protocol_version")
    if protocol_version not in TRUSTED_SCHEMA_SETS_SHA256:
        raise PolicyError("migration protocol is incompatible with this updater")
    schema_path = "schemas/migration.schema.json"
    if schema_path not in files:
        raise PolicyError("bundle is missing the migration schema")
    schema = load_json_bytes(files[schema_path], schema_path)
    migrations = sorted(
        name for name in files if name.startswith("migrations/") and name.endswith(".json")
    )
    if not migrations:
        raise PolicyError("bundle must contain a declarative migration chain")
    expected_from: int | None = None
    for name in migrations:
        migration = load_json_bytes(files[name], name)
        validate_with_schema(migration, schema, name)
        if migration["from_protocol"] != expected_from:
            raise PolicyError(f"migration chain is broken at {name}")
        expected_from = migration["to_protocol"]
    if expected_from != protocol_version:
        raise PolicyError("migration chain does not resolve to the supported protocol")


def validate_adapters(files: dict[str, bytes]) -> None:
    codex_skill = "adapters/codex/.agents/skills/project-engineering-workflow/SKILL.md"
    openai_yaml = "adapters/codex/.agents/skills/project-engineering-workflow/agents/openai.yaml"
    for required in (codex_skill, openai_yaml):
        if required not in files:
            raise PolicyError(f"bundle is missing adapter contract: {required}")
    codex_frontmatter = _frontmatter(files[codex_skill], codex_skill)
    if set(codex_frontmatter) != {"name", "description"}:
        raise PolicyError("Codex skill frontmatter may contain only name and description")
    openai = load_yaml_bytes(files[openai_yaml], openai_yaml)
    if openai.get("policy", {}).get("allow_implicit_invocation") is not False:
        raise PolicyError("Codex skill must disable implicit invocation in agents/openai.yaml")
    if set(openai) != {"interface", "policy"} or set(openai["policy"]) != {
        "allow_implicit_invocation"
    }:
        raise PolicyError("Codex skill metadata contains unsupported policy or dependency fields")
    if set(openai["interface"]) != {"display_name", "short_description", "default_prompt"}:
        raise PolicyError("Codex skill interface contains unsupported fields")
    codex_config_path = "adapters/codex/.codex/config.toml"
    try:
        codex_config = tomllib.loads(files[codex_config_path].decode())
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError("Codex project config is missing or invalid") from exc
    if set(codex_config) != {"project_doc_max_bytes", "agents"} or set(codex_config["agents"]) != {
        "enabled",
        "max_concurrent_threads_per_session",
    }:
        raise PolicyError("Codex project config contains unsupported settings")
    claude_skill = "adapters/claude/.claude/skills/project-engineering-workflow/SKILL.md"
    claude_settings_path = "adapters/claude/.claude/settings.json"
    for required in (claude_skill, claude_settings_path):
        if required not in files:
            raise PolicyError(f"bundle is missing adapter contract: {required}")
    claude_frontmatter = _frontmatter(files[claude_skill], claude_skill)
    if set(claude_frontmatter) != {"name", "description", "disable-model-invocation"}:
        raise PolicyError("Claude skill frontmatter contains unsupported fields")
    if claude_frontmatter["name"] != "project-engineering-workflow":
        raise PolicyError("Claude skill has an unexpected name")
    if claude_frontmatter["disable-model-invocation"] is not True:
        raise PolicyError("Claude skill must disable implicit model invocation")
    claude_settings = load_json_bytes(files[claude_settings_path], claude_settings_path)
    if claude_settings != {"$schema": "https://json.schemastore.org/claude-code-settings.json"}:
        raise PolicyError("Claude project settings contain unsupported fields")
    expected_adapter_files = _expected_adapter_files()
    actual_adapter_files = {name for name in files if name.startswith("adapters/")}
    unexpected = sorted(actual_adapter_files - expected_adapter_files)
    missing = sorted(expected_adapter_files - actual_adapter_files)
    if unexpected or missing:
        raise PolicyError(
            f"adapter file contract mismatch; missing={missing}, unexpected={unexpected}"
        )
    if _routing_path(files) is not None:
        routing = validate_model_routing(files)
        expected_reviewers = {
            item["agent"]: (item["model"], item["reasoning_effort"])
            for item in routing["routes"]["reviewers"]
        }
        for agent, (model, effort) in expected_reviewers.items():
            path = f"adapters/codex/.codex/agents/{agent}.toml"
            configured = tomllib.loads(files[path].decode())
            if (configured.get("model"), configured.get("model_reasoning_effort")) != (
                model,
                effort,
            ):
                raise PolicyError(f"Codex reviewer route does not match its agent contract: {path}")
    for name, content in files.items():
        if name.startswith("adapters/codex/.codex/agents/") and name.endswith(".toml"):
            try:
                agent = tomllib.loads(content.decode())
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise PolicyError(f"invalid Codex agent TOML in {name}: {exc}") from exc
            if agent.get("sandbox_mode") != "read-only":
                raise PolicyError(f"Codex reviewer is not read-only: {name}")
            allowed = {
                "name",
                "description",
                "model",
                "model_reasoning_effort",
                "sandbox_mode",
                "developer_instructions",
            }
            if set(agent) != allowed:
                raise PolicyError(f"Codex reviewer contains unsupported settings: {name}")
        if name.startswith("adapters/claude/.claude/agents/") and name.endswith(".md"):
            agent = _frontmatter(content, name)
            if set(agent) != {"name", "description", "tools", "model", "permissionMode"}:
                raise PolicyError(f"Claude reviewer contains unsupported settings: {name}")
            if (
                agent["tools"] != "Read, Grep, Glob"
                or agent["model"] != "inherit"
                or agent["permissionMode"] != "plan"
            ):
                raise PolicyError(f"Claude reviewer is not read-only: {name}")


def validate_model_routing(files: dict[str, bytes]) -> dict[str, Any]:
    """Validate the fail-closed Codex planner, executor, and reviewer graph."""

    path = _routing_path(files)
    if path is None:
        raise PolicyError(f"bundle is missing the Codex model-routing spec: {_CODEX_ROUTING_PATH}")
    routing = load_yaml_bytes(files[path], _CODEX_ROUTING_PATH)
    expected_top_level = {
        "schema_version",
        "routing_version",
        "client",
        "fail_closed",
        "max_review_fix_passes",
        "orchestration",
        "routes",
    }
    if set(routing) != expected_top_level:
        raise PolicyError("Codex model-routing spec contains unknown or missing fields")
    if routing["schema_version"] != 1 or routing["routing_version"] != 1:
        raise PolicyError("Codex model-routing spec version is unsupported")
    if routing["client"] != "codex" or routing["fail_closed"] is not True:
        raise PolicyError("Codex model-routing spec must be Codex-only and fail closed")
    if type(routing["max_review_fix_passes"]) is not int or routing["max_review_fix_passes"] != 2:
        raise PolicyError("Codex model-routing spec must allow exactly two review/fix passes")

    orchestration = routing["orchestration"]
    if not isinstance(orchestration, dict) or set(orchestration) != {
        "parent_writes",
        "planner_complete_before_executor",
        "review_fanout",
        "executor_retained_for_fixes",
        "route_findings_to",
        "rerun_reviewers_after_fix",
    }:
        raise PolicyError("Codex model-routing orchestration contract is invalid")
    if orchestration != {
        "parent_writes": False,
        "planner_complete_before_executor": True,
        "review_fanout": "parallel",
        "executor_retained_for_fixes": True,
        "route_findings_to": "executor",
        "rerun_reviewers_after_fix": True,
    }:
        raise PolicyError("Codex model-routing orchestration contract is not fail closed")

    routes = routing["routes"]
    if not isinstance(routes, dict) or set(routes) != {"planner", "executor", "reviewers"}:
        raise PolicyError("Codex model-routing routes are invalid")
    _validate_route(
        routes["planner"],
        expected={
            "agent": "default",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "permission": "read-only",
            "output": "decision-complete execution packet",
        },
        label="planner",
    )
    _validate_route(
        routes["executor"],
        expected={
            "agent": "worker",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "permission": "writer",
            "output": "implementation diff, tests, evidence, and residual risks",
        },
        label="executor",
    )
    reviewers = routes["reviewers"]
    if not isinstance(reviewers, list) or len(reviewers) != len(_CODEX_REVIEWERS):
        raise PolicyError("Codex model-routing reviewer fan-out must contain three reviewers")
    for reviewer, expected_id in zip(reviewers, _CODEX_REVIEWERS, strict=True):
        _validate_route(
            reviewer,
            expected={
                "id": expected_id,
                "agent": expected_id,
                "model": "gpt-5.6-terra",
                "reasoning_effort": "xhigh",
                "permission": "read-only",
                "parallel": True,
            },
            label=f"reviewer {expected_id}",
        )
    return routing


def _validate_route(value: object, *, expected: dict[str, object], label: str) -> None:
    if not isinstance(value, dict) or set(value) != set(expected) or value != expected:
        raise PolicyError(f"Codex model-routing {label} route is invalid")


def _routing_path(files: dict[str, bytes]) -> str | None:
    if _CODEX_ROUTING_PATH in files:
        return _CODEX_ROUTING_PATH
    if _CODEX_ROUTING_SNAPSHOT_PATH in files:
        return _CODEX_ROUTING_SNAPSHOT_PATH
    return None


def _frontmatter(content: bytes, label: str) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"frontmatter is not UTF-8: {label}") from exc
    match = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, re.DOTALL)
    if match is None:
        raise PolicyError(f"frontmatter is missing or malformed: {label}")
    value = yaml.safe_load(match.group(1))
    if not isinstance(value, dict):
        raise PolicyError(f"frontmatter must be an object: {label}")
    return value


def _expected_adapter_files() -> set[str]:
    codex_agents = {
        "project_scope_explorer.toml",
        "project_contract_reviewer.toml",
        "project_test_reviewer.toml",
        "project_security_reviewer.toml",
    }
    claude_agents = {
        "project-scope-explorer.md",
        "project-contract-reviewer.md",
        "project-test-reviewer.md",
        "project-security-reviewer.md",
    }
    return {
        "adapters/codex/.codex/config.toml",
        "adapters/codex/.agents/skills/project-engineering-workflow/SKILL.md",
        "adapters/codex/.agents/skills/project-engineering-workflow/agents/openai.yaml",
        *{f"adapters/codex/.codex/agents/{name}" for name in codex_agents},
        "adapters/claude/.claude/settings.json",
        "adapters/claude/.claude/skills/project-engineering-workflow/SKILL.md",
        *{f"adapters/claude/.claude/agents/{name}" for name in claude_agents},
    }


def validate_overlay(repo: Path, policy: dict[str, Any]) -> dict[str, Any]:
    overlay_path = repo / "engineering-policy.project.yaml"
    schema_path = repo / ".engineering-policy/spec/schemas/overlay.schema.json"
    if not overlay_path.is_file():
        raise PolicyError("engineering-policy.project.yaml is missing")
    if overlay_path.is_symlink():
        raise PolicyError("engineering-policy.project.yaml must not be a symlink")
    if not schema_path.is_file():
        raise PolicyError("overlay schema is missing from the policy snapshot")
    overlay = load_yaml_bytes(overlay_path.read_bytes(), str(overlay_path))
    schema = load_json_bytes(schema_path.read_bytes(), str(schema_path))
    validate_trusted_schema(
        schema_path.read_bytes(),
        "schemas/overlay.schema.json",
        protocol_version=policy["protocol_version"],
    )
    validate_with_schema(overlay, schema, "project overlay")
    canonical = {item["id"] for item in policy["canonical_policies"]}
    for item in overlay.get("additional_policies", []):
        if item["id"] in canonical or not item["id"].startswith("project."):
            raise PolicyError(f"project policy cannot replace canonical ID: {item['id']}")
    for item in overlay.get("stricter_policies", []):
        if item["canonical_id"] not in canonical:
            raise PolicyError(f"stricter policy references unknown ID: {item['canonical_id']}")
    return overlay


def validate_trusted_schemas(files: dict[str, bytes], *, protocol_version: int) -> None:
    expected = TRUSTED_SCHEMA_SETS_SHA256.get(protocol_version)
    if expected is None:
        raise PolicyError("policy protocol is incompatible with this updater")
    actual = {name for name in files if name.startswith("schemas/")}
    missing = sorted(set(expected) - actual)
    unexpected = sorted(actual - set(expected))
    if missing or unexpected:
        raise PolicyError(
            f"policy snapshot trusted schema set mismatch; missing={missing}, "
            f"unexpected={unexpected}"
        )
    for name in expected:
        validate_trusted_schema(files[name], name, protocol_version=protocol_version)


def validate_trusted_schema(content: bytes, name: str, *, protocol_version: int) -> None:
    schema_set = TRUSTED_SCHEMA_SETS_SHA256.get(protocol_version)
    expected = schema_set.get(name) if schema_set is not None else None
    if expected is None or hashlib.sha256(content).hexdigest() != expected:
        raise PolicyError(f"candidate attempted to replace trusted protocol schema: {name}")


def validate_manifest_shape(manifest: dict[str, Any]) -> None:
    expected = {
        "schema_version",
        "protocol_version",
        "repository",
        "version",
        "channel",
        "files",
    }
    if set(manifest) != expected:
        raise PolicyError("bundle manifest has unknown or missing fields")
    _validate_supported_schema_protocol(
        manifest["schema_version"], manifest["protocol_version"], label="bundle"
    )
    if manifest["repository"] != SOURCE_REPOSITORY:
        raise PolicyError("bundle repository identity is not canonical")
    version = Version.parse(manifest["version"])
    if manifest["version"] != str(version):
        raise PolicyError("bundle version is not canonical")
    if version.major != manifest["protocol_version"]:
        raise PolicyError("bundle version major does not match its protocol")
    channel = manifest["channel"]
    if not isinstance(channel, str) or channel not in {"stable", "prerelease"}:
        raise PolicyError("bundle channel is invalid")
    if (channel == "stable") != version.stable:
        raise PolicyError("bundle channel does not match its version")
    if not isinstance(manifest["files"], list) or not manifest["files"]:
        raise PolicyError("bundle manifest files must be a non-empty list")


def _validate_supported_schema_protocol(
    schema_version: object, protocol_version: object, *, label: str
) -> None:
    if not isinstance(schema_version, int) or schema_version not in SUPPORTED_SCHEMA_PROTOCOLS:
        raise PolicyError(f"{label} schema is unsupported")
    if (
        not isinstance(protocol_version, int)
        or protocol_version not in TRUSTED_SCHEMA_SETS_SHA256
        or SUPPORTED_SCHEMA_PROTOCOLS[schema_version] != protocol_version
    ):
        if label == "bundle":
            raise PolicyError(
                "bundle protocol is incompatible; manual re-bootstrap or an explicit major "
                "update is required"
            )
        raise PolicyError(f"{label} protocol is incompatible with this updater")
