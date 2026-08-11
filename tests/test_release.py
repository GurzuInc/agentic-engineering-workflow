from __future__ import annotations

import hashlib
import io
import json
import shutil
import urllib.request
from pathlib import Path

import pytest

from engineering_policy.errors import PolicyError
from engineering_policy.release import (
    ReleaseClient,
    _CanonicalApiRedirectHandler,
    _download_public_asset,
    _request_json,
    _required_assets,
    _resolve_annotated_tag_commit,
    _validate_release_artifacts,
    _validate_release_summary,
)
from engineering_policy.semver import Version


class _Response(io.BytesIO):
    def __init__(self, content: bytes, url: str = "https://api.github.com/result") -> None:
        super().__init__(content)
        self._url = url

    def geturl(self) -> str:
        return self._url


class _Opener:
    def __init__(self, respond) -> None:
        self._respond = respond

    def open(self, request, timeout):
        return self._respond(request, timeout)


def test_github_metadata_uses_only_the_explicit_read_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def respond(request, timeout):
        requests.append((request, timeout))
        return _Response(b"{}")

    monkeypatch.setattr(
        "engineering_policy.release.urllib.request.build_opener",
        lambda *_handlers: _Opener(respond),
    )
    monkeypatch.setenv("GH_TOKEN", "ambient-write-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-actions-token")
    monkeypatch.setenv("POLICYCTL_GITHUB_READ_TOKEN", "explicit-read-token")
    assert _request_json("https://api.github.com/repos/example/releases") == {}
    headers = dict(requests[0][0].header_items())
    assert headers["Authorization"] == "Bearer explicit-read-token"
    assert "ambient-write-token" not in headers.values()
    assert "ambient-actions-token" not in headers.values()
    assert requests[0][1] == 5


def test_public_asset_download_is_tokenless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests = []

    def respond(request, timeout):
        requests.append((request, timeout))
        return _Response(b"asset", "https://objects.githubusercontent.com/release-asset")

    monkeypatch.setattr("engineering_policy.release.urllib.request.urlopen", respond)
    monkeypatch.setenv("GH_TOKEN", "ambient-write-token")
    monkeypatch.setenv("POLICYCTL_GITHUB_READ_TOKEN", "explicit-read-token")
    destination = tmp_path / "asset"
    _download_public_asset(
        "https://github.com/GurzuInc/agentic-engineering-workflow/releases/download/"
        "v1.0.0-rc.3/policyctl.pyz",
        destination,
    )
    assert destination.read_bytes() == b"asset"
    assert "Authorization" not in dict(requests[0][0].header_items())
    assert requests[0][1] == 5


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {},
        {"tag_name": 1, "draft": False, "prerelease": True},
        {"tag_name": "v1.0.0-rc.3", "draft": "false", "prerelease": True},
        {"tag_name": "v1.0.0-rc.3", "draft": False, "prerelease": 1},
    ],
)
def test_release_summary_rejects_malformed_metadata(metadata: object) -> None:
    with pytest.raises(PolicyError, match="release metadata"):
        _validate_release_summary(metadata)


@pytest.mark.parametrize(
    "assets",
    [
        None,
        [None],
        [{"name": "policyctl.pyz"}],
        [
            {"name": "policyctl.pyz", "browser_download_url": "https://example.invalid/1"},
            {"name": "policyctl.pyz", "browser_download_url": "https://example.invalid/2"},
        ],
    ],
)
def test_required_release_assets_are_well_formed_and_unique(assets: object) -> None:
    with pytest.raises(PolicyError, match="asset|unique"):
        _required_assets(
            {"assets": assets},
            "v1.0.0-rc.3",
            {
                "policyctl.pyz",
                "engineering-policy-1.0.0-rc.3.zip",
                "release-manifest.json",
                "SHA256SUMS",
            },
        )


def test_required_release_assets_reject_an_unexpected_fifth_asset() -> None:
    required = {
        "policyctl.pyz",
        "engineering-policy-1.0.0-rc.3.zip",
        "release-manifest.json",
        "SHA256SUMS",
    }
    names = required | {"unexpected.txt"}
    assets = [
        {
            "name": name,
            "browser_download_url": (
                "https://github.com/GurzuInc/agentic-engineering-workflow/releases/download/"
                f"v1.0.0-rc.3/{name}"
            ),
        }
        for name in names
    ]
    with pytest.raises(PolicyError, match="exactly match"):
        _required_assets({"assets": assets}, "v1.0.0-rc.3", required)


