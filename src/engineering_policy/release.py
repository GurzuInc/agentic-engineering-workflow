from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from engineering_policy.bundle import Bundle
from engineering_policy.constants import BUNDLE_ASSET_TEMPLATE, SOURCE_REPOSITORY
from engineering_policy.errors import PolicyError
from engineering_policy.repository import atomic_write_path
from engineering_policy.semver import Version, choose_latest

_API_ROOT = f"https://api.github.com/repos/{SOURCE_REPOSITORY}"
_SIGNER_WORKFLOW = f"{SOURCE_REPOSITORY}/.github/workflows/release.yml"
_MAX_RELEASE_ASSET_SIZE = 150 * 1024 * 1024
_MAX_METADATA_SIZE = 2 * 1024 * 1024
_NETWORK_IDLE_TIMEOUT = 5
_METADATA_DEADLINE = 30
_ASSET_DEADLINE = 60
_SUPPORTED_PYTHON = ["3.11", "3.12", "3.13", "3.14"]
_RECOVERY = {
    "original_source_commit": "eb79ceb19b1e82ac5f504170ab56abc20e4f484a",
    "original_source_is_ancestor": False,
    "unpublished_rc1_bundle_sha256": (
        "181bc71c4087640685d7bfd8cc575e90afa68a876dc6e50c8a66a0d4f3496ca2"
    ),
    "unpublished_rc1_updater_sha256": (
        "7c2e39091d855a66e8f30fde232a447e916b1b04a8d5ad826fd6a7dd052f3376"
    ),
    "unpublished_rc1_was_tagged": False,
}


class ReleaseClient:
    def resolve_latest(self, *, major: int, channel: str, current: Version) -> Version:
        payload = _request_json(f"{_API_ROOT}/releases?per_page=100")
        if not isinstance(payload, list):
            raise PolicyError("GitHub releases response is invalid")
        releases = []
        for item in payload:
            tag_name, draft, _prerelease = _validate_release_summary(item)
            if not draft:
                releases.append(tag_name)
        return choose_latest(releases, major=major, channel=channel, current=current)

    @contextmanager
    def verified_bundle(self, version: Version) -> Iterator[Bundle]:
        tag = f"v{version}"
        release = _request_json(f"{_API_ROOT}/releases/tags/{tag}")
        tag_name, draft, prerelease = _validate_release_summary(release)
        if tag_name != tag:
            raise PolicyError(f"release identity mismatch for {tag}")
        if draft:
            raise PolicyError(f"release {tag} is still a draft")
        if prerelease != (not version.stable):
            raise PolicyError(f"release channel mismatch for {tag}")
        bundle_name = BUNDLE_ASSET_TEMPLATE.format(version=version)
        pyz_name = "policyctl.pyz"
        manifest_name = "release-manifest.json"
        checksums_name = "SHA256SUMS"
        required = {bundle_name, pyz_name, manifest_name, checksums_name}
        assets = _required_assets(release, tag, required)
        with tempfile.TemporaryDirectory(prefix="policyctl-release-") as temporary:
            work = Path(temporary).resolve()
            paths = {name: work / name for name in required}
            for name in sorted(required):
                _download_public_asset(assets[name], paths[name])
            _verify_release(tag, *(paths[name] for name in sorted(required)))
            bundle_path = paths[bundle_name]
            pyz_path = paths[pyz_name]
            _validate_release_artifacts(
                version,
                tag,
                bundle_path,
                pyz_path,
                paths[manifest_name],
                paths[checksums_name],
            )
            bundle = Bundle.load(bundle_path)
            if bundle.version != str(version):
                raise PolicyError("downloaded bundle version differs from requested release")
            if bundle.files.get("bin/policyctl.pyz") != pyz_path.read_bytes():
                raise PolicyError(
                    "standalone updater differs from the updater embedded in the bundle"
                )
            yield bundle


