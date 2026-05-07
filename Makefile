.PHONY: install sync test lint codegen clean

# iCloud re-hides files prefixed with underscore in site-packages; Python's
# site.py then skips the editable-install .pth file. Bypass entirely by
# setting PYTHONPATH=src for every Python invocation.
PY = PYTHONPATH=src .venv/bin/python

install:
	uv sync --extra dev

sync: install

test:
	$(PY) -m pytest

lint:
	.venv/bin/ruff check .

codegen:
	$(PY) scripts/codegen.py --version 26.4

clean:
	rm -rf .venv build dist *.egg-info
	find . -name __pycache__ -prune -exec rm -rf {} +
