# Agentic Engineering Workflow

The canonical, model-neutral engineering policy and synchronization tooling for Gurzu repositories.

This repository publishes declarative policy bundles and a self-contained `policyctl.pyz`. Consumer repositories pin a major-version-compatible snapshot and commit the generated Codex and Claude adapters for local review.

Release assets and consumer enrollment are intentionally gated until client conformance, provenance, GitHub App, and protected-branch checks pass.

## License

Apache License 2.0. See [LICENSE](LICENSE).
