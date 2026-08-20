# Deployment helpers. Requires mpremote (pip install mpremote).
# Close any other serial connection (e.g. Thonny) before deploying.
PORT ?= auto

.PHONY: deploy console reset test test-mpy lint

deploy:
	mpremote connect $(PORT) cp main.py :main.py + cp -r app :

console:
	mpremote connect $(PORT) repl

reset:
	mpremote connect $(PORT) reset

test:
	python3 -m unittest discover tests
	@command -v micropython >/dev/null && $(MAKE) --no-print-directory test-mpy || \
		echo "micropython not installed — skipped MicroPython tests (brew install micropython)"

# Runs the app modules under the real MicroPython interpreter (unix port)
# with stubbed hardware. .frozen keeps the built-in asyncio importable.
test-mpy:
	MICROPYPATH="tests/mpy_stubs:.:.frozen" micropython tests/mpy/run_tests.py

lint:
	ruff check .
