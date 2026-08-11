# Canonical Engineering Policy Repository

## Scope and ownership

- This repository is the source of truth for the declarative engineering policy, Codex adapter, trusted `policyctl` updater, release tooling, schemas, migrations, and consumer bootstrap assets.
- Keep canonical source under `policy/`, updater code under `src/engineering_policy/`, build logic under `scripts/`, and conformance coverage under `tests/`.
- Treat generated `dist/` contents as local evidence only. Never edit or commit them as source.
- Do not enroll this repository into its own unpublished policy snapshot. Root `AGENTS.md` is repository governance, not a generated consumer adapter.
- Version 1.0 supports Codex only. Claude files, diagnostics, or compatibility behavior are outside the supported interface.

## Change discipline

- Preserve protocol and schema version 1 unless an explicitly approved compatibility design includes the migration and trusted-updater transition.
- Keep public CLI commands and flags backward compatible except where a reviewed release plan explicitly removes an unsafe interface.
- Candidate bundles may change only the exact declarative allowlist. They must never change consumer workflows, CODEOWNERS, project overlays, Git metadata, or paths outside the managed namespace.
- Never execute a candidate updater. The already-trusted consumer updater verifies and copies candidate bytes.
- Keep archives deterministic and include `LICENSE` and `NOTICE` in both the zipapp and outer bundle.

## Verification

Run the following before committing source changes:

```text
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run --isolated --python 3.11 --locked pytest
uv run python scripts/build_release.py
python3 dist/policyctl.pyz --help
git diff --check
```

Also parse edited workflow YAML, build twice, and inspect archive paths, modes, checksums, licenses, release-negative cases, and cross-build determinism. Security-sensitive changes require fresh contract, test, and security review against the actual diff.

## Release and authorization safety

- Local implementation and a passing build do not authorize a push, pull request, tag, release, GitHub settings change, workflow dispatch, or consumer enrollment.
- Verify tag, exact source commit, protected default-branch reachability, workflow identity, GitHub-hosted runner provenance, checksums, unique release assets, and attestations before accepting a release.
- Public asset downloads stay tokenless. Never print credentials, tokens, sensitive attestation context, or unredacted environment values.
