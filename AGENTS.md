# AGENTS.md

## Pull request rules (for Codex and humans)

Before creating or updating any PR:
1) Install deps and hooks: `make setup`
2) Run all linters/formatters: `make lint`
3) Run tests: `make test`

Only open/update the PR if **all** steps succeed. Otherwise, fix and re-run `make check`.

---

## Repository structure

- `main.py`
  - Thin entrypoint that calls `babelarr.cli.main()`. Do not add logic here.
- `babelarr/`
  - `app.py` → Application orchestration, filesystem watcher, worker pool, queueing.
  - `cli.py` → CLI startup, environment validation, logging config, signal handling.
  - `config.py` → All environment/config parsing (`Config.from_env()`).
  - `libretranslate_api.py` → Thin HTTP wrapper around LibreTranslate; manages raw
    requests and thread-local sessions. Consumed by `translator.py`, which handles
    translation logic, retries, and backoff.
  - `queue_db.py` → SQLite queue repository; thread-safe access to queued paths.
  - `translator.py` → `Translator` protocol and `LibreTranslateClient` implementation (retries/backoff).
  - `__init__.py` → Public exports.
- `tests/` → Pytest suite (unit tests by default; integration tests explicitly marked).
- `Dockerfile` → Runtime container definition.
- `Makefile` → Unified dev commands (`setup`, `lint`, `test`, `check`).
- `pyproject.toml` → Tool configs and/or dependencies.
- `pytest.ini` → Pytest defaults.
- `requirements-dev.txt` → Dev/test dependencies.

---

## Coding boundaries

- **CLI (`cli.py`)**: only config parsing, env validation (including service reachability), logging setup, signal handling, and app bootstrap. No business logic here.
- **Application (`app.py`)**: high-level orchestration: watchers, debouncing, queueing, worker threads, scheduling full scans. No direct HTTP/DB logic beyond using collaborators.
- **Configuration (`config.py`)**: all env var parsing/validation and defaults. No other module should read `os.environ` directly.
- **Queue (`queue_db.py`)**: SQLite queue persistence, thread-safe CRUD.
- **Translator (`translator.py`)**: translation logic and HTTP calls to LibreTranslate with retries/backoff and optional download flow. No filesystem scanning or queue management here.

When adding features, respect these boundaries. If cross-cutting concerns appear, introduce a small, focused interface rather than leaking responsibilities across modules.

---

## Logging & error handling

- Use `logging.getLogger("babelarr")` or `logging.getLogger(__name__)`; never use bare `print()` for runtime logs.
- Fail fast on unrecoverable errors; raise typed exceptions in library code. Handle at the edges (CLI/app) with clear messages.
- Network interactions must use timeouts and retries with exponential backoff where appropriate (see `LibreTranslateClient`).

### Logging conventions

- Prefix messages with the module name (e.g., `app`, `cli`, `translator`).
- Begin messages with a lowercase action verb.
- Express context as `key=value` pairs.

---

## Testing rules

- All new/changed behavior must have unit tests under `tests/`, mirroring module names (`test_<module>.py`).
- Keep unit tests fast and hermetic (no network or real filesystem where avoidable).
- Mark slower or external tests as integration: `@pytest.mark.integration`. Unit suite must pass by default without special env.
- When fixing a bug, first add a failing test that reproduces it; then implement the fix.
- Target coverage for new/changed code: **≥85%** (enforce in CI if/when coverage tooling is added).

Common commands:
```bash
make test          # pytest -q
pytest -q          # direct
pytest -q -k name  # filter by test name
```

---

## Dependency policy

- Prefer stdlib; keep the dependency set minimal.
- Runtime deps belong in `pyproject.toml` (or `requirements.txt` if used).
- Dev/test deps belong in `requirements-dev.txt`.
- Do **not** hardcode service URLs or secrets; surface via env and load through `Config`.
- When adding a new dep, justify in the PR description and add tests that exercise it.

---

## CI/CD rules

- CI must run both `pre-commit` (format/lint/type checks) and `pytest` before merge.
- Do **not** modify CI workflows unless the task is explicitly about CI.
- Do **not** disable failing checks or reduce thresholds to pass CI.

Branch protection should require CI checks for PRs into protected branches.

---

## PR checklist

Codex (and humans) must ensure:

- [ ] `make check` passes locally (setup → lint → tests).
- [ ] New/changed behavior is covered by tests.
- [ ] No debug prints; sensible logging only.
- [ ] Log messages follow the logging conventions.
- [ ] Public CLI behavior changes are documented in `README.md`.
- [ ] No direct env reads or hidden globals; configuration goes through `Config`.
- [ ] No secrets or credentials are committed.
- [ ] Functions/classes have clear docstrings and types where helpful.

### Commit message format

Use [Conventional Commits](https://www.conventionalcommits.org/) with a brief, imperative summary.

Examples:

- feat: add translation caching
- fix: handle missing API key

---

## What not to change (without explicit instruction)

- Don’t change `main.py` — it should only call `babelarr.cli.main()`.
- Don’t rename or remove existing top-level modules in `babelarr/` without a clear, documented reason.
- Don’t move logic across boundaries (see **Coding boundaries**) unless part of a deliberate refactor.
- Don’t reduce test coverage thresholds or disable failing tests to “get CI green.”
- Don’t bypass or disable `pre-commit` hooks.
- Don’t remove retries/backoff in the translator client.

**Allowed exceptions (deliberate refactors):**
If creating a new module or splitting an existing one improves separation of concerns or maintainability, you may do so **provided that**:
1. Existing modules’ responsibilities remain clear and slimmed, not duplicated.
2. Tests are added/updated to cover the new module and any moved code.
3. Imports and `babelarr/__init__.py` (public API) are updated to maintain stability.
4. Documentation and `README.md` are updated if user-facing behavior changes.
5. The refactor is incremental and avoids bundling unrelated changes.

---

## Commands summary

- Install deps & hooks:
```bash
make setup
```
- Run linters/formatters:
```bash
make lint
```
- Run tests:
```bash
make test
```
- Full gate (must pass before PR):
```bash
make check
```

---

## Task guidance for Codex

When implementing changes:
1. Read the target module and its tests first; keep changes minimal and focused.
2. Respect module boundaries; if boundaries are unclear, propose a small interface or a targeted refactor.
3. If introducing a new module, follow **Allowed exceptions** steps above.
4. Write/adjust tests **before** opening the PR.
5. Run `make check`; fix all issues locally.
6. Only then open/update the PR.

If uncertain, prefer smaller, incremental PRs over broad changes.
