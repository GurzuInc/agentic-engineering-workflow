from __future__ import annotations

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
