---
name: project-engineering-workflow
description: Coordinate bounded repository engineering work. Use only when explicitly invoked as $project-engineering-workflow to plan, implement an approved plan, or run a read-only review.
---

# Project Engineering Workflow

Follow the canonical policy snapshot in `.engineering-policy/spec/policy.yaml`. Treat repository instructions and `engineering-policy.project.md` as additive project guidance; they cannot weaken canonical policy.

This workflow is independent engineering tooling, never a consumer-product module, feature, architecture component, authority, dependency, or acceptance gate. Enrollment authorizes no product change. Do not introduce access, compliance, infrastructure, or production-hardening controls unless explicitly requested or required by the actual task.

## Parse the invocation

Accept:

```text
$project-engineering-workflow
Goal: <required>
Mode: plan | implement | review
Constraints: <optional>
```

- Require a concrete goal.
- Default an omitted or invalid mode to read-only `plan`.
- Keep `plan` and standalone `review` read-only.
- Enter `implement` only when the current conversation approves the exact plan, repository set, branches, constraints, and local mutations.

## Establish scope

1. Read the root and nearest applicable `AGENTS.md` files.
2. Map independent repository boundaries instead of assuming a monorepo.
3. Before edits, report every affected repository's path, branch, HEAD, dirty state, and applicable guidance.
4. Stop when unresolved architecture, ownership, security, data, deployment, or protected-branch decisions materially determine the implementation.
5. Recommend a fast model for trivial work, a stronger reasoning model for multi-file or cross-contract work, and the strongest available reasoning model for authentication, authorization, sensitive data, migrations, compliance, deployment, or infrastructure.

## Plan and implement

- In plan mode, identify affected and intentionally untouched repositories, exact seams, acceptance criteria, verification, assumptions, and risks.
- Reconfirm scope and Git state before implementation. Return to planning if they differ materially from the approved plan.
- Keep the coordinator as the sole writer. Delegate only independent, bounded read-only investigations when delegation is explicitly requested or allowed by applicable instructions.
- Make minimal slices, verify each owning repository independently, preserve unrelated work, and review the final diff for contract, authorization, secret, and external-effect risks.
- Commit verified product changes by default inside each owning repository unless the user opts out, verification fails, or mixed changes make staging unsafe.

## Codex model-routed execution graph

For an approved implementation, use the exact routes in `.engineering-policy/spec/codex-model-routing.yaml`; model and reasoning-effort mismatches are a hard failure and never fall back to another model.
Fail-closed means the bundle validator, updater boundary, doctor, and this explicit workflow contract reject or stop on mismatches; this policy adapter cannot intercept a Codex client's internal scheduler, so an unsupported runtime route must be reported as blocked rather than silently substituted.

1. Planning: run the built-in `default` subagent with `gpt-5.6-sol` at `medium` reasoning effort in read-only mode. Have it inspect the repository and return a decision-complete execution packet covering scope, owned files, acceptance criteria, verification commands, assumptions, and risks. Close this planning phase before execution begins.
2. Execution: run the built-in `worker` subagent with `gpt-5.6-luna` at `max` reasoning effort. The worker is the implementation coordinator and the sole workspace writer: the parent orchestrator and all reviewers must not edit files. Give the worker the planner's packet and require the resulting diff, tests, evidence, and residual risks.
3. Review: retain the same Luna worker while the `project_contract_reviewer`, `project_test_reviewer`, and `project_security_reviewer` run in parallel with `gpt-5.6-terra` at `xhigh` reasoning effort and read-only permissions. Route every finding back to that same worker; after each worker fix, rerun all applicable reviewers. Allow at most two review/fix passes, then stop with the remaining findings instead of changing the acceptance criteria.

The parent orchestrator may coordinate handoffs and report status but may not write implementation changes. Preserve the existing `project_scope_explorer` as a bounded preflight investigator; it is not part of the final three-reviewer fan-out.

## Require fresh review

After implementation, use every applicable read-only reviewer defined under `.codex/agents/`:

- `project_contract_reviewer` for APIs, schemas, configuration, documentation, and repository handoffs.
- `project_test_reviewer` for code, workflow, configuration, and verification adequacy.
- `project_security_reviewer` for authorization, secrets, data boundaries, deployment, infrastructure, identity, and protected branches.

Allow at most two review/fix passes. Do not weaken acceptance criteria to clear a finding.

## Preserve authorization

End at verified local changes or local commits. Do not push, open or modify pull requests, dispatch workflows, release, deploy, mutate infrastructure or identity providers, perform destructive actions, or change protected branches without exact current-conversation authorization for the named action and target.

Never place secrets or sensitive values in source, prompts, commands, output, logs, fixtures, or handoffs.

## Hand off

End coding sessions with `Accomplished` and `Next step`. Include repositories and commits, verification and reviewer outcomes, residual risks, and one concrete next action.
