from pathlib import PurePosixPath

SOURCE_REPOSITORY = "GurzuInc/agentic-engineering-workflow"
PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
SUPPORTED_ADAPTERS = ("codex",)
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
    "schemas/lock.schema.json": "38286eb8fe77634b5a7be50905afb5c0634f7d423a7f75c2a0bb50069a0a3089",
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
        "c651851032ddce6f48659c48dfe5341e61f4e6e69cdd4ce4ff23ab3616872aab"
    ),
}
