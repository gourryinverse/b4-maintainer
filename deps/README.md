# Dependencies

| Submodule | Role | Upstream ([conf](upstreams.conf)) |
| --- | --- | --- |
| deps/b4 | Mail, series, tracking, review, reply, apply/take | https://git.kernel.org/pub/scm/utils/b4/b4.git |
| deps/liblore | Archive access | https://git.kernel.org/pub/scm/utils/liblore/liblore.git |
| deps/review-prompts | Analysis templates and subsystem context | https://github.com/masoncl/review-prompts.git |
| deps/kernel-style | Planning, coding, review, changelog guidance | https://github.com/rikvanriel/kernel-style.git |
| deps/virtme-ng | Kernel build/VM/test runner | https://github.com/arighi/virtme-ng.git |
| deps/kres | Kernel analysis workflows, findings, validation, export | https://github.com/masoncl/kres.git |
| deps/semcode | Semantic code search, call graphs, commit history, MCP queries | https://github.com/facebookexperimental/semcode.git |
| deps/sashiko | AI review service: API schema reference, local review | https://github.com/sashiko-dev/sashiko.git |

Default source: maintainer forks under https://github.com/gourryinverse/.

Initialize from the project root:

    git submodule update --init --recursive

- Initialization retrieves source; installation and execution are separate.
- Recursive initialization includes b4's liblore, patatt, and ezgb submodules.
- Sashiko's `third_party/linux` reference kernel is `update = none`; recursive
  initialization skips it.
- Runtime packaging uses one compatible liblore version.
- Configure Semcode's repository, revision, and index per project/session.
- Semcode builds, indexing, model downloads, and MCP launch are explicit actions.
- Sashiko builds and `sashiko review` runs (LLM, cost) are explicit actions.
- Upgrade pins explicitly and validate adapter compatibility.
- Configure project workspaces, LLM providers, and credentials externally.
- Maintain target repositories under projects/.
- Register backlog repositories through the backlog feature.

## Development

- Address gaps in the owning project; use maintainer fork branches.
- Track upstream and submit reusable fixes, or maintain a private patch set.
- Validate dependency and integration tests before updating the parent gitlink.
- Ensure pinned commits are reachable from the configured source.
- Keep HTTPS URLs in .gitmodules; configure push access locally.
- Remotes per submodule: origin is the fork; upstream is the original project.

Configure remotes after initialization:

    git submodule foreach '
      n=$(basename "$sm_path")
      up=$(git -C "$toplevel" config -f deps/upstreams.conf --get "upstream.$n.url")
      [ "$(git remote get-url origin)" = "$up" ] && git remote rename origin upstream
      git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/gourryinverse/$n.git"
      git remote set-url origin "https://github.com/gourryinverse/$n.git"
      git remote set-url --push origin "git@github.com:gourryinverse/$n.git"
      git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$up"
    '

## Known gaps

- Carried on the fork's master, pending upstream: `build_thread_tree()` keeps
  In-Reply-To cycles; `LiteThreadScreen` accepts a mailbox; per-repository
  `b4.data-dir`/`b4.cache-dir`; reply flow (`review_tui._reply`: quote, preview, send)
  usable outside `LiteThreadScreen`; public Sashiko fetch/parse helpers; `get_editor_cmd()`
  and `unix_newlines()` factored out of `edit_in_editor()`.
- Carried on the sashiko fork: `third_party/linux` is `update = none` so recursive
  initialization does not clone the kernel. Drop it if upstream adopts it.
- b4 `LoreMailbox.get_series()` hangs on a two-message reply cycle carrying trailers.
- b4 `get_series()` runs `git patch-id` once per patch; parsing a list window is slow.
- liblore has no linear-time thread splitter; the client keys threads by References root.
  Remove the client grouping when liblore provides one.
