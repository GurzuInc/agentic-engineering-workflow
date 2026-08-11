from pathlib import PurePosixPath

SOURCE_REPOSITORY = "GurzuInc/agentic-engineering-workflow"
PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
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

# Protocol-v1 schemas are part of the trusted updater contract. Candidate releases may carry
# snapshots of them, but cannot loosen them until a reviewed updater becomes trusted after merge.
TRUSTED_SCHEMA_SHA256 = {
    "schemas/lock.schema.json": "ff2b3b3b203ff9ca14a166888d91e2d269ae0ae3bee2c8acf681009a68f79efb",
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
}