def test_resolve_latest_rejects_malformed_release_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("engineering_policy.release._request_json", lambda _url: [None])
    with pytest.raises(PolicyError, match="release metadata"):
        ReleaseClient().resolve_latest(
            major=1,
            channel="prerelease",
            current=Version.parse("1.0.0-rc.3"),
        )


def test_network_timeout_becomes_policy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def time_out(_request, timeout):
        raise TimeoutError(f"timed out after {timeout}")

    monkeypatch.setattr(
        "engineering_policy.release.urllib.request.build_opener",
        lambda *_handlers: _Opener(time_out),
    )
    with pytest.raises(PolicyError, match="failed to read public GitHub release metadata"):
        _request_json("https://api.github.com/repos/example/releases")


def test_asset_timeout_preserves_existing_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class PartialResponse:
        reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://objects.githubusercontent.com/release-asset"

        def read1(self, _size):
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise TimeoutError("download timed out")

    monkeypatch.setattr(
        "engineering_policy.release.urllib.request.urlopen",
        lambda _request, timeout: PartialResponse(),
    )
    destination = tmp_path / "asset"
    destination.write_bytes(b"original")
    with pytest.raises(PolicyError, match="failed to download public release asset"):
        _download_public_asset(
            "https://github.com/GurzuInc/agentic-engineering-workflow/releases/download/"
            "v1.0.0-rc.3/policyctl.pyz",
            destination,
        )
    assert destination.read_bytes() == b"original"


def test_metadata_total_deadline_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "engineering_policy.release.urllib.request.build_opener",
        lambda *_handlers: _Opener(lambda _request, _timeout: _Response(b"{}")),
    )
    moments = iter((0.0, 31.0))
    monkeypatch.setattr("engineering_policy.release.time.monotonic", moments.__next__)

    with pytest.raises(PolicyError, match="exceeded the total download deadline"):
        _request_json("https://api.github.com/repos/example/releases")


def test_asset_total_deadline_preserves_existing_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "engineering_policy.release.urllib.request.urlopen",
        lambda _request, timeout: _Response(
            b"replacement", "https://objects.githubusercontent.com/release-asset"
        ),
    )
    moments = iter((0.0, 61.0))
    monkeypatch.setattr("engineering_policy.release.time.monotonic", moments.__next__)
    destination = tmp_path / "asset"
    destination.write_bytes(b"original")

    with pytest.raises(PolicyError, match="exceeded the total download deadline"):
        _download_public_asset(
            "https://github.com/GurzuInc/agentic-engineering-workflow/releases/download/"
            "v1.0.0-rc.3/policyctl.pyz",
            destination,
        )

    assert destination.read_bytes() == b"original"


def test_metadata_redirect_cannot_leave_canonical_api() -> None:
    handler = _CanonicalApiRedirectHandler()
    request = urllib.request.Request(
        "https://api.github.com/repos/example/releases",
        headers={"Authorization": "Bearer read-token"},
    )
    with pytest.raises(PolicyError, match="redirect left the canonical API"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.invalid/capture",
        )


def test_metadata_response_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    def response(_request, _timeout):
        return _Response(b"x" * (2 * 1024 * 1024 + 1))

    monkeypatch.setattr(
        "engineering_policy.release.urllib.request.build_opener",
        lambda *_handlers: _Opener(response),
    )
    with pytest.raises(PolicyError, match="allowed download size"):
        _request_json("https://api.github.com/repos/example/releases")


