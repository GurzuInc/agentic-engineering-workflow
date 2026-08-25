from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from engineering_policy.bundle import Bundle
from engineering_policy.errors import PolicyError
from engineering_policy.validation import validate_model_routing


def test_model_routing_contract_is_exact(release_bundle: Path) -> None:
    bundle = Bundle.load(release_bundle)
    routing = validate_model_routing(bundle.files)

    assert routing["fail_closed"] is True
    assert routing["max_review_fix_passes"] == 2
    assert routing["routes"]["planner"]["model"] == "gpt-5.6-sol"
    assert routing["routes"]["executor"]["model"] == "gpt-5.6-luna"
    assert [item["model"] for item in routing["routes"]["reviewers"]] == [
        "gpt-5.6-terra",
        "gpt-5.6-terra",
        "gpt-5.6-terra",
    ]
    assert all(item["reasoning_effort"] == "xhigh" for item in routing["routes"]["reviewers"])


def test_model_routing_sentinel_is_copied_into_the_codex_adapter(release_bundle: Path) -> None:
    bundle = Bundle.load(release_bundle)
    assert (
        bundle.files["spec/codex-model-routing.yaml"]
        == bundle.files["adapters/codex/.codex/model-routing.yaml"]
    )


def test_adapter_contract_rejects_skill_route_divergence(
    release_bundle: Path, mutate_bundle
) -> None:
    skill = "adapters/codex/.agents/skills/project-engineering-workflow/SKILL.md"
    source = Bundle.load(release_bundle).files[skill].decode()
    mutated = source.replace("gpt-5.6-luna", "gpt-5.6-sol", 1).encode()
    candidate = mutate_bundle(release_bundle, replacements={skill: mutated})
    with pytest.raises(PolicyError, match="workflow skill does not implement"):
        Bundle.load(candidate)


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ("hard failure and never fall back to another model", "best effort"),
        ("in read-only mode", "in writer mode"),
        ("run in parallel", "run sequentially"),
        ("read-only permissions", "writer permissions"),
    ],
)
def test_adapter_contract_rejects_weakened_skill_controls(
    release_bundle: Path, mutate_bundle, needle: str, replacement: str
) -> None:
    skill = "adapters/codex/.agents/skills/project-engineering-workflow/SKILL.md"
    source = Bundle.load(release_bundle).files[skill].decode()
    assert needle in source
    mutated = source.replace(needle, replacement, 1).encode()
    candidate = mutate_bundle(release_bundle, replacements={skill: mutated})
    with pytest.raises(PolicyError, match="workflow skill does not implement"):
        Bundle.load(candidate)


def test_adapter_contract_rejects_a_missing_routing_sentinel(
    release_bundle: Path, mutate_bundle
) -> None:
    candidate = mutate_bundle(
        release_bundle,
        removals={"adapters/codex/.codex/model-routing.yaml"},
    )
    with pytest.raises(PolicyError, match="missing its model-routing sentinel"):
        Bundle.load(candidate)


def test_adapter_contract_rejects_an_altered_routing_sentinel(
    release_bundle: Path, mutate_bundle
) -> None:
    candidate = mutate_bundle(
        release_bundle,
        replacements={"adapters/codex/.codex/model-routing.yaml": b"tampered\n"},
    )
    with pytest.raises(PolicyError, match="sentinel differs"):
        Bundle.load(candidate)


def test_adapter_contract_rejects_a_three_thread_configuration(
    release_bundle: Path, mutate_bundle
) -> None:
    config = b"""project_doc_max_bytes = 32768

[agents]
enabled = true
max_concurrent_threads_per_session = 3
"""
    candidate = mutate_bundle(
        release_bundle,
        replacements={"adapters/codex/.codex/config.toml": config},
    )
    with pytest.raises(PolicyError, match="four concurrent review threads"):
        Bundle.load(candidate)


def test_model_routing_rejects_a_model_or_effort_fallback(release_bundle: Path) -> None:
    bundle = Bundle.load(release_bundle)
    content = bundle.files["spec/codex-model-routing.yaml"].decode()
    mutated = content.replace("model: gpt-5.6-luna", "model: gpt-5.6-sol", 1)
    with pytest.raises(PolicyError, match="executor route is invalid"):
        validate_model_routing({"spec/codex-model-routing.yaml": mutated.encode("utf-8")})


def test_model_routing_requires_the_route_in_v21_policy(
    release_bundle: Path, mutate_bundle
) -> None:
    missing = mutate_bundle(
        release_bundle,
        version="2.1.0-rc.1",
        removals={"spec/codex-model-routing.yaml"},
    )
    with pytest.raises(PolicyError, match="required Codex model-routing spec"):
        Bundle.load(missing)


def test_real_v20_bundle_keeps_the_legacy_adapter_contract(legacy_v2_bundle: Bundle) -> None:
    assert legacy_v2_bundle.version == "2.0.0"
    assert "spec/codex-model-routing.yaml" not in legacy_v2_bundle.files
    assert "adapters/codex/.codex/model-routing.yaml" not in legacy_v2_bundle.files


def test_v20_validator_rejects_the_v21_adapter_sentinel(
    legacy_v2_bundle: Bundle, release_bundle: Path
) -> None:
    legacy_source = legacy_v2_bundle.path.parent.parent / "source"
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from engineering_policy.bundle import Bundle\n"
        "try:\n"
        "    Bundle.load(Path(sys.argv[1]))\n"
        "except Exception as exc:\n"
        "    print(exc)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(release_bundle)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(legacy_source / "src")},
    )
    assert result.returncode == 0
    assert "adapter file contract mismatch" in result.stdout
