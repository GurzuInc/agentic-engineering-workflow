---
name: project-engineering-workflow
description: Coordinate bounded repository engineering work in plan, implement, or review mode.
disable-model-invocation: true
---

# Project Engineering Workflow

Follow `.engineering-policy/spec/policy.yaml` and repository-owned extension and guidance files.

Accept `Goal`, `Mode` (`plan`, `implement`, or `review`), and optional `Constraints`. Require a concrete goal, default invalid or omitted modes to read-only plan, and enter implement only with current-conversation approval of the exact plan, repository set, branches, constraints, and local mutations.

Map repository boundaries and applicable guidance. Report branch, HEAD, dirty state, ownership, acceptance criteria, verification, assumptions, and risks before mutation. Keep one coordinator as writer and use applicable read-only agents from `.claude/agents/` after implementation.

End at verified local changes or commits. Require exact authorization for pushes, pull requests, workflow dispatches, releases, deployments, infrastructure or identity mutations, destructive actions, and protected-branch changes. Never expose secrets or sensitive values.

End coding sessions with `Accomplished` and `Next step`, including repositories, commits, verification, review outcomes, residual risk, and one concrete next action.