def _release_artifacts(tmp_path: Path) -> tuple[Version, str, Path, Path, Path, Path, str]:
    version = Version.parse("1.0.0-rc.3")
    tag = "v1.0.0-rc.3"
    commit = "0123456789abcdef0123456789abcdef01234567"
    bundle = tmp_path / "engineering-policy-1.0.0-rc.3.zip"
    pyz = tmp_path / "policyctl.pyz"
    release_manifest = tmp_path / "release-manifest.json"
    checksums = tmp_path / "SHA256SUMS"
    bundle.write_bytes(b"bundle")
    pyz.write_bytes(b"updater")
    digests = {
        bundle.name: hashlib.sha256(bundle.read_bytes()).hexdigest(),
        pyz.name: hashlib.sha256(pyz.read_bytes()).hexdigest(),
    }
    manifest = {
        "schema_version": 1,
        "protocol_version": 1,
        "repository": "GurzuInc/agentic-engineering-workflow",
        "version": str(version),
        "tag": tag,
        "channel": "prerelease",
        "source_commit": commit,
        "asset_digests": digests,
        "supported_adapters": ["codex", "claude"],
        "adapter_validation": {"codex": "validated", "claude": "pending"},
        "supported_python": ["3.11", "3.12", "3.13", "3.14"],
        "recovery": {
            "original_source_commit": "eb79ceb19b1e82ac5f504170ab56abc20e4f484a",
            "original_source_is_ancestor": False,
            "unpublished_rc1_bundle_sha256": (
                "181bc71c4087640685d7bfd8cc575e90afa68a876dc6e50c8a66a0d4f3496ca2"
            ),
            "unpublished_rc1_updater_sha256": (
                "7c2e39091d855a66e8f30fde232a447e916b1b04a8d5ad826fd6a7dd052f3376"
            ),
            "unpublished_rc1_was_tagged": False,
            "failed_unpublished_candidates": ["v1.0.0-rc.2"],
        },
    }
    release_manifest.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    checksum_targets = (bundle, pyz, release_manifest)
    checksums.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(checksum_targets, key=lambda item: item.name)
        ),
        encoding="utf-8",
    )
    return version, tag, bundle, pyz, release_manifest, checksums, commit


def _refresh_release_checksums(bundle: Path, pyz: Path, manifest: Path, checksums: Path) -> None:
    checksums.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted((bundle, pyz, manifest), key=lambda item: item.name)
        ),
        encoding="utf-8",
    )


def test_release_artifact_contract_binds_checksums_manifest_and_annotated_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = _release_artifacts(tmp_path)
    monkeypatch.setattr(
        "engineering_policy.release._resolve_annotated_tag_commit", lambda _tag: artifacts[-1]
    )
    _validate_release_artifacts(*artifacts[:-1])


@pytest.mark.parametrize(
    "mutation",
    [
        "remove-supported-adapters",
        "change-supported-adapters",
        "remove-adapter-validation",
        "change-adapter-validation",
        "change-failed-candidate-history",
    ],
)
def test_release_artifact_contract_rejects_adapter_and_candidate_history_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    artifacts = _release_artifacts(tmp_path)
    _version, _tag, bundle, pyz, manifest_path, checksums, commit = artifacts
    manifest = json.loads(manifest_path.read_text())
    if mutation == "remove-supported-adapters":
        del manifest["supported_adapters"]
    elif mutation == "change-supported-adapters":
        manifest["supported_adapters"] = ["codex"]
    elif mutation == "remove-adapter-validation":
        del manifest["adapter_validation"]
    elif mutation == "change-adapter-validation":
        manifest["adapter_validation"]["claude"] = "validated"
    else:
        manifest["recovery"]["failed_unpublished_candidates"] = []
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    _refresh_release_checksums(bundle, pyz, manifest_path, checksums)
    monkeypatch.setattr(
        "engineering_policy.release._resolve_annotated_tag_commit", lambda _tag: commit
    )
    with pytest.raises(PolicyError, match="unknown or missing fields|contract does not match"):
        _validate_release_artifacts(*artifacts[:-1])


def test_release_artifact_contract_rejects_duplicate_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = _release_artifacts(tmp_path)
    checksums = artifacts[-2]
    checksums.write_text(checksums.read_text() + checksums.read_text().splitlines()[0] + "\n")
    monkeypatch.setattr(
        "engineering_policy.release._resolve_annotated_tag_commit", lambda _tag: artifacts[-1]
    )
    with pytest.raises(PolicyError, match="duplicate asset"):
        _validate_release_artifacts(*artifacts[:-1])


def test_release_artifact_contract_rejects_source_commit_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = _release_artifacts(tmp_path)
    monkeypatch.setattr(
        "engineering_policy.release._resolve_annotated_tag_commit", lambda _tag: "f" * 40
    )
    with pytest.raises(PolicyError, match="source commit"):
        _validate_release_artifacts(*artifacts[:-1])


