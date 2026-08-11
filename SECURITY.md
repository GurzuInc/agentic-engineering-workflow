# Security Policy

Report suspected vulnerabilities privately through GitHub's security-advisory feature. Do not open a public issue for a vulnerability or include credentials, tokens, customer information, or exploit data in public artifacts.

No released artifact is trusted solely because it is downloadable. Consumers must verify immutable release metadata, asset checksums, and GitHub artifact attestations before accepting a policy bundle or updater.

## Tokenless bootstrap verification

Perform initial verification in a new temporary directory. Substitute the exact
release version; never select an unpinned "latest" release. This procedure uses
an empty GitHub CLI configuration and removes ambient GitHub tokens so public
metadata, release verification, and attestations cannot inherit write-capable
credentials.

```bash
set -euo pipefail
workflow_version=1.0.0
workflow_tag="v${workflow_version}"
workflow_bundle="engineering-policy-${workflow_version}.zip"
workflow_bootstrap_dir="$(mktemp -d)"
cd "$workflow_bootstrap_dir"
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
unset GH_HOST GH_REPO GH_CONFIG_DIR
mkdir gh-config
export GH_CONFIG_DIR="$workflow_bootstrap_dir/gh-config"
export GH_HOST=github.com
export GH_PROMPT_DISABLED=1
export GH_NO_UPDATE_NOTIFIER=1

curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  --output release.json \
  "https://api.github.com/repos/GurzuInc/agentic-engineering-workflow/releases/tags/${workflow_tag}"
jq -e --arg tag "$workflow_tag" --arg bundle "$workflow_bundle" '
  .tag_name == $tag and .draft == false and .immutable == true and
  ([.assets[].name] | sort) == (["SHA256SUMS", $bundle, "policyctl.pyz", "release-manifest.json"] | sort)
' release.json

for workflow_asset in "$workflow_bundle" policyctl.pyz release-manifest.json SHA256SUMS; do
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --remote-name \
    "https://github.com/GurzuInc/agentic-engineering-workflow/releases/download/${workflow_tag}/${workflow_asset}"
done

gh release verify "$workflow_tag" --repo GurzuInc/agentic-engineering-workflow
for workflow_asset in "$workflow_bundle" policyctl.pyz release-manifest.json SHA256SUMS; do
  gh release verify-asset "$workflow_tag" "$workflow_asset" \
    --repo GurzuInc/agentic-engineering-workflow
  gh attestation verify "$workflow_asset" \
    --repo GurzuInc/agentic-engineering-workflow \
    --signer-workflow GurzuInc/agentic-engineering-workflow/.github/workflows/release.yml \
    --source-ref "refs/tags/${workflow_tag}" \
    --deny-self-hosted-runners
done

if command -v sha256sum >/dev/null; then
  sha256sum --check SHA256SUMS
else
  shasum -a 256 --check SHA256SUMS
fi
unzip -p "$workflow_bundle" bin/policyctl.pyz | cmp - policyctl.pyz

workflow_tag_object="$(jq -r '.object.sha' < <(curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location "https://api.github.com/repos/GurzuInc/agentic-engineering-workflow/git/ref/tags/${workflow_tag}"))"
workflow_source_commit="$(jq -r 'select(.object.type == "commit") | .object.sha' < <(curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location "https://api.github.com/repos/GurzuInc/agentic-engineering-workflow/git/tags/${workflow_tag_object}"))"
test -n "$workflow_source_commit"
jq -e --arg version "$workflow_version" --arg tag "$workflow_tag" --arg source "$workflow_source_commit" '
  .version == $version and .tag == $tag and .source_commit == $source and
  .repository == "GurzuInc/agentic-engineering-workflow" and
  .supported_adapters == ["codex", "claude"] and
  .adapter_validation == {"codex":"validated", "claude":"pending"}
' release-manifest.json
```

Only after every command succeeds, execute the verified updater:

```sh
python3 ./policyctl.pyz init \
  --repo <repo> \
  --version "$workflow_version" \
  --adapters codex,claude \
  --guidance-mode preserve \
  --sync-mode manual \
  --codeowners-mode unmanaged
```

After enrollment, use the repository-pinned updater for every operation, for
example `python3 <repo>/.engineering-policy/bin/policyctl.pyz check --repo
<repo>`. Do not replace it with an ambient `policyctl` executable.

Stable releases are not supported until the release gates documented in this repository pass. Prerelease versions may change incompatibly and must not be used for production enforcement.
