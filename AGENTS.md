# AGENTS.md
## Pull request rules (for Codex and humans)

Before creating or updating any PR:
1) Install deps and hooks: `make setup`
2) Run all linters/formatters: `make lint`
3) Run tests: `make test`

Only open/update the PR if **all** steps succeed. Otherwise, fix and re-run `make check`.
