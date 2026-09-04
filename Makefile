.PHONY: help install install-greenlight test lint format reproduce diagram clean

help:
	@echo "install             core plus dev dependencies"
	@echo "install-greenlight  the separate AGPL worker environment"
	@echo "test                run the test suite"
	@echo "lint                ruff"
	@echo "reproduce           regenerate every result from a clean state"
	@echo "diagram             re-render docs/architecture.{svg,png} from the .mmd"

install:
	pip install -e ".[dev]"

# Deliberately a separate environment: gl-gym is AGPL-3.0 and pins numpy<2,
# while power-grid-model requires numpy>=2. See docs/DECISIONS.md ADR-0001/0002.
install-greenlight:
	python3 -m venv .venv-greenlight
	./.venv-greenlight/bin/pip install -r workers/greenlight/requirements.txt
	@echo "Now: export KASFLEX_GREENLIGHT_PYTHON=$$PWD/.venv-greenlight/bin/python"

test:
	pytest -q

lint:
	ruff check src/ tests/ workers/

reproduce: clean
	kasflex experiment --config configs/scenario_westland_winter.yaml --days 3 \
		--output results/runs.jsonl

diagram:
	npx -y @mermaid-js/mermaid-cli -i docs/architecture.mmd -o docs/architecture.svg -b transparent
	npx -y @mermaid-js/mermaid-cli -i docs/architecture.mmd -o docs/architecture.png -b white -w 1500

clean:
	rm -rf results/ .pytest_cache/ .ruff_cache/ .coverage