def _validate_release_artifacts(
    version: Version,
    tag: str,
    bundle: Path,
    pyz: Path,
    release_manifest: Path,
    checksums: Path,
) -> None:
    expected_checksums = {
        bundle.name: hashlib.sha256(bundle.read_bytes()).hexdigest(),
        pyz.name: hashlib.sha256(pyz.read_bytes()).hexdigest(),
        release_manifest.name: hashlib.sha256(release_manifest.read_bytes()).hexdigest(),
    }
    actual_checksums: dict[str, str] = {}
    try:
        lines = checksums.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PolicyError("SHA256SUMS is not UTF-8") from exc
    for line in lines:
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9._-]+)", line)
        if match is None:
            raise PolicyError("SHA256SUMS contains an invalid line")
        digest, name = match.groups()
        if name in actual_checksums:
            raise PolicyError("SHA256SUMS contains a duplicate asset")
        actual_checksums[name] = digest
    if actual_checksums != expected_checksums:
        raise PolicyError("SHA256SUMS does not exactly match the released assets")

    try:
        manifest = json.loads(release_manifest.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError("release-manifest.json is invalid") from exc
    expected_keys = {
        "schema_version",
        "protocol_version",
        "repository",
        "version",
        "tag",
        "channel",
        "source_commit",
        "asset_digests",
        "supported_adapters",
        "supported_python",
        "recovery",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise PolicyError("release manifest has unknown or missing fields")
    expected_channel = "stable" if version.stable else "prerelease"
    if (
        manifest["schema_version"] != 1
        or manifest["protocol_version"] != 1
        or manifest["repository"] != SOURCE_REPOSITORY
        or manifest["version"] != str(version)
        or manifest["tag"] != tag
        or manifest["channel"] != expected_channel
        or manifest["supported_adapters"] != ["codex"]
        or manifest["supported_python"] != _SUPPORTED_PYTHON
        or manifest["recovery"] != _RECOVERY
    ):
        raise PolicyError("release manifest contract does not match the requested release")
    if manifest["asset_digests"] != {
        bundle.name: expected_checksums[bundle.name],
        pyz.name: expected_checksums[pyz.name],
    }:
        raise PolicyError("release manifest asset digests do not match released bytes")
    commit = _resolve_annotated_tag_commit(tag)
    if manifest["source_commit"] != commit:
        raise PolicyError("release manifest source commit differs from the annotated tag")


def _resolve_annotated_tag_commit(tag: str) -> str:
    ref = _request_json(f"{_API_ROOT}/git/ref/tags/{tag}")
    if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
        raise PolicyError("release tag reference metadata is invalid")
    ref_object = ref["object"]
    if ref_object.get("type") != "tag" or not isinstance(ref_object.get("sha"), str):
        raise PolicyError("release tag must be annotated")
    tag_object = _request_json(f"{_API_ROOT}/git/tags/{ref_object['sha']}")
    if not isinstance(tag_object, dict) or not isinstance(tag_object.get("object"), dict):
        raise PolicyError("annotated tag metadata is invalid")
    target = tag_object["object"]
    commit = target.get("sha")
    if target.get("type") != "commit" or not isinstance(commit, str):
        raise PolicyError("annotated tag does not point to a commit")
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise PolicyError("annotated tag commit is invalid")
    return commit


def _download_public_asset(url: str, destination: Path) -> None:
    if not url.startswith(f"https://github.com/{SOURCE_REPOSITORY}/releases/download/"):
        raise PolicyError("release asset URL does not belong to the canonical repository")
    request = urllib.request.Request(  # noqa: S310 - exact HTTPS release prefix checked above
        url, headers={"User-Agent": "policyctl/1"}
    )
    deadline = time.monotonic() + _ASSET_DEADLINE
    try:
        with urllib.request.urlopen(request, timeout=_NETWORK_IDLE_TIMEOUT) as response:  # noqa: S310
            if not response.geturl().split("?", 1)[0].startswith("https://"):
                raise PolicyError("release asset redirected to an insecure URL")
            content = _read_bounded_response(
                response,
                maximum=_MAX_RELEASE_ASSET_SIZE,
                deadline=deadline,
                label="release asset",
            )
        atomic_write_path(destination, content)
    except (OSError, urllib.error.URLError) as exc:
        raise PolicyError(f"failed to download public release asset: {exc}") from exc


def _verify_release(tag: str, *assets: Path) -> None:
    gh = shutil.which("gh")
    if gh is None:
        raise PolicyError("GitHub CLI is required for release and attestation verification")
    environment = _isolated_gh_environment()
    commands = [[gh, "release", "verify", tag, "--repo", SOURCE_REPOSITORY]]
    for asset in assets:
        commands.append(
            [gh, "release", "verify-asset", tag, str(asset), "--repo", SOURCE_REPOSITORY]
        )
        commands.append(
            [
                gh,
                "attestation",
                "verify",
                str(asset),
                "--repo",
                SOURCE_REPOSITORY,
                "--signer-workflow",
                _SIGNER_WORKFLOW,
                "--source-ref",
                f"refs/tags/{tag}",
                "--deny-self-hosted-runners",
            ]
        )
    with tempfile.TemporaryDirectory(prefix="policyctl-gh-config-") as config_dir:
        environment["GH_CONFIG_DIR"] = config_dir
        for command in commands:
            try:
                result = subprocess.run(  # noqa: S603 - command is fixed and version-validated
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                raise PolicyError("release provenance verification timed out") from exc
            except OSError as exc:
                raise PolicyError("release provenance verification could not be started") from exc
            if result.returncode != 0:
                summary = (result.stderr or result.stdout).strip().splitlines()
                detail = summary[-1] if summary else "verification failed"
                raise PolicyError(f"release provenance verification failed: {detail}")


def _request_json(url: str) -> object:
    if not _is_canonical_api_url(url):
        raise PolicyError("GitHub release metadata URL is not canonical")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "policyctl/1"}
    read_token = os.environ.get("POLICYCTL_GITHUB_READ_TOKEN")
    if read_token:
        if "\r" in read_token or "\n" in read_token:
            raise PolicyError("POLICYCTL_GITHUB_READ_TOKEN contains invalid characters")
        headers["Authorization"] = f"Bearer {read_token}"
    request = urllib.request.Request(  # noqa: S310 - canonical origin is checked above
        url,
        headers=headers,
    )
    opener = urllib.request.build_opener(_CanonicalApiRedirectHandler())
    deadline = time.monotonic() + _METADATA_DEADLINE
    try:
        with opener.open(request, timeout=_NETWORK_IDLE_TIMEOUT) as response:
            if not _is_canonical_api_url(response.geturl()):
                raise PolicyError("GitHub release metadata redirected outside the canonical API")
            content = _read_bounded_response(
                response,
                maximum=_MAX_METADATA_SIZE,
                deadline=deadline,
                label="GitHub release metadata",
            )
            return json.loads(content)
    except (OSError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"failed to read public GitHub release metadata: {exc}") from exc


class _CanonicalApiRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if not _is_canonical_api_url(new_url):
            raise PolicyError("GitHub release metadata redirect left the canonical API")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def _is_canonical_api_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.github.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _read_bounded_response(response, *, maximum: int, deadline: float, label: str) -> bytes:
    content = bytearray()
    read = getattr(response, "read1", None)
    if read is None:
        read = response.read
    while True:
        if time.monotonic() >= deadline:
            raise PolicyError(f"{label} exceeded the total download deadline")
        chunk = read(min(1024 * 1024, maximum + 1 - len(content)))
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > maximum:
            raise PolicyError(f"{label} exceeds the allowed download size")


def _isolated_gh_environment() -> dict[str, str]:
    environment = os.environ.copy()
    read_token = environment.pop("POLICYCTL_GITHUB_READ_TOKEN", None)
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GH_REPO",
        "GH_CONFIG_DIR",
    ):
        environment.pop(name, None)
    environment["GH_HOST"] = "github.com"
    environment["GH_PROMPT_DISABLED"] = "1"
    environment["GH_NO_UPDATE_NOTIFIER"] = "1"
    if read_token:
        environment["GH_TOKEN"] = read_token
    return environment


def _validate_release_summary(release: object) -> tuple[str, bool, bool]:
    if not isinstance(release, dict):
        raise PolicyError("GitHub release metadata must be an object")
    tag_name = release.get("tag_name")
    draft = release.get("draft")
    prerelease = release.get("prerelease")
    if not isinstance(tag_name, str) or not tag_name:
        raise PolicyError("GitHub release metadata has an invalid tag name")
    if type(draft) is not bool or type(prerelease) is not bool:
        raise PolicyError("GitHub release metadata has invalid release flags")
    return tag_name, draft, prerelease


def _required_assets(release: dict, tag: str, required: set[str]) -> dict[str, str]:
    asset_items = release.get("assets")
    if not isinstance(asset_items, list):
        raise PolicyError(f"release {tag} has invalid asset metadata")
    matches: dict[str, list[str]] = {name: [] for name in required}
    seen_names: set[str] = set()
    for item in asset_items:
        if not isinstance(item, dict):
            raise PolicyError(f"release {tag} has invalid asset metadata")
        name = item.get("name")
        url = item.get("browser_download_url")
        if not isinstance(name, str) or not name or not isinstance(url, str) or not url:
            raise PolicyError(f"release {tag} has invalid asset metadata")
        if name in seen_names:
            raise PolicyError(f"release {tag} contains duplicate asset names")
        seen_names.add(name)
        if name in matches:
            matches[name].append(url)
    if seen_names != required:
        raise PolicyError(f"release {tag} asset names do not exactly match the release contract")
    invalid = sorted(name for name, urls in matches.items() if len(urls) != 1)
    if invalid:
        raise PolicyError(f"release {tag} does not contain one unique copy of each required asset")
    return {name: urls[0] for name, urls in matches.items()}
