
VENV ?= .venv

.PHONY: setup lint test check

setup:
	@if [ ! -d "$(VENV)" ]; then python -m venv $(VENV); fi
	$(VENV)/bin/pip install -U pip
	@if [ -f requirements.txt ]; then $(VENV)/bin/pip install -r requirements.txt; fi
	@if [ -f requirements-dev.txt ]; then $(VENV)/bin/pip install -r requirements-dev.txt; fi
	$(VENV)/bin/pip install -e .
	@which $(VENV)/bin/pre-commit >/dev/null 2>&1 || $(VENV)/bin/pip install pre-commit
	$(VENV)/bin/pre-commit install --hook-type pre-commit --hook-type pre-push && echo "pre-commit hooks installed"

lint:
	$(VENV)/bin/pre-commit run --all-files

test:
	$(VENV)/bin/pytest --maxfail=1 --disable-warnings -q

check: setup
	$(VENV)/bin/pre-commit run --all-files
	$(VENV)/bin/pytest --maxfail=1 --disable-warnings -q
