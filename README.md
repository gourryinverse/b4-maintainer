# b4-maintainer

Mailing-list maintainer workspace: patch review, bug triage, interactive agents, project tests.

Stage: design. Next milestone: fixture-backed interface prototype.

## Scope

- User-managed project repositories under projects/: Linux, QEMU, ndctl, and others.
- Initial support: mailing-list patch workflows through b4.
- Configurable subsystem subscriptions, ownership rules, refs, prompts, and tests.
- Series browsing, review progress, discussion, and upstream tracking.
- Management and per-series agent sessions; backend selected per session.
- Findings linked to reviews, reproductions, tests, and responses.
- Optional backlog repositories.

Agents start or resume only on maintainer request. Approved work can continue
in the background.

## Requirements

- [Interface](docs/design/interface.md)
- [Workflow and tests](docs/design/workflow.md)
- [Agents](docs/design/agents.md)
- [Architecture](docs/design/architecture.md)
- [Backlogs](docs/design/backlogs.md)
- [Project workspaces](docs/design/projects.md)
- [Contributor instructions](AGENTS.md)

## Dependencies

Pinned sources: b4, liblore, review-prompts, kernel-style, virtme-ng, kres, semcode.
See [setup](deps/README.md).

Development favors improvements in the owning dependencies, maintained through
forks and upstream contributions. Client adapters stay focused on integration.

Workspace paths, LLM providers/credentials, and backlogs are maintainer configuration.
Mail configuration and delivery use b4's existing integrations.
