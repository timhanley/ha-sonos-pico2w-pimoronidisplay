# Deployment helpers. Requires mpremote (pip install mpremote).
# Close any other serial connection (e.g. Thonny) before deploying.
PORT ?= auto

.PHONY: deploy console reset test lint

deploy:
	mpremote connect $(PORT) cp main.py :main.py + cp -r app :

console:
	mpremote connect $(PORT) repl

reset:
	mpremote connect $(PORT) reset

test:
	python3 -m unittest discover tests

lint:
	ruff check .
