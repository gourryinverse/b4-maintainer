# Dependencies

| Submodule | Role |
| --- | --- |
| deps/b4 | Mail, series, tracking, review, reply, apply/take |
| deps/liblore | Archive access |
| deps/review-prompts | Analysis templates and subsystem context |
| deps/kernel-style | Planning, coding, review, changelog guidance |
| deps/virtme-ng | Kernel build/VM/test runner |
| deps/kres | Kernel analysis workflows, findings, validation, export |
| deps/semcode | Semantic code search, call graphs, commit history, MCP queries |

Initialize from the project root:

    git submodule update --init --recursive

- Initialization retrieves source; installation and execution are separate.
- Recursive initialization includes b4's liblore, patatt, and ezgb submodules.
- Runtime packaging uses one compatible liblore version.
- Configure Semcode's repository, revision, and index per project/session.
- Semcode builds, indexing, model downloads, and MCP launch are explicit actions.
- Upgrade pins explicitly and validate adapter compatibility.
- Configure project workspaces, LLM providers, and credentials externally.
- Maintain target repositories under projects/; see [project requirements](../docs/design/projects.md).
- Register backlog repositories through the [backlog feature](../docs/design/backlogs.md).

## Development

- Address gaps in the owning project; use maintainer fork branches.
- Track upstream and submit reusable fixes, or maintain a private patch set.
- Validate dependency and integration tests before updating the parent gitlink.
- Ensure pinned commits are reachable from the configured source.
- Configure fork remotes/access explicitly; retain portable source URLs.
- See [dependency-first requirements](../docs/design/architecture.md).
