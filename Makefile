# The same checks CI runs, for running before you push.  `make check` runs them all.
PYTHON ?= python3
BASE ?= origin/main

.PHONY: help install lint format test cov docs docs-check links names version commits build check clean

help:  ## list the targets
	@grep -E "^[a-z-]+:.*## " $(MAKEFILE_LIST) | sed -E "s/:.*## /\t/"

install:  ## editable install with the development tools
	$(PYTHON) -m pip install -e ".[dev]"

lint:  ## ruff check and ruff format --check
	ruff check initialize_claude_ai tests tools
	ruff format --check initialize_claude_ai tests tools

format:  ## apply ruff format and the safe ruff fixes
	ruff format initialize_claude_ai tests tools
	ruff check --fix initialize_claude_ai tests tools

test:  ## the offline test suite, in parallel
	$(PYTHON) -m pytest -n auto

cov:  ## the suite with coverage, failing under the floor in pyproject.toml
	$(PYTHON) -m pytest -n auto --cov --cov-report=term

docs:  ## regenerate docs/cli-reference.md and docs/config-reference.md
	$(PYTHON) tools/gen_docs.py

docs-check:  ## the generated references are current
	$(PYTHON) tools/gen_docs.py --check

links:  ## every relative Markdown link resolves
	$(PYTHON) tools/check_links.py

names:  ## no internal host names or personal paths
	$(PYTHON) tools/check_names.py

version:  ## one version everywhere
	$(PYTHON) tools/check_version.py

commits:  ## the branch is one commit with a title and a body (BASE=origin/main)
	$(PYTHON) tools/check_commits.py $(BASE) HEAD

build:  ## sdist and wheel in dist/, metadata checked
	rm -rf dist
	$(PYTHON) -m build
	$(PYTHON) -m twine check --strict dist/*

check: lint test docs-check links names version  ## everything CI checks except the commit gate

clean:  ## remove build and test leftovers
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .coverage .coverage.* coverage.xml htmlcov
