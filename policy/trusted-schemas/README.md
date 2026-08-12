# Trusted future schema sets

These files are reviewed source evidence for schema digests embedded in the
already-trusted updater. They are not collected into the current release bundle.

`v2/` is the complete protocol-v2 schema set accepted by the v1.0.2 bridge. A
future protocol-v2 release must copy these files byte-for-byte into
`policy/schemas/`; any change requires a new reviewed bridge release and a new
exact digest set. The protocol-v2 overlay schema intentionally preserves the
repository-owned overlay's schema marker so an explicit major update can replace
the managed snapshot without rewriting `engineering-policy.project.yaml`.
