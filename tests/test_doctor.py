from __future__ import annotations

import json
from pathlib import Path

from engineering_policy.bundle import Bundle
from engineering_policy.doctor import (
    _client_diagnostic,
    _codex_models,
    _personal_skill_conflicts,
    run_doctor,
)
from engineering_policy.operations import initialize
from engineering_policy.semver import Version


def test_doctor_detects_personal_skill_conflicts(monkeypatch, tmp_path: Path) -> None:
    skill = tmp_path / ".agents/skills/project-engineering-workflow/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: project-engineering-workflow\n---\n", encoding="utf-8")
    monkeypatch.setattr("engineering_policy.doctor.Path.home", lambda: tmp_path)
    diagnostics = _personal_skill_conflicts({"codex", "claude"})
    assert [item.code for item in diagnostics] == ["personal-skill-conflict"]


def test_doctor_detects_personal_claude_skill_conflict(monkeypatch, tmp_path: Path) -> None:
    skill = tmp_path / ".claude/skills/project-engineering-workflow/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: project-engineering-workflow\n---\n", encoding="utf-8")
    monkeypatch.setattr("engineering_policy.doctor.Path.home", lambda: tmp_path)
    diagnostics = _personal_skill_conflicts({"claude"})
    assert [(item.code, "claude" in item.message) for item in diagnostics] == [
        ("personal-skill-conflict", True)
    ]


def test_doctor_detects_unavailable_configured_models(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    agents = repo / ".codex/agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.toml").write_text(
        'name = "reviewer"\n'
        'description = "review"\n'
        'developer_instructions = "review"\n'
        'model = "unavailable-model"\n',
        encoding="utf-8",
    )
    cache = tmp_path / ".codex/models_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"models": [{"slug": "available-model"}]}), encoding="utf-8")
    monkeypatch.setattr("engineering_policy.doctor.Path.home", lambda: tmp_path)
    diagnostics = _codex_models(repo)
    assert diagnostics[0].code == "codex-models"
    assert "unavailable-model" in diagnostics[0].message


def test_doctor_reports_ready_for_a_conformant_supported_install(
    monkeypatch, git_repo: Path, valid_bundle: Bundle
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    monkeypatch.setattr("engineering_policy.doctor._personal_skill_conflicts", lambda _a: [])
    monkeypatch.setattr("engineering_policy.doctor._client_diagnostic", lambda _c, _m: [])
    monkeypatch.setattr("engineering_policy.doctor._codex_trust", lambda _r: [])
    monkeypatch.setattr("engineering_policy.doctor._codex_models", lambda _r: [])
    diagnostics = run_doctor(git_repo)
    assert [(item.severity, item.code) for item in diagnostics] == [("ok", "ready")]


def test_doctor_requires_an_explicitly_selected_pending_claude_client(
    monkeypatch, git_repo: Path, valid_bundle: Bundle
) -> None:
    initialize(git_repo, valid_bundle, ("claude",))
    monkeypatch.setattr("engineering_policy.doctor._personal_skill_conflicts", lambda _a: [])
    diagnostics = run_doctor(git_repo)
    assert [(item.severity, item.code) for item in diagnostics] == [("error", "client-missing")]


def test_doctor_reports_detected_claude_version_while_validation_is_pending(
    monkeypatch, git_repo: Path, valid_bundle: Bundle
) -> None:
    initialize(git_repo, valid_bundle, ("claude",))
    monkeypatch.setattr("engineering_policy.doctor._personal_skill_conflicts", lambda _a: [])
    monkeypatch.setattr(
        "engineering_policy.doctor._client_version", lambda _client: Version.parse("1.2.3")
    )
    diagnostics = run_doctor(git_repo, client="claude")
    assert [(item.severity, item.code) for item in diagnostics] == [
        ("warning", "claude-validation-pending")
    ]
    assert "1.2.3" in diagnostics[0].message


def test_doctor_rejects_a_client_that_is_not_enrolled(git_repo: Path, valid_bundle: Bundle) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    diagnostics = run_doctor(git_repo, client="claude")
    assert [(item.severity, item.code) for item in diagnostics] == [
        ("error", "client-not-enrolled")
    ]


def test_validated_claude_accepts_its_exact_minimum(monkeypatch) -> None:
    monkeypatch.setattr(
        "engineering_policy.doctor._client_version", lambda _client: Version.parse("1.2.3")
    )
    assert _client_diagnostic("claude", "1.2.3") == []


def test_validated_claude_rejects_an_older_version(monkeypatch) -> None:
    monkeypatch.setattr(
        "engineering_policy.doctor._client_version", lambda _client: Version.parse("1.2.2")
    )
    diagnostics = _client_diagnostic("claude", "1.2.3")
    assert [(item.severity, item.code) for item in diagnostics] == [("error", "client-version")]