def test_annotated_tag_resolution_requires_tag_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "engineering_policy.release._request_json",
        lambda _url: {"object": {"type": "commit", "sha": "a" * 40}},
    )
    with pytest.raises(PolicyError, match="must be annotated"):
        _resolve_annotated_tag_commit("v1.0.0-rc.3")


def test_annotated_tag_resolution_returns_exact_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"

    def metadata(url: str) -> object:
        if "/git/ref/tags/" in url:
            return {"object": {"type": "tag", "sha": "a" * 40}}
        return {"object": {"type": "commit", "sha": commit}}

    monkeypatch.setattr("engineering_policy.release._request_json", metadata)
    assert _resolve_annotated_tag_commit("v1.0.0-rc.3") == commit


def _mock_release_downloads(
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, Path],
    *,
    tag_name: str = "v1.0.0-rc.3",
    draft: bool = False,
    prerelease: bool = True,
) -> list[tuple[str, ...]]:
    base = "https://github.com/GurzuInc/agentic-engineering-workflow/releases/download/v1.0.0-rc.3/"
    release = {
        "tag_name": tag_name,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [{"name": name, "browser_download_url": base + name} for name in sorted(assets)],
    }
    monkeypatch.setattr("engineering_policy.release._request_json", lambda _url: release)

    def download(_url: str, destination: Path) -> None:
        shutil.copyfile(assets[destination.name], destination)

    verified: list[tuple[str, ...]] = []
    monkeypatch.setattr("engineering_policy.release._download_public_asset", download)
    monkeypatch.setattr(
        "engineering_policy.release._verify_release",
        lambda tag, *paths: verified.append((tag, *(path.name for path in paths))),
    )
    monkeypatch.setattr(
        "engineering_policy.release._resolve_annotated_tag_commit",
        lambda _tag: "0123456789abcdef0123456789abcdef01234567",
    )
    return verified


def test_verified_bundle_composes_release_checks_before_yielding(
    monkeypatch: pytest.MonkeyPatch, release_bundle: Path
) -> None:
    assets = {path.name: path for path in release_bundle.parent.iterdir()}
    verified = _mock_release_downloads(monkeypatch, assets)
    with ReleaseClient().verified_bundle(Version.parse("1.0.0-rc.3")) as bundle:
        assert bundle.version == "1.0.0-rc.3"
    assert len(verified) == 1
    assert verified[0][0] == "v1.0.0-rc.3"
    assert set(verified[0][1:]) == set(assets)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"tag_name": "v1.0.0-rc.4"}, "identity mismatch"),
        ({"draft": True}, "still a draft"),
        ({"prerelease": False}, "channel mismatch"),
    ],
)
def test_verified_bundle_rejects_release_identity_and_state_before_download(
    monkeypatch: pytest.MonkeyPatch,
    release_bundle: Path,
    metadata: dict[str, object],
    message: str,
) -> None:
    assets = {path.name: path for path in release_bundle.parent.iterdir()}
    _mock_release_downloads(monkeypatch, assets, **metadata)
    with pytest.raises(PolicyError, match=message):
        with ReleaseClient().verified_bundle(Version.parse("1.0.0-rc.3")):
            pass


def test_verified_bundle_rejects_standalone_updater_that_differs_from_bundle(
    monkeypatch: pytest.MonkeyPatch, release_bundle: Path, tmp_path: Path
) -> None:
    assets: dict[str, Path] = {}
    for source in release_bundle.parent.iterdir():
        destination = tmp_path / source.name
        shutil.copyfile(source, destination)
        assets[source.name] = destination
    pyz = assets["policyctl.pyz"]
    pyz.write_bytes(b"different updater")
    manifest_path = assets["release-manifest.json"]
    manifest = json.loads(manifest_path.read_text())
    manifest["asset_digests"][pyz.name] = hashlib.sha256(pyz.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    checksums = assets["SHA256SUMS"]
    checksum_targets = [assets[name] for name in manifest["asset_digests"]] + [manifest_path]
    checksums.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(checksum_targets, key=lambda item: item.name)
        ),
        encoding="utf-8",
    )
    _mock_release_downloads(monkeypatch, assets)
    with pytest.raises(PolicyError, match="standalone updater differs"):
        with ReleaseClient().verified_bundle(Version.parse("1.0.0-rc.3")):
            pass
