from __future__ import annotations

from pathlib import Path

import pytest
from conftest import commit_all

from engineering_policy.bundle import Bundle
from engineering_policy.errors import PolicyError
from engineering_policy.operations import apply_update, initialize
from engineering_policy.rendering import load_lock
from engineering_policy.semver import Version, choose_latest


def test_stable_channel_excludes_prereleases_and_skips_to_latest() -> None:
    selected = choose_latest(
        ["v1.0.1", "v1.2.0-rc.1", "v1.1.5", "v2.0.0"],
        major=1,
        channel="stable",
        current=Version.parse("1.0.0"),
    )
    assert str(selected) == "1.1.5"


def test_prerelease_channel_selects_latest_within_major() -> None:
    selected = choose_latest(
        ["v1.0.0-rc.2", "v1.0.0-rc.2", "v1.0.0", "v2.0.0-rc.1"],
        major=1,
        channel="prerelease",
        current=Version.parse("1.0.0-rc.2"),
    )
    assert str(selected) == "1.0.0"


def test_automatic_same_major_upgrade_is_allowed(
    git_repo: Path, valid_bundle: Bundle, v2_bundle: Path
) -> None:
    initialize(git_repo, valid_bundle, ("codex",))
    commit_all(git_repo)
    candidate = Bundle.load(v2_bundle)
    apply_update(git_repo, candidate, explicit_version=False)
    assert load_lock(git_repo)["constraint"] == "2.x"


def test_explicit_rollback_is_supported(
    git_repo: Path, release_bundle: Path, mutate_bundle
) -> None:
    newer = Bundle.load(
        mutate_bundle(
            release_bundle,
            version="2.0.0-rc.2",
            channel="prerelease",
        )
    )
    initialize(git_repo, newer, ("codex",))
    commit_all(git_repo)
    older = Bundle.load(release_bundle)
    apply_update(git_repo, older, explicit_version=True)
    assert load_lock(git_repo)["version"] == "2.0.0-rc.1"


def test_stable_consumer_rejects_prerelease_update(
    git_repo: Path, release_bundle: Path, mutate_bundle
) -> None:
    stable = Bundle.load(mutate_bundle(release_bundle, version="2.0.0", channel="stable"))
    initialize(git_repo, stable, ("codex",))
    commit_all(git_repo)
    prerelease = Bundle.load(release_bundle)
    with pytest.raises(PolicyError, match="cannot update to a prerelease"):
        apply_update(git_repo, prerelease, explicit_version=True)


@pytest.mark.parametrize(
    "raw",
    [
        "01.0.0",
        "1.01.0",
        "1.0.01",
        "1.0.0-01",
        "1.0.0-alpha.01",
        "1.0.0-00.beta",
        "not-semver",
    ],
)
def test_invalid_semantic_versions_are_rejected(raw: str) -> None:
    with pytest.raises(PolicyError, match="invalid semantic version"):
        Version.parse(raw)


def test_numeric_prerelease_ordering_is_total_and_deterministic() -> None:
    first = Version.parse("1.0.0-1")
    second = Version.parse("1.0.0-2")
    assert first < second
    assert not second < first
    assert first != second
