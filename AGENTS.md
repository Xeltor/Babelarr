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
  - `app.py` → Application orchestration only.
  - `cli.py` → CLI startup, environment validation, logging config, signal handling.
  - `config.py` → All environment/config parsing (`Config.from_env()`).
  - `jellyfin_api.py` → Jellyfin refresh client.
  - `libretranslate_api.py` → Thin HTTP wrapper around LibreTranslate; manages raw
    requests and thread-local sessions. Consumed by `translator.py`, which handles
    translation logic, retries, and backoff.
  - `mkv.py` → Subtitle stream helpers: list tracks via `ffprobe`, sample via `ffmpeg`,
    and provide heuristics for language hints and metrics.
  - `mkv_scan.py` → MKV-first translation pipeline that extracts streams, detects
    languages, and feeds missing-language jobs into LibreTranslate, writing `.lang.srt`
    outputs next to each MKV.
  - `translator.py` → `Translator` protocol and `LibreTranslateClient` implementation (retries/backoff).
  - `watch.py` → Filesystem watcher that now monitors MKV directories only and triggers
    on stabilized MKV files.
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
- **Application (`app.py`)**: orchestrates modules and schedules scans; no direct HTTP/DB work.
- **Watch (`watch.py`)**: filesystem monitoring and debouncing only.
  - **Watch (`watch.py`)**: MKV-focused filesystem monitoring; SRT watching is gone.
- **Jellyfin (`jellyfin_api.py`)**: minimal HTTP client to refresh Jellyfin.
- **Configuration (`config.py`)**: all env var parsing/validation and defaults. No other module should read `os.environ` directly.
-- **Translator (`translator.py`)**: translation logic and HTTP calls to LibreTranslate with retries/backoff and optional download flow. No filesystem scanning here.

When adding features, respect these boundaries. If cross-cutting concerns appear, introduce a small, focused interface rather than leaking responsibilities across modules.

---

## Logging & error handling

- Use `logging.getLogger("babelarr")` or `logging.getLogger(__name__)`; never use bare `print()` for runtime logs.
- Fail fast on unrecoverable errors; raise typed exceptions in library code. Handle at the edges (CLI/app) with clear messages.
- Network interactions must use timeouts and retries with exponential backoff where appropriate (see `LibreTranslateClient`).

## Commenting

Use annotations to make interfaces explicit and keep runtime behavior unsurprising.

- Annotate public functions, methods, and class attributes; prefer built-in generics (`list[str]`, `dict[str, int]`) over `typing.List`.
- Keep optionality clear (`str | None`), and avoid `Any` unless interop forces it—document why when it does.
- Favor `Protocol`/`TypedDict` for structured contracts instead of loose `dict`/`tuple` shapes.
- Import from `typing`/`collections.abc` as needed; keep type-only imports guarded with `TYPE_CHECKING` to avoid runtime weight.
- Let readable names replace redundant annotations; keep docstrings for public surfaces to describe purpose and behavior.

### Logging conventions

- Rely on the logger's name (`%(name)s`) for module context; do **not** repeat it in the message.
- Begin messages with a lowercase action verb.
- Express context as `key=value` pairs.

---

## Testing rules

- All new/changed behavior must have unit tests under `tests/`, mirroring module names (`test_<module>.py`).
- Keep unit tests fast and hermetic (no network or real filesystem where avoidable).
- Mark slower or external tests as integration: `@pytest.mark.integration`. Unit suite must pass by default without special env.
-- Example: `watch.py` tests use `pytest.mark.integration`.
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
- [ ] Comments and docstrings follow `COMMENTING_GUIDELINES.md`.
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

---

## MKV-first translation plan

- Scope: treat `.mkv` containers as the primary source of subtitles and translate missing target languages directly from the streams. The old SRT-watcher/queue path has been retired.
- Detection: reuse LibreTranslate's `/detect` API (via `MkvSubtitleTagger.detect_stream_language`) plus title heuristics to normalize ISO-639-2 codes for each stream and rank them by character/cue counts.
- Extraction: use `ffmpeg` (`MkvSubtitleExtractor.extract_stream`) to export the chosen stream to a temporary SRT file before feeding it to LibreTranslate.
- Translation: for every configured target language that lacks a stable `.lang.srt` output (or whose output is older than the MKV), pick the best available source language that LibreTranslate supports for that target, translate the temporary file, and atomically write the result beside the MKV.
- Scheduling: `Application` now schedules periodic MKV scans, handles watcher notifications via `watch.py`, and offloads per-MKV work to `MkvScanner`, which can run in a thread pool and respects the consolidated MKV cache manager (`cache.db`).
- Constraints: LibreTranslate treats `bs` as target-only, so translation jobs should never send `bs` as the source language. New builds should verify `is_target_supported()` before scheduling translations.

If uncertain, prefer smaller, incremental PRs over broad changes.
