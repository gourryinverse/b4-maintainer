# b4-maintainer

Mailing-list maintainer workspace: patch review, bug triage, interactive agents, project tests.

Stage: fixture-backed interface prototype (browse screen).

Python, Textual, GPL-2.0-or-later. See [decisions](docs/design/architecture.md#decisions).

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
- [Sashiko feedback](docs/design/sashiko.md)
- [External editor drafts](docs/design/editor.md)
- [Open decisions](docs/design/open.md)
- [Contributor instructions](AGENTS.md)

## Dependencies

Pinned sources: b4, liblore, review-prompts, kernel-style, virtme-ng, kres, semcode, sashiko.
See [setup](deps/README.md).

Development favors improvements in the owning dependencies, maintained through
forks and upstream contributions. Client adapters stay focused on integration.

Workspace paths, LLM providers/credentials, and backlogs are maintainer configuration.
Mail configuration and delivery use b4's existing integrations.

## Development

    git submodule update --init --recursive
    /usr/bin/python3 -m venv .venv  # a real interpreter, not a managed python3 symlink
    .venv/bin/pip install -e deps/liblore -e 'deps/b4[tui]' -e . pytest pytest-asyncio ruff
    .venv/bin/python -m pytest
    .venv/bin/b4-maintainer --demo .local/demo

- `b4-maintainer` in a workspace with `workspace.toml` opens it live: `r` fetches profile
  lists and your own mail from lore, `t` tracks the selected series through b4 (To Review),
  `x` hides it, `R` marks it read, `/` filters by regex, `r` in the reader drafts a reply beside
  the thread and `ctrl+s` sends it through b4 (`--email-dry-run` to test); Responses lists
  unread replies to your mail (b4's Seen flag).
- `b4-maintainer DIR` opens a fixture workspace: `workspace.json`, `mail/*.mbx`, `state.json`.
- `b4-maintainer project add ID --list LIST --ref LABEL:REF:AUTHORITY` registers the
  existing checkout at `projects/ID` in `workspace.toml`; b4 enrolls it with state in `var/`.
- Mail is parsed offline through pinned b4; keep real mail under ignored `.local/`.
- A hang: `kill -USR1 <pid>` writes every thread's stack to
  `/tmp/b4-maintainer-<pid>.stacks` without stopping the app.
