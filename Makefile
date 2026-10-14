# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0

.PHONY: install test lint fmt type build demo clean

UV ?= uv
NPM ?= npm

install:
	$(UV) sync --extra dev
	cd frontend && $(NPM) ci

test: build
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	cd frontend && $(NPM) run typecheck

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

type:
	$(UV) run mypy

build:
	@test -d frontend/node_modules || (cd frontend && $(NPM) ci)
	cd frontend && $(NPM) run build

demo: build
	$(UV) run python scripts/demo.py

clean:
	rm -rf out demo src/senseai_viewer/static .coverage .pytest_cache .mypy_cache .ruff_cache
