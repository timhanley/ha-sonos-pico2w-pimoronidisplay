# Two-tier sleep.
#
# Tier 1 — screen sleep (SCREEN_SLEEP_TIMEOUT): display off, LED fast-blinks,
# WiFi stays up and the state poll keeps running → any button wakes instantly
# with fresh data.
#
# Tier 2 — deep sleep (DEEP_SLEEP_TIMEOUT): CYW43 chip deactivated and CPU
# dropped to 48 MHz. Powering the chip down prevents the SDIO transport stall
# ("F2 not ready" / STALL) seen after long idle periods; wake is a full,
# clean reconnect (~3-5 s) with the cached state shown immediately.
#
# The deep-sleep loop deliberately blocks asyncio with time.sleep_ms —
# machine.lightsleep pauses the PWM timer driving the RGB LED, causing
# erratic flicker, so it is not used.
import time

import machine

from app import hw, log

_SLEEP_CPU_HZ = 48_000_000
_WAKE_POLL_MS = 50


class PowerManager:
    def __init__(self, app):
        self.app = app
        self.screen_sleeping = False
        self.deep_sleeping = False

    # LED cadences distinguish the tiers: 1 s blink = screen sleep,
    # 2 s blink = deep sleep.
    @staticmethod
    def led_pulse_screen():
        if (time.ticks_ms() // 1000) % 2 == 0:
            hw.led_sleep_pulse()
        else:
            hw.led_off()

    @staticmethod
    def led_pulse_deep():
        if (time.ticks_ms() // 2000) % 2 == 0:
            hw.led_sleep_pulse()
        else:
            hw.led_off()

    def enter_screen_sleep(self):
        log.debug("screen sleep")
        self.screen_sleeping = True
        hw.display.set_backlight(0)
        hw.led_off()

    def wake_screen_sleep(self):
        """Instant wake: discard the wake press, light up, redraw cached state."""
        log.debug("screen wake")
        self.screen_sleeping = False
        self.app.buttons.clear()
        self.app.buttons.touch()
        hw.display.set_backlight(self.app.brightness)
        hw.led_active()
        self.app.redraw_current()

    def enter_deep_sleep(self):
        log.info("deep sleep: WiFi off")
        self.screen_sleeping = False
        self.deep_sleeping = True
        # INVARIANT: deactivate the chip now so wake is a clean restart —
        # never trust isconnected() after a long idle.
        self.app.wifi.deactivate()
        # Cached art may be stale by wake time — force a fresh download.
        self.app.art.reset()
        hw.display.set_backlight(0)
        hw.led_off()

    def deep_sleep_blocking(self):
        """Deep-sleep loop: blocks the scheduler until a button wakes us,
        then restores the CPU clock, reconnects WiFi and restarts Core 1."""
        buttons = self.app.buttons
        buttons.stop()  # Core 1 must not poll while we own the pins

        original_freq = machine.freq()
        machine.freq(_SLEEP_CPU_HZ)

        poll_count = 0
        while True:
            time.sleep_ms(_WAKE_POLL_MS)
            poll_count += 1
            if poll_count % 4 == 0:  # 200 ms LED cadence
                self.led_pulse_deep()
            if buttons.any_pin_pressed():
                break

        machine.freq(original_freq)

        # Light up immediately — visual feedback before the slow reconnect.
        hw.display.set_backlight(self.app.brightness)
        hw.led_active()

        # Wait for full release before Core 1 restarts, or it would catch the
        # release edge and fire an action for the wake press.
        while buttons.any_pin_pressed():
            time.sleep_ms(5)
        time.sleep_ms(50)  # debounce

        # Clean reconnect from a powered-down chip. First attempt can fail if
        # the AP responds slowly after a long chip-off period; retry once.
        if not self.app.wifi.connect_blocking():
            time.sleep_ms(1000)
            self.app.wifi.connect_blocking()
        # Settle so lwIP has ARP/routing ready before the first HA poll —
        # otherwise it fails with ECONNABORTED.
        time.sleep_ms(500)

        buttons.start()
        self.deep_sleeping = False
        log.info("deep sleep: woke after button press")
