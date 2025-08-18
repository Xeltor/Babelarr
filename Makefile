.PHONY: setup lint test check

setup:
	python -m pip install -U pip
	@if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
	@if [ -f requirements-dev.txt ]; then pip install -r requirements-dev.txt; fi
	pip install -e .
	@which pre-commit >/dev/null 2>&1 || pip install pre-commit
	pre-commit install --hook-type pre-commit --hook-type pre-push && echo "pre-commit hooks installed"

lint:
	pre-commit run --all-files

test:
	pytest --maxfail=1 --disable-warnings -q

check: setup lint test
