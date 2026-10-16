.PHONY: install run test

install:
	./install.sh

run:
	./start.sh

test:
	. .venv/bin/activate && python -m pytest -q || true
