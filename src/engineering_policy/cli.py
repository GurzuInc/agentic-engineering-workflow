from __future__ import annotations

import argparse
import sys
from pathlib import Path

from engineering_policy.doctor import run_doctor
from engineering_policy.errors import PolicyError
from engineering_policy.operations import apply_update, initialize
from engineering_policy.release import ReleaseClient
from engineering_policy.rendering import check_repository, load_lock, render_current
from engineering_policy.repository import require_git_repository
from engineering_policy.semver import Version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="policyctl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="enroll a clean Git repository")
    init.add_argument("--repo", type=Path, required=True)
    init.add_argument("--version", required=True)
    init.add_argument("--adapters", required=True, help="comma-separated: codex,claude")
    init.add_argument("--guidance-mode", required=True, choices=("preserve",))
    init.add_argument("--sync-mode", required=True, choices=("manual",))
    init.add_argument("--codeowners-mode", required=True, choices=("unmanaged",))

    render = subparsers.add_parser("render", help="regenerate adapters from the pinned snapshot")
    render.add_argument("--repo", type=Path, default=Path.cwd())

    check = subparsers.add_parser("check", help="validate schemas, checksums, and generated drift")
    check.add_argument("--repo", type=Path, default=Path.cwd())

    doctor = subparsers.add_parser(
        "doctor", help="diagnose client, trust, model, and skill conflicts"
    )
    doctor.add_argument("--repo", type=Path, default=Path.cwd())

    update = subparsers.add_parser("update", help="verify and apply a canonical release snapshot")
    update.add_argument("--repo", type=Path, default=Path.cwd())
    update.add_argument("--version", help="exact version; required for a major upgrade or rollback")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _init(args)
        if args.command == "render":
            repo = require_git_repository(args.repo)
            changed = render_current(repo)
            _print_paths("rendered", changed)
            return 0
        if args.command == "check":
            repo = require_git_repository(args.repo)
            issues = check_repository(repo)
            if issues:
                for issue in issues:
                    print(f"ERROR {issue}")
                return 1
            print("OK policy snapshot is conformant and generated files are deterministic")
            return 0
        if args.command == "doctor":
            repo = require_git_repository(args.repo)
            diagnostics = run_doctor(repo)
            for item in diagnostics:
                print(f"{item.severity.upper()} {item.code}: {item.message}")
            return 1 if any(item.severity == "error" for item in diagnostics) else 0
        if args.command == "update":
            return _update(args)
    except PolicyError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


def _init(args: argparse.Namespace) -> int:
    version = Version.parse(args.version)
    adapters = tuple(item.strip() for item in args.adapters.split(",") if item.strip())
    client = ReleaseClient()
    with client.verified_bundle(version) as bundle:
        changed = initialize(
            args.repo,
            bundle,
            adapters,
            guidance_mode=args.guidance_mode,
            sync_mode=args.sync_mode,
            codeowners_mode=args.codeowners_mode,
        )
    _print_paths(f"initialized {version}", changed)
    return 0


def _update(args: argparse.Namespace) -> int:
    repo = require_git_repository(args.repo)
    lock = load_lock(repo)
    issues = check_repository(repo)
    if issues:
        raise PolicyError("current snapshot is not conformant: " + "; ".join(issues))
    current = Version.parse(lock["version"])
    client = ReleaseClient()
    explicit = args.version is not None
    version = (
        Version.parse(args.version)
        if explicit
        else client.resolve_latest(major=current.major, channel=lock["channel"], current=current)
    )
    if version == current:
        print(f"OK already at latest compatible release {version}")
        return 0
    with client.verified_bundle(version) as bundle:
        changed = apply_update(repo, bundle, explicit_version=explicit)
    _print_paths(f"updated to {version}", changed)
    return 0


def _print_paths(action: str, paths: list[str]) -> None:
    print(f"OK {action}")
    for path in paths:
        print(f"  {path}")


if __name__ == "__main__":
    raise SystemExit(main())
