# b4-maintainer

Mailing-list maintainer workspace: patch review, bug triage, interactive agents, project tests.

Stage: fixture-backed interface prototype (browse screen).

Python, Textual, GPL-2.0-or-later.

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

## Contributing

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
    ./setup.sh        # first run creates setup.conf: set STATE_REPO, run again (if no state/)
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
    .venv/bin/python -m pytest -n auto        # in parallel; timing tests skipped
    .venv/bin/python -m pytest -m serial      # the timing tests, alone
    ./b4-maintainer --demo .local/demo

- Tabs across the top: `F2` LKML (the series list and threads), `F3` Workbench, `F4`
  Artifacts; each keeps its place when you switch away and back. The footer shows each
  tab's common keys; `?` lists all of them.
- `b4-maintainer` in a workspace with `workspace.toml` (or `state/workspace.toml`) opens it
  live: `F5` fetches profile lists and your own mail from lore (in a thread, the thread reloads
  in place), `t` tracks the selected series
  through b4 (To Review), `x` hides it, `M` marks it read, `/` filters by regex, `r` drafts a
  reply beside the thread (in the list: to the cover letter), `R` does too after picking
  session output to add at its end (any session's: the series' own first, then its
  workbenches', then the rest; `/` filters) (their commits as inline
  patches for `git am --scissors`, their results and reproducers as text), and `ctrl+s`
  sends it through b4
  (`--email-dry-run` to test); Responses lists unread replies to your mail (b4's Seen flag).
- `a` in the series list or a thread applies that series to a maintainer base or a b4 prep
  branch of any project (for a series routed to the wrong project, or none): pick the
  project and branch (when there are several), then the patches (all ticked to start). b4 applies them as `b4 am` would: authors kept, trailers given on the list
  (and, online, on other postings of the same patches) and a Link added (`b4.linkmask`),
  you as committer, no sign-off added. The branch must not be checked out; it moves only
  if every patch applies. Your FETCH_HEAD is left as it was; b4 records the series for
  `b4 ty`. When the patches conflict you choose: hand them to an agent (an apply session
  applies b4's mbox on a copy of the branch and resolves the conflicts; `A` on its tree in
  Artifacts (`F4`) accepts the result, only while the branch has not moved), or leave the branch as
  it is.
- Workbenches are recipes for agent sessions over several projects (e.g. a Linux tree and an
  ndctl tree, patched together): per project, a base and the series and local branches
  applied on it in order, one file each in `state/workbenches/` (synced). `w` in the
  series list or a thread adds the series to one (or a new one); `F3` lists them with a
  preview of what a launch prepares: `n` new, `r` rename, `x` remove (a workbench or
  project after a confirmation), `b` a project's base, `B` add a local branch (the
  project's, or a session tree's), shift+up/down move an item, `v` takes a newer revision,
  and `F1` launches a session over it: lightweight, no trees, each series' thread in its
  context to read and compare (series that conflict are fine); or worktree, one tree
  per project, side by side in `var/worktrees/<id>/`, where the agent starts. The tab
  shows, left, the workbenches and the selected one's sessions; middle, its Contents in
  sections: Items (each project and its items, edited as above), Mailboxes (each
  series), Trees (each session tree and its commits by class), Artifacts (each
  session's prompt, reports, results, files); right, the selected row's text (for
  Items, the launch preview). On Trees, space marks commits, `p` plucks them (or the
  selected one) onto the project's base, a prep branch, or another session tree, `P` cuts
  a prep branch from them, `D` derives a session, and `x` on a tree removes it after
  saying what it would lose. Enter on a Mailboxes row opens that thread in the tab (its
  tree and the selected message; Escape closes it), where `r` replies to the selected
  message and `R` adds session output (with the tmux editor the draft opens beside the
  app and `ctrl+s` sends from the tab; otherwise the reply opens on LKML). Enter on a
  session (or on it in the drawer) puts it
  beside the app, `o` opens the workbench's live session from any pane, and one launched
  from the tab joins beside at once. The column follows the tab: back on LKML the
  workbench session returns to its own window.
- Every session is filed in a workbench: F1 on a series makes that series' workbench the
  first time (named after it, the series on its base; `origin` in its file) and files
  the session there, so its sessions and their output are found in the Workbench tab; a
  derived session goes with its parent. Sessions from before are listed under
  Unfiled sessions.
- Every session (lightweight too) writes its results under `out/` and records them in
  `out/artifacts.toml` (the b4m-artifacts skill): findings, reports, comparisons,
  reproducers, results, and, without a tree, proposed changes as `diff` files.
  A tree whose items do not apply offers to hand that tree to the agent (it starts at
  the base with every item as one mbox to `git am`), or to launch nothing.
- `d` drops that series from a maintainer base again: its commits there (found by their
  Link, else subject) are taken out and the later commits replayed onto what remains,
  after a confirmation; if one does not replay without them, nothing changes. Your own
  branches named in `refs` (e.g. a base with a view) are rescanned on every reload, so
  `a` and `d` show in their views at once; fetched refs wait for `F5`.
- Workspace state syncs through a private state repository cloned as `state/` (see
  [setup](#setup));
  `state/workspace.toml` is the synced configuration. `b4-maintainer sync` syncs once by
  hand; the app syncs at startup, around `F5`, and on quit.
- `var/workspace.toml` (optional, per machine, never synced) overrides it on this machine,
  e.g. `[sessions] max_builds` or a checkout's `path`: tables merge key by key,
  `[[projects]]`, `[[profiles]]`, and `[[models]]` entries by `id` (a new `id` is added);
  any other value it sets replaces the shared one. `project add` writes the shared file.
- `b4-maintainer DIR` opens a fixture workspace: `workspace.json`, `mail/*.mbx`, `state.json`.
- `b4-maintainer project add ID --list LIST --ref LABEL:REF:AUTHORITY` registers the
  existing checkout at `projects/ID` in `workspace.toml`; b4 enrolls it with state in `var/`.
- Threads leave out Sashiko replies that found nothing (its review of the patch has no
  findings, or its mail says it found no issues), unless someone answered them;
  `[sashiko] hide_clean = false` shows them.
- Series go to the project whose `lists` they were sent to. On lists projects share, a
  project's `subject_tags` (e.g. `["ndctl", "daxctl"]` for `[ndctl PATCH]`) claim the
  series whose subject prefix carries one; a project with tags takes only those. Only
  `[[profiles]]` lists are fetched by `F5`; a profile labelled like its project stands in
  for that project's own sidebar view.
- A ref's `authority` is `mainline`, `acceptance`, or `preliminary` (highest first); an
  unknown one is reported, and a reload with it keeps the current view.
- When b4 cannot apply the series for an F1 worktree session, you choose: hand it to the
  agent (the tree starts at that base without the series, with b4's mbox in its `.git`;
  the agent applies it with `git am -3`, resolves the conflicts, records how, then does
  the task), or launch nothing.
- F1's Command field puts its text (e.g. `/goal`) at the very start of the session's
  initial prompt, before the composed task.
- Guidance: agent sessions get the review-prompts kernel skill (review, bug analysis,
  debugging, inline replies) and kernel-style (code and changelog style) as pi skills,
  from the pinned `deps/` checkouts, picked by the F1 choices (adjustable under Advanced);
  `[[guidance]] id = "review-prompts", path = "~/git/review-prompts"` in `workspace.toml`
  uses another checkout. Each session records the commits it was given.
- Maintainer bases: a project's `bases = [{ branch = "BRANCH", upstream = "REMOTE/BRANCH" }]`
  in `workspace.toml` names integration branches on top of upstream branches.
  `b4-maintainer bases` shows how far behind each is and which of its commits have landed
  upstream; `--rebase BRANCH` replays the rest onto the upstream (never in a checkout; a
  conflict changes nothing). In the app, `F4` in a thread shows what its sessions
  produced (their initial prompts, commits, results, reproducers) in place of the content
  (`F4` or Escape returns), and elsewhere opens the Artifacts tab:
  each project's bases, worktrees, session trees (commits by class, artifacts), b4 prep
  and review branches, and other local branches with work not on any remote; `r` rebases
  a base; when that stops at a conflict, `a` launches a rebase session on a copy, and `A`
  on its tree (or an apply session's) moves the base to its result. Space marks commits and `p` plucks them onto
  a base or a prep branch: an agent's become yours with `Assisted-by:` (`[sessions]
  assisted_by`, default the session's model); others keep their authors. `n` makes a b4
  prep branch from the marked commits (name, fork point, cover drafted from the agents'
  summaries) in a sparse worktree under `var/branches/`; on it, `t` adds recipients,
  `c` checks, `e` edits the cover letter, `s` adds your Signed-off-by (you certify the
  DCO), `o` previews exactly what would be sent (`b4 send -o`), `u` collects trailers
  from the list, `d` compares with the last sent revision, and `S` runs `b4 send` in the
  terminal, where b4 shows its summary and asks before anything goes out
  (`--email-dry-run` and `--no-sign` pass through). All by running b4. `x` removes a prep
  branch (`b4 prep --cleanup`, which archives it), another local branch, or a session tree,
  after saying what goes; rebasing a base moves the prep branches cut from it. `p` onto
  another session's tree copies commits there (same branch names, authors kept, a note in
  that session's `context/inbox.md`): a fix back to the session that found the bug, or
  work onto the tree of a series' next revision. `D` on a session tree derives a new
  session from its output: its tree starts at the series base, the parent's tree, or a
  maintainer base (no series applied), its prompt package carries the parent's artifact,
  and the launch dialog follows.
- Mail is parsed offline through pinned b4; keep real mail under ignored `.local/`.
- A hang: `kill -USR1 <pid>` writes every thread's stack to
  `/tmp/b4-maintainer-<pid>.stacks` without stopping the app.
