# Maintenance repositories

User-managed Git checkouts live under projects/<project-id>/.
Checkout contents are ignored by the client repository.

- Add projects explicitly by cloning or registering a checkout.
- Initial scope: mailing-list patch workflows supported by b4.
- Configure sources, b4 identity/tracking, refs, prompts, and test profiles per project.
- b4 review/apply trees belong to the assigned project's repository.
- Background jobs use isolated worktrees; preserve the maintenance checkout.
- Project removal preserves repositories and review data.

See [project requirements](../docs/design/projects.md).
