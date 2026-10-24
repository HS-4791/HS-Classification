.PHONY: setup fetch validate validate-pdf reconcile test lint check clean

VENV := .venv/Scripts
REV  ?= 2026HTSRev21

setup:
	python -m venv .venv
	$(VENV)/python -m pip install -U pip
	$(VENV)/python -m pip install -e ".[dev]"

fetch:
	$(VENV)/python scripts/fetch_schedule.py --revision $(REV) --verify

validate:
	$(VENV)/hsclass schedule validate --source export

validate-pdf:
	$(VENV)/hsclass schedule validate --source pdf

reconcile:
	$(VENV)/hsclass schedule reconcile --chapter 61

test:
	$(VENV)/python -m pytest -q

lint:
	$(VENV)/ruff check src tests scripts scripts

check: lint test validate reconcile

clean:
	rm -rf .pytest_cache .ruff_cache reports/*.json reports/*.md
