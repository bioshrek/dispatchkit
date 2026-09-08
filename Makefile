.PHONY: check test lint types imports doctor validate resolve tick

# The whole gate, in the order that fails fastest.
check: test lint types imports

test:
	uv run pytest -q

lint:
	uv run ruff check .

types:
	uv run mypy

imports:
	uv run lint-imports

# Can this repository run a pass at all? Offline without REPO/PROJECT.
doctor:
	uv run dispatchkit doctor $(if $(REPO),--repo $(REPO) --project $(PROJECT),)

# Check a task graph before it is reviewed or applied. Exit 0 valid, 1 invalid,
# 2 unreadable.
validate:
	uv run dispatchkit validate $(GRAPH)

# Derive every task's status from a recorded snapshot. Read-only.
resolve:
	uv run dispatchkit resolve --state $(STATE) --plan $(PLAN)

# One scheduler pass, dry run against a recorded snapshot.
tick:
	uv run dispatchkit tick --plan $(PLAN) --state $(STATE)
