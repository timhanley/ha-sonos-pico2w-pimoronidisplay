# Deployment helpers. Requires mpremote (pip install mpremote).
# Close any other serial connection (e.g. Thonny) before deploying.
PORT ?= auto
# RP2350 is Cortex-M33 — needed so mpy-cross AOT-compiles the viper module.
MARCH ?= armv7emsp

.PHONY: deploy deploy-mpy console reset test test-mpy test-live lint clean

deploy:
	mpremote connect $(PORT) cp main.py :main.py + cp -r app :

# Precompiled deploy: faster boot, less heap fragmentation (the device skips
# compiling ~90KB of source). Requires mpy-cross MATCHING the device firmware
# (pip install mpy-cross==<firmware MicroPython version>.*). Don't mix with
# `make deploy` — this removes :app first so no stale .py files shadow .mpy.
deploy-mpy:
	rm -rf build && mkdir -p build/app
	for f in app/*.py; do \
		if [ "$$f" = "app/pngfilters_viper.py" ]; then \
			mpy-cross -march=$(MARCH) -o build/$${f%.py}.mpy $$f || exit 1; \
		else \
			mpy-cross -o build/$${f%.py}.mpy $$f || exit 1; \
		fi \
	done
	-mpremote connect $(PORT) rm -r :app
	mpremote connect $(PORT) cp main.py :main.py + cp -r build/app :

clean:
	rm -rf build

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

# Read-only check of the network layer against the REAL Home Assistant,
# using the real config.py. Run from the home LAN before first flash.
test-live:
	MICROPYPATH=".:.frozen" micropython tests/integration/live_ha_check.py

lint:
	ruff check .
