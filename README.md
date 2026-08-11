# Agentic Engineering Workflow

Generic engineering-policy and workflow tooling for Git repositories. Version
1.0 publishes deterministic policy bundles and a self-contained `policyctl.pyz`
for repository-scoped Codex workflows.

Functional source is published through an Emitii-linked pull request after the
documentation-only repository genesis. See [RECOVERY.md](RECOVERY.md) for the
unpublished RC1 provenance boundary.

## Supported interface

Version 1.0 supports Codex only. Enrollment is intentionally explicit:

```text
policyctl init \
  --repo <repo> \
  --version <exact-version> \
  --adapters codex \
  --guidance-mode preserve \
  --sync-mode manual \
  --codeowners-mode unmanaged
```

For initial enrollment, `policyctl` above means the externally verified
downloaded `policyctl.pyz`, invoked as `python3 ./policyctl.pyz`. It does not
mean an ambient executable found on `PATH`. Follow the bootstrap verification
procedure in [SECURITY.md](SECURITY.md) before the first execution. After
enrollment, invoke `python3 <repo>/.engineering-policy/bin/policyctl.pyz` for
`render`, `check`, `doctor`, and `update`; that updater is pinned by the policy
lock.

The three modes are persisted in both `engineering-policy.project.yaml` and
`.engineering-policy/policy.lock.json`:

- `preserve` keeps the repository-owned `AGENTS.md` byte-identical.
- `manual` installs no policy synchronization workflow.
- `unmanaged` neither creates nor modifies `.github/CODEOWNERS`.

The Codex workflow skill is installed at
`.agents/skills/project-engineering-workflow`. `render`, `check`, `doctor`, and
`update` enforce the pinned modes. Claude is unsupported in v1.0 and is rejected
as an adapter. The tested minimum Codex version is `0.147.0`; supported Python
versions are 3.11 through 3.14.

`--adopt-existing` is not supported because its implicit guidance and ownership
rewrites are outside this release contract.

## Release contract

Each release contains exactly four assets:

- `engineering-policy-<version>.zip`
- `policyctl.pyz`
- `release-manifest.json`
- `SHA256SUMS`

The updater requires a published immutable release, one unique copy of every
asset, checksum and manifest agreement, an annotated tag bound to the manifest's
exact source commit, GitHub-hosted release-workflow attestations for every asset,
and byte identity between the standalone and embedded updater.

Release assets and consumer enrollment remain gated until source, deterministic
build, release integrity, attestation, clean-install, and live Codex validation
pass. Unpublished RC1 is recovery evidence only and was never retroactively
tagged.

## License

Apache License 2.0. See [LICENSE](LICENSE).
