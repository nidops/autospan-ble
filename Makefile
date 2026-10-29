ifeq ($(OS),Windows_NT)
PYTHON ?= python
else
PYTHON ?= python3
endif

.DEFAULT_GOAL := help
.PHONY: help format lint pre-commit pre-commit-install

help:
	@echo "AutoSpan BLE development (install .[dev] in your virtual environment first)"
	@echo "  make format               Format Python code with Ruff"
	@echo "  make lint                 Check Python code style and syntax"
	@echo "  make pre-commit           Run all pre-commit hooks on tracked files"
	@echo "  make pre-commit-install   Install the Git pre-commit hook"
	@echo "  PYTHON=path/to/python     Override the active Python interpreter"

format:
	@$(PYTHON) -m ruff format src tests scripts

lint:
	@$(PYTHON) -m ruff check src tests scripts
	@$(PYTHON) -m ruff format --check src tests scripts
	@$(PYTHON) -m compileall -q src tests scripts

pre-commit:
	@$(PYTHON) -m pre_commit run --all-files

pre-commit-install:
	@$(PYTHON) -m pre_commit install
