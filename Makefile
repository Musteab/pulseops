.DEFAULT_GOAL := help
PY := .venv/bin/python
PIP := .venv/bin/pip

EVENTS ?= 5000
SEED ?= 42
FAULT_RATE ?= 0.05

.PHONY: help setup demo generate validate test lint clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv and install the package
	python3 -m venv .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[dev]"
	@echo "ready. try: make demo"

demo: generate validate ## Generate a faulty dataset and score the contract layer

generate: ## Write synthetic events plus the fault ground-truth manifest
	$(PY) -m pulseops generate --events $(EVENTS) --seed $(SEED) --fault-rate $(FAULT_RATE)

validate: ## Apply the contract, quarantine failures, report detection rate
	$(PY) -m pulseops validate --quarantine data/raw/quarantine.jsonl

test: ## Run the test suite
	$(PY) -m pytest

lint: ## Check formatting and lint rules
	.venv/bin/ruff check src tests

clean: ## Remove generated data and caches
	rm -rf data/raw data/seeds .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
