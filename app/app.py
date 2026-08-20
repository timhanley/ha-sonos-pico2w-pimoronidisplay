# Application core: owns all mutable state and wires the modules together.
#
# Concurrency model:
#   Core 0  — asyncio: action loop (events, sleep tiers, GC), state poll task,
#             WiFi check task, ad-hoc album-art tasks.
#   Core 1  — button poll thread (app/buttons.py) feeding the event queue.
#
# INVARIANTS:
#   - Core 1 starts BEFORE the boot speaker-selection loop (buttons are dead
#     during selection otherwise).
#   - Only one HTTP connection at a time — the state poll skips while an art
#     download holds the CYW43's connection.
#   - GC is periodic and paused during art work: a collect costs ~40 ms and
#     would add seconds to a row-by-row PNG decode.
import gc
import json
import time

import asyncio

from app import hw, log, screens
from app.artwork import ArtPipeline, DECODING, DOWNLOADING
from app.buttons import Buttons, EV_X_TAP, EV_Y_TAP
from app.ha import HAClient
from app.net import WiFi
from app.power import PowerManager
from app.settings import (DEEP_SLEEP_TIMEOUT, DEFAULT_BRIGHTNESS,
                          MIN_BRIGHTNESS, POLL_INTERVAL, SCREEN_SLEEP_TIMEOUT)

_BRIGHTNESS_FILE = "brightness.json"
_BRIGHTNESS_STEP = 0.05
_WIFI_CHECK_S = 30
_GC_INTERVAL_MS = 5000
_LOOP_TICK_MS = 50          # action-loop cadence; button events bypass it
_POLL_FAILURE_LIMIT = 3     # consecutive poll failures before the error screen


