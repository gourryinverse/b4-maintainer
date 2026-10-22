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

## Setup

Python 3.11 or newer.

    git clone <b4-maintainer repository> b4-maintainer && cd b4-maintainer
    ./setup.sh        # first run creates setup.conf: set STATE_REPO, then run it again
    ./b4-maintainer

- `setup.conf` (ignored by Git; from `setup.conf.example`): `STATE_REPO`, the private state
  repository; `PYTHON`, the interpreter (default: newest `python3.1x`); `DEV_TOOLS=yes` for
  pytest and ruff.
- An optional site file adapts setup to its environment without changes here:
  `setup.site.conf` (ignored), or the file `SITE_CONF` names (e.g. one distributed inside an
  organisation). `setup.sh` and `./b4-maintainer` source it: it may export environment
  (proxies), set a default `PYTHON`, add `PIP_ARGS`, or define `site_install` to replace pip.
  Without one, setup uses plain pip.
- `./setup.sh` initializes submodules, creates `.venv` and installs the pinned packages,
  clones `STATE_REPO` as `state/`, and runs `b4-maintainer setup`: missing project
  checkouts are cloned blobless as recorded (Linux takes a while), enrolled with workspace
  b4 state, and synced b4 state is imported. Existing checkouts and `state/` are left as
  they are; safe to run again.
- `./b4-maintainer` runs `.venv`'s b4-maintainer on this workspace from any directory,
  active virtual environment or not; relative paths in its arguments are from the workspace.
- Per machine, not synced: Git identity and b4's `sendemail.*` settings (SMTP
  credentials), drafts, caches, and review branches.

## Development

    ./setup.sh        # with DEV_TOOLS=yes in setup.conf
    .venv/bin/python -m pytest
    ./b4-maintainer --demo .local/demo

- `b4-maintainer` in a workspace with `workspace.toml` (or `state/workspace.toml`) opens it
  live: `r` fetches profile lists and your own mail from lore, `t` tracks the selected series
  through b4 (To Review), `x` hides it, `R` marks it read, `/` filters by regex, `r` in the
  reader drafts a reply beside the thread and `ctrl+s` sends it through b4
  (`--email-dry-run` to test); Responses lists unread replies to your mail (b4's Seen flag).
- Workspace state syncs through a private state repository cloned as `state/` (see
  [sync](docs/design/projects.md#multi-machine-sync) and [setup](#setup));
  `state/workspace.toml` is the synced configuration. `b4-maintainer sync` syncs once by
  hand; the app syncs at startup, around `r`, and on quit.
- `b4-maintainer DIR` opens a fixture workspace: `workspace.json`, `mail/*.mbx`, `state.json`.
- `b4-maintainer project add ID --list LIST --ref LABEL:REF:AUTHORITY` registers the
  existing checkout at `projects/ID` in `workspace.toml`; b4 enrolls it with state in `var/`.
- Mail is parsed offline through pinned b4; keep real mail under ignored `.local/`.
- A hang: `kill -USR1 <pid>` writes every thread's stack to
  `/tmp/b4-maintainer-<pid>.stacks` without stopping the app.
