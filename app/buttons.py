# Dual-core button handling.
#
# Core 1 polls the four buttons every 5 ms, draws instant label feedback, and
# pushes discrete events into a small queue; Core 0 awaits a ThreadSafeFlag
# and drains the queue. Compared with v1's shared booleans this cannot lose a
# second press that lands before the first is processed, and Core 0 no longer
# spins at 5 ms.
#
# INVARIANT: start() must be called before any UI loop that expects button
# input (v1 bug: buttons were dead during speaker selection when Core 1
# started too late).
#
# All timing is time.ticks_ms/ticks_diff — time.time() is whole seconds on
# MicroPython and silently breaks sub-second constants.
import time
import _thread

import asyncio

from app import hw

# Event codes (queue entries)
EV_A_SHORT = 0   # play/pause, menu select
EV_A_LONG = 1    # next track
EV_B_SHORT = 2   # menu/back
EV_B_LONG = 3    # previous track
EV_X_TAP = 4     # volume up / menu up
EV_Y_TAP = 5     # volume down / menu down

LONG_PRESS_MS = 1000
FEEDBACK_MS = 200      # how long the green press blob lingers after release
_POLL_MS = 5
STALE_EVENT_MS = 1500  # events queued longer than this are dropped at drain


class Buttons:
    def __init__(self):
        self._q = []
        self._qlock = _thread.allocate_lock()
        self.flag = asyncio.ThreadSafeFlag()
        self.running = False
        # Read by the label renderer (Core 0 or Core 1, under display_lock):
        self.a_ms = self.b_ms = self.x_ms = self.y_ms = time.ticks_ms() - FEEDBACK_MS
        self.a_held = self.b_held = self.x_held = self.y_held = False
        self.a_long_active = False   # blue blob while a long press is latched
        self.b_long_active = False
        self.last_activity_ms = time.ticks_ms()
        # Core 1 calls this (if set) on any press/release to repaint labels.
        # The callback must acquire display_lock itself.
        self.feedback_draw = None

    # ---- Core 0 side -------------------------------------------------------

    def start(self):
        self.running = True
        _thread.start_new_thread(self._core1, ())

    def stop(self):
        """Stop Core 1 and wait out one poll period so it has exited."""
        self.running = False
        time.sleep_ms(4 * _POLL_MS)

    def get_events(self):
        """Drain and return all pending events (possibly empty list).

        Events that sat queued longer than STALE_EVENT_MS are dropped: they
        were pressed while Core 0 was stuck (e.g. a network stall) and
        replaying them later would fire a burst of surprise actions."""
        if not self._q:
            return ()
        self._qlock.acquire()
        entries = self._q
        self._q = []
        self._qlock.release()
        now = time.ticks_ms()
        return [ev for ev, t in entries
                if time.ticks_diff(now, t) <= STALE_EVENT_MS]

    def clear(self):
        """Discard pending events (e.g. the press that woke the device)."""
        self._qlock.acquire()
        self._q = []
        self._qlock.release()

    def idle_ms(self):
        return time.ticks_diff(time.ticks_ms(), self.last_activity_ms)

    def touch(self):
        """Register activity from Core 0 (e.g. on wake)."""
        self.last_activity_ms = time.ticks_ms()

    @staticmethod
    def any_pin_pressed():
        """Direct pin poll — for deep sleep, when Core 1 is stopped."""
        return (hw.button_a.value() == 0 or hw.button_b.value() == 0 or
                hw.button_x.value() == 0 or hw.button_y.value() == 0)

    # ---- Core 1 side -------------------------------------------------------

    def _push(self, ev):
        self._qlock.acquire()
        self._q.append((ev, time.ticks_ms()))
        self._qlock.release()
        self.flag.set()

    def _core1(self):
        a_start = b_start = 0  # press timestamps (ticks_ms)
        while self.running:
            now = time.ticks_ms()
            changed = False

            # A and B: short press fires on release, long press at threshold.
            if hw.button_a.value() == 0:
                if not self.a_held:
                    self.a_held = True
                    a_start = now
                    self.a_ms = now
                    self.last_activity_ms = now
                    changed = True
                elif not self.a_long_active and time.ticks_diff(now, a_start) >= LONG_PRESS_MS:
                    self.a_long_active = True
                    self.a_ms = now
                    self._push(EV_A_LONG)
                    changed = True
            elif self.a_held:
                if not self.a_long_active:
                    self.a_ms = now
                    self._push(EV_A_SHORT)
                self.a_held = False
                self.a_long_active = False
                changed = True

            if hw.button_b.value() == 0:
                if not self.b_held:
                    self.b_held = True
                    b_start = now
                    self.b_ms = now
                    self.last_activity_ms = now
                    changed = True
                elif not self.b_long_active and time.ticks_diff(now, b_start) >= LONG_PRESS_MS:
                    self.b_long_active = True
                    self.b_ms = now
                    self._push(EV_B_LONG)
                    changed = True
            elif self.b_held:
                if not self.b_long_active:
                    self.b_ms = now
                    self._push(EV_B_SHORT)
                self.b_held = False
                self.b_long_active = False
                changed = True

            # X and Y: tap on press; hold-repeat is generated by Core 0
            # from the held flags.
            if hw.button_x.value() == 0:
                if not self.x_held:
                    self.x_held = True
                    self.x_ms = now
                    self.last_activity_ms = now
                    self._push(EV_X_TAP)
                    changed = True
            else:
                self.x_held = False

            if hw.button_y.value() == 0:
                if not self.y_held:
                    self.y_held = True
                    self.y_ms = now
                    self.last_activity_ms = now
                    self._push(EV_Y_TAP)
                    changed = True
            else:
                self.y_held = False

            if changed and self.feedback_draw:
                self.feedback_draw()

            time.sleep_ms(_POLL_MS)