class App:
    def __init__(self):
        self.wifi = WiFi(notify=screens.show_message)
        self.ha = HAClient()
        self.buttons = Buttons()
        self.power = PowerManager(self)
        self.art = ArtPipeline(self.ha.headers, self._playback_active)

        self.state = None            # compact state dict (see ha.STATE_KEYS)
        self.speakers = []
        self.current_speaker = None  # entity_id
        self.force_poll = False      # set after next/prev for an immediate poll
        self.brightness = DEFAULT_BRIGHTNESS

        self.playback_screen = screens.PlaybackScreen(self)
        self.menu_screen = screens.MenuScreen(self)
        self.speaker_screen = screens.SpeakerSelectScreen(self)
        self.brightness_screen = screens.BrightnessScreen(self)
        self.screen = self.playback_screen

        self.buttons.feedback_draw = self._feedback_draw
        self._poll_failures = 0

    # ---- wiring ------------------------------------------------------------

    def _playback_active(self):
        return (self.screen is self.playback_screen and
                not self.power.screen_sleeping and not self.power.deep_sleeping)

    def _feedback_draw(self):
        """Called from Core 1 on any press/release: repaint the label strip."""
        if self.power.screen_sleeping or self.power.deep_sleeping:
            return
        hw.display_lock.acquire()
        try:
            screens.draw_button_labels_locked(self.buttons, self.screen.labels())
            hw.display.update()
        finally:
            hw.display_lock.release()

    # ---- navigation --------------------------------------------------------

    def set_screen(self, screen):
        self.screen = screen
        if screen is self.playback_screen:
            screen.invalidate()
        screen.draw()

    async def open_menu(self):
        self.menu_screen.index = 0
        self.set_screen(self.menu_screen)

    async def to_playback(self):
        new_state = await self.ha.get_state()
        if new_state:
            self.state = new_state
        self.set_screen(self.playback_screen)

    def redraw_current(self):
        if self.screen is self.playback_screen:
            self.playback_screen.invalidate()
        self.screen.draw()

    # ---- HA actions --------------------------------------------------------

    async def media_command(self, service):
        if not self.current_speaker:
            return
        if not await self.ha.call_service(service, self.current_speaker):
            screens.show_message("HA Connection Error", scale=1)
            await asyncio.sleep(1)
            self.redraw_current()

    async def load_speakers(self):
        if not self.wifi.check():
            screens.show_loading("WiFi Not Connected")
            await asyncio.sleep(2)
            return False
        if not await self.ha.ping():
            screens.show_loading("Cannot Reach HA")
            await asyncio.sleep(2)
            return False
        gc.collect()
        speakers = await self.ha.discover_speakers()
        if not speakers:
            screens.show_loading("No Sonos Speakers Found")
            await asyncio.sleep(2)
            return False
        self.speakers = speakers
        log.info("Total Sonos speakers found: %d" % len(speakers))
        return True

    async def select_speaker(self, entity_id):
        self.current_speaker = entity_id
        self.ha.set_entity(entity_id)
        new_state = await self.ha.get_state()
        if new_state:
            self.state = new_state
        self.set_screen(self.playback_screen)

    # ---- brightness --------------------------------------------------------

    def load_brightness(self):
        try:
            with open(_BRIGHTNESS_FILE) as f:
                self.brightness = float(json.load(f).get("brightness",
                                                         DEFAULT_BRIGHTNESS))
        except (OSError, ValueError):
            self.brightness = DEFAULT_BRIGHTNESS
        return self.brightness

    def adjust_brightness(self, direction):
        value = self.brightness + direction * _BRIGHTNESS_STEP
        self.brightness = max(MIN_BRIGHTNESS, min(1.0, value))
        hw.display.set_backlight(self.brightness)
        try:
            with open(_BRIGHTNESS_FILE, "w") as f:
                json.dump({"brightness": self.brightness}, f)
        except OSError:
            log.error("Error saving brightness")

    # ---- background tasks --------------------------------------------------

    async def _state_poll_task(self):
        elapsed = 0.0
        while True:
            await asyncio.sleep(0.1)
            elapsed += 0.1
            if not self.force_poll and elapsed < POLL_INTERVAL:
                continue
            self.force_poll = False
            elapsed = 0.0
            if self.power.deep_sleeping:
                continue
            if self.screen is not self.playback_screen:
                # Full redraw when the playback screen next shows.
                self.playback_screen.invalidate()
                continue
            if self.power.screen_sleeping:
                # Keep state fresh (and WiFi active) but draw nothing.
                new_state = await self.ha.get_state()
                if new_state:
                    self.state = new_state
                self.playback_screen.invalidate()
                continue
            if self.art.state == DOWNLOADING:
                continue  # CYW43: one HTTP connection at a time
            new_state = await self.ha.get_state()
            if new_state:
                self._poll_failures = 0
                self.state = new_state
                self.playback_screen.update(new_state)
            else:
                self._poll_failures += 1
                if self._poll_failures >= _POLL_FAILURE_LIMIT:
                    self.wifi.check()  # classify the outage for the error screen
                    self.playback_screen.update(None)

    async def _wifi_check_task(self):
        while True:
            await asyncio.sleep(_WIFI_CHECK_S)
            if not self.power.deep_sleeping:
                await self.wifi.ensure_connected()

    # ---- main loop ---------------------------------------------------------

    async def _wait_events(self, ms):
        """Sleep up to ms, waking early the instant Core 1 queues an event."""
        try:
            await asyncio.wait_for(self.buttons.flag.wait(), ms / 1000)
        except asyncio.TimeoutError:
            pass

    async def _action_loop(self):
        last_x_repeat = last_y_repeat = time.ticks_ms()
        last_gc = time.ticks_ms()
        while True:
            idle_s = self.buttons.idle_ms() // 1000
            art_busy = self.art.state in (DOWNLOADING, DECODING)

            if self.power.deep_sleeping:
                self.power.deep_sleep_blocking()  # returns after wake+reconnect
                self.buttons.clear()              # discard the wake press
                self.buttons.touch()
                self.force_poll = True
                self.redraw_current()             # cached state — no network here
                continue

            if self.power.screen_sleeping:
                if idle_s >= DEEP_SLEEP_TIMEOUT and not art_busy:
                    self.power.enter_deep_sleep()
                    continue
                if self.buttons.get_events():
                    self.power.wake_screen_sleep()  # wake press is discarded
                    continue
                self.power.led_pulse_screen()
                await self._wait_events(_LOOP_TICK_MS)
                continue

            if not art_busy:
                if idle_s >= DEEP_SLEEP_TIMEOUT:
                    self.power.enter_deep_sleep()
                    continue
                if idle_s >= SCREEN_SLEEP_TIMEOUT:
                    self.power.enter_screen_sleep()
                    continue
                now_ms = time.ticks_ms()
                if time.ticks_diff(now_ms, last_gc) >= _GC_INTERVAL_MS:
                    gc.collect()
                    last_gc = now_ms

            for ev in self.buttons.get_events():
                now_ms = time.ticks_ms()
                if ev == EV_X_TAP:
                    last_x_repeat = now_ms
                elif ev == EV_Y_TAP:
                    last_y_repeat = now_ms
                await self.screen.handle(ev)

            repeat_ms = self.screen.repeat_ms
            if repeat_ms:
                now_ms = time.ticks_ms()
                if self.buttons.x_held and time.ticks_diff(now_ms, last_x_repeat) >= repeat_ms:
                    last_x_repeat = now_ms
                    await self.screen.handle(EV_X_TAP)
                if self.buttons.y_held and time.ticks_diff(now_ms, last_y_repeat) >= repeat_ms:
                    last_y_repeat = now_ms
                    await self.screen.handle(EV_Y_TAP)

            await self._wait_events(_LOOP_TICK_MS)

    # ---- boot --------------------------------------------------------------

    async def _main(self):
        if self.wifi.check():
            hw.led_active()
        else:
            hw.led_error()
            if not self.wifi.connect_blocking():  # asyncio tasks not started yet
                screens.show_message("WiFi Disconnected")
                return

        hw.display.set_backlight(self.load_brightness())
        screens.show_message("Loading Speakers...")

        # INVARIANT: Core 1 must run before the selection loop below.
        self.buttons.start()

        if not await self.load_speakers():
            screens.show_message("No Speakers Found")
            return

        self.speaker_screen.allow_back = False  # no menu behind it at boot
        self.screen = self.speaker_screen
        self.speaker_screen.draw()
        while not self.current_speaker:
            for ev in self.buttons.get_events():
                await self.screen.handle(ev)
            await self._wait_events(_LOOP_TICK_MS)
        self.speaker_screen.allow_back = True

        gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())
        asyncio.create_task(self._state_poll_task())
        asyncio.create_task(self._wifi_check_task())
        await self._action_loop()

    def run(self):
        try:
            asyncio.run(self._main())
        finally:
            self.buttons.running = False  # let Core 1 exit on soft reboot
            asyncio.new_event_loop()
