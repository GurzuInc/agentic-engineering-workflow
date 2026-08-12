from pathlib import PurePosixPath

SOURCE_REPOSITORY = "GurzuInc/agentic-engineering-workflow"
PROTOCOL_VERSION = 2
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_PROTOCOLS = {1: 1, 2: 2}
SUPPORTED_ADAPTERS = ("codex", "claude")
SUPPORTED_GUIDANCE_MODES = ("preserve",)
SUPPORTED_SYNC_MODES = ("manual",)
SUPPORTED_CODEOWNERS_MODES = ("unmanaged",)
BUNDLE_ASSET_TEMPLATE = "engineering-policy-{version}.zip"

CANDIDATE_PREFIXES = (
    PurePosixPath("spec"),
    PurePosixPath("schemas"),
    PurePosixPath("prompts"),
    PurePosixPath("migrations"),
    PurePosixPath("adapters"),
)
CANDIDATE_EXACT_FILES = {
    PurePosixPath("LICENSE"),
    PurePosixPath("NOTICE"),
    PurePosixPath("bin/policyctl.pyz"),
}

MANAGED_EXACT_OUTPUTS = {
    PurePosixPath(".engineering-policy/policy.lock.json"),
}
MANAGED_OUTPUT_PREFIXES = (
    PurePosixPath(".engineering-policy/spec"),
    PurePosixPath(".engineering-policy/bin"),
    PurePosixPath(".codex"),
    PurePosixPath(".claude"),
    PurePosixPath(".agents/skills/project-engineering-workflow"),
)

PROTECTED_CONSUMER_PATHS = {
    PurePosixPath("engineering-policy.project.yaml"),
    PurePosixPath("engineering-policy.project.md"),
    PurePosixPath(".github/CODEOWNERS"),
    PurePosixPath(".github/workflows/engineering-policy-sync.yml"),
}

# Protocol schemas are part of the already-trusted updater contract. A candidate is accepted
# only when every schema byte matches one complete reviewed set for its declared protocol.
TRUSTED_SCHEMA_SETS_SHA256 = {
    1: {
        "schemas/lock.schema.json": (
            "ff2b3b3b203ff9ca14a166888d91e2d269ae0ae3bee2c8acf681009a68f79efb"
        ),
        "schemas/manifest.schema.json": (
            "43d5a808e41546f527ca563e8b41c9277f8cbd2a96f77cdd20eb520b73b0d60a"
        ),
        "schemas/migration.schema.json": (
            "fa4ac7d3fe691712a624017dde1ab002c23ce6c33a83b8d3a5b6c87e3b43a1d6"
        ),
        "schemas/overlay.schema.json": (
            "41bcfd24e04fd3513da7d7a1587d68e5e633562b14bd191cc7480e0499fe0fc6"
        ),
        "schemas/policy.schema.json": (
            "77b1442ca60f6b100e4014c37a690f9d99e658819a75430e363bed1be7ac9067"
        ),
    },
    2: {
        "schemas/lock.schema.json": (
            "59bee7264f69eb555790a0f573103cc69ce450c6f22447454e3932a2310c8be5"
        ),
        "schemas/manifest.schema.json": (
            "6cf07d78759f049f6331e916f58eec67dd4722095a124b349d8590b9f1c1630c"
        ),
        "schemas/migration.schema.json": (
            "f1a14dc1dc4858d73fed461973776e5bc2c0c106ee35224ecfd52bfd46503a4c"
        ),
        "schemas/overlay.schema.json": (
            "41bcfd24e04fd3513da7d7a1587d68e5e633562b14bd191cc7480e0499fe0fc6"
        ),
        "schemas/policy.schema.json": (
            "0a756655f261603f5979897a2306db04f789b3033f88760e10dc34f1e1b5bc5d"
        ),
    },
}
