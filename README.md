# Agentic Engineering Workflow

Generic engineering-policy and workflow tooling for Git repositories. Version
1.0 publishes deterministic policy bundles and a self-contained `policyctl.pyz`
for repository-scoped Codex and Claude workflows.

Functional source is published through an Emitii-linked pull request after the
documentation-only repository genesis. See [RECOVERY.md](RECOVERY.md) for the
unpublished RC1 provenance boundary.

## Supported interface

Version 1.0 supports Codex and Claude adapters. Enrollment is intentionally explicit:

```text
policyctl init \
  --repo <repo> \
  --version <exact-version> \
  --adapters codex,claude \
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

- `preserve` keeps repository-owned `AGENTS.md` and `CLAUDE.md` byte-identical.
- `manual` installs no policy synchronization workflow.
- `unmanaged` neither creates nor modifies `.github/CODEOWNERS`.

The Codex workflow skill is installed at
`.agents/skills/project-engineering-workflow`; the Claude skill is installed at
`.claude/skills/project-engineering-workflow`. `render`, `check`, `doctor`, and
`update` enforce the pinned modes for either or both adapters. The tested minimum
Codex version is `0.147.0`. Claude adapter installation is supported, while live
Claude client validation remains explicit pending follow-up. Supported Python
versions are 3.11 through 3.14.

Version 1.0.1 is the protocol bridge for explicit major updates. Its released
bundle remains schema/protocol v1 and keeps Claude validation `pending`. The
already-trusted updater embeds two complete, exact schema-digest sets: the
released v1 set and the reviewed v2 set in `policy/trusted-schemas/v2`. It
accepts a v2 candidate only when the manifest, policy, migration chain, release
contract, and every schema byte match the reviewed v2 contract. Automatic
updates remain pinned to the enrolled major, and candidate updater bytes are
verified and copied without execution.

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

A stable tag must point to the exact passing RC source commit and include
`Promotes: <exact-rc-tag>` in its annotation. Before publishing stable, the
release workflow verifies the immutable RC release, all four assets, checksums,
build attestations, source commit, and the exact recorded RC smoke result.

Release assets and consumer enrollment remain gated until source, deterministic
build, release integrity, attestation, clean-install, and live Codex validation
pass. Unpublished RC1 is recovery evidence only and was never retroactively
tagged. The failed, unpublished `v1.0.0-rc.2` candidate is retained as immutable
failure evidence and is never altered or reused.

## License

Apache License 2.0. See [LICENSE](LICENSE).
