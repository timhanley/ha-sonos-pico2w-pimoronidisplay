# Screens: Playback, Menu, SpeakerSelect, Brightness.
#
# Each screen owns its full redraw, its button-label captions, and its event
# handling — captions can never disagree with behaviour again. The playback
# screen renders zone-by-zone (status / text / art / volume) so unchanged
# regions (notably album art) survive in the framebuffer between updates and
# display.clear() is never needed on a running screen.
#
# Locking convention: public draw()/update() methods acquire display_lock for
# the whole framebuffer-build → update() sequence; *_locked helpers assume it
# is held.
import time

from app import hw, textutil
from app.buttons import (EV_A_LONG, EV_A_SHORT, EV_B_LONG, EV_B_SHORT,
                         EV_X_TAP, EV_Y_TAP, FEEDBACK_MS)

# Layout constants (320×240, bitmap8: 8px/char at scale 2)
_FRAME_H = 197
_TEXT_X = 110
_CHAR_W = 8
_CHARS_PER_LINE = (hw.WIDTH - _TEXT_X - 20) // _CHAR_W

_LIST_START_Y = 60
_LIST_SPACING = 30
_LIST_VISIBLE = 4


def _frame_locked():
    """Gray border with black interior above the button-label strip."""
    display = hw.display
    display.set_pen(hw.BLACK)
    display.clear()
    display.set_pen(hw.GRAY)
    display.rectangle(0, 0, hw.WIDTH, _FRAME_H)
    display.set_pen(hw.BLACK)
    display.rectangle(2, 2, hw.WIDTH - 4, _FRAME_H - 4)


def _centered_text_locked(text, y, scale=2):
    display = hw.display
    display.text(text, (hw.WIDTH - display.measure_text(text, scale)) // 2, y, scale=scale)


def show_message(message, scale=2):
    """Full-screen centered message (own lock acquisition)."""
    hw.display_lock.acquire()
    try:
        hw.display.set_pen(hw.BLACK)
        hw.display.clear()
        hw.display.set_pen(hw.WHITE)
        _centered_text_locked(message, hw.HEIGHT // 2 - 8 * scale, scale)
        hw.display.update()
    finally:
        hw.display_lock.release()


def show_loading(message="Loading..."):
    """Framed centered message (own lock acquisition)."""
    hw.display_lock.acquire()
    try:
        _frame_locked()
        hw.display.set_pen(hw.WHITE)
        _centered_text_locked(message, hw.HEIGHT // 2 - 10, 2)
        hw.display.update()
    finally:
        hw.display_lock.release()


def draw_button_labels_locked(buttons, labels):
    """Bottom strip: A/B/X/Y circles with press feedback + caption text.
    labels: 4-tuple of captions for (A, B, X, Y). Lock MUST be held."""
    display = hw.display
    display.set_pen(hw.BLACK)
    display.rectangle(0, hw.HEIGHT - 40, hw.WIDTH, 40)

    now = time.ticks_ms()
    circles = (
        (30, "A", buttons.a_held or time.ticks_diff(now, buttons.a_ms) < FEEDBACK_MS,
         buttons.a_long_active),
        (145, "B", buttons.b_held or time.ticks_diff(now, buttons.b_ms) < FEEDBACK_MS,
         buttons.b_long_active),
        (hw.WIDTH - 90, "X", buttons.x_held or time.ticks_diff(now, buttons.x_ms) < FEEDBACK_MS,
         False),
        (hw.WIDTH - 35, "Y", buttons.y_held or time.ticks_diff(now, buttons.y_ms) < FEEDBACK_MS,
         False),
    )
    for x_pos, letter, active, long_active in circles:
        if long_active:
            display.set_pen(hw.BLUE)
        elif active:
            display.set_pen(hw.GREEN)
        else:
            display.set_pen(hw.GRAY)
        display.circle(x_pos, hw.HEIGHT - 20, 7)
        display.set_pen(hw.WHITE)
        display.text(letter, x_pos - 3, hw.HEIGHT - 22, scale=1)

    display.set_pen(hw.WHITE)
    caption_x = (45, 160, hw.WIDTH - 75, hw.WIDTH - 25)
    for x_pos, caption in zip(caption_x, labels):
        if caption:
            display.text(caption, x_pos, hw.HEIGHT - 22, scale=1)


class Screen:
    """Base screen. repeat_ms > 0 enables X/Y hold-repeat at that interval."""
    repeat_ms = 0

    def __init__(self, app):
        self.app = app

    def labels(self):
        return ("", "", "", "")

    def draw(self):
        raise NotImplementedError

    async def handle(self, ev):
        raise NotImplementedError

    def _finish_locked(self):
        draw_button_labels_locked(self.app.buttons, self.labels())
        hw.display.update()


class PlaybackScreen(Screen):
    repeat_ms = 300  # volume hold-repeat

    def __init__(self, app):
        super().__init__(app)
        self.prev_visible = None   # None → next update does a full redraw
        self._error_shown = False

    def labels(self):
        return ("Play/Pause > Next", "Menu < Prev", "Vol+", "Vol-")

    @staticmethod
    def _visible(state):
        """Fields that affect pixels — media position ticking must not redraw."""
        return (state["state"], state["artist"], state["title"],
                state["album"], state["volume"], state["name"])

    def invalidate(self):
        self.prev_visible = None

    def draw(self):
        """Full redraw from app.state (or a waking placeholder)."""
        state = self.app.state
        if state is None:
            show_loading("Waking...")
            self.prev_visible = None
            return
        hw.display_lock.acquire()
        try:
            self._draw_full_locked(state)
        finally:
            hw.display_lock.release()

    def _draw_full_locked(self, state):
        _frame_locked()
        self._zone_status_locked(state)
        self._zone_text_locked(state)
        self._art_zone_locked(state)
        self._zone_volume_locked(state)
        self._finish_locked()
        self.prev_visible = self._visible(state)
        self._error_shown = False

    def update(self, state):
        """Poll-driven update: full redraw on first show, zone redraw after."""
        if state is None:
            self._draw_disconnected()
            return
        self._error_shown = False
        new_visible = self._visible(state)
        if self.prev_visible is None:
            hw.display_lock.acquire()
            try:
                self._draw_full_locked(state)
            finally:
                hw.display_lock.release()
            return
        if new_visible == self.prev_visible:
            return
        old = self.prev_visible
        hw.display_lock.acquire()
        try:
            if new_visible[0] != old[0] or new_visible[5] != old[5]:
                self._zone_status_locked(state)
            if new_visible[1] != old[1] or new_visible[2] != old[2]:
                self._zone_text_locked(state)
            if new_visible[3] != old[3]:
                self._art_zone_locked(state)
            if new_visible[4] != old[4]:
                self._zone_volume_locked(state)
            self._finish_locked()
            self.prev_visible = new_visible
        finally:
            hw.display_lock.release()

    def _draw_disconnected(self):
        """Connectivity error screen — drawn once per outage."""
        if self._error_shown:
            return
        self._error_shown = True
        self.prev_visible = None
        hw.display_lock.acquire()
        try:
            display = hw.display
            display.set_pen(hw.BLACK)
            display.clear()
            display.set_pen(hw.WHITE)
            if not self.app.wifi.connected:
                _centered_text_locked("WiFi Disconnected", hw.HEIGHT // 2, 2)
            else:
                _centered_text_locked("Home Assistant", hw.HEIGHT // 2 - 20, 2)
                _centered_text_locked("Unavailable", hw.HEIGHT // 2 + 10, 2)
            self._finish_locked()
        finally:
            hw.display_lock.release()

    def _zone_status_locked(self, state):
        display = hw.display
        display.set_pen(hw.BLACK)
        display.rectangle(3, 3, hw.WIDTH - 6, 33)
        play_state = state["state"] or "unknown"
        play_state = play_state[0].upper() + play_state[1:].lower()
        display.set_pen(hw.GRAY)
        display.text("%s - %s" % (play_state, state["name"] or ""), 20, 10, scale=2)

    def _zone_text_locked(self, state):
        display = hw.display
        display.set_pen(hw.BLACK)
        display.rectangle(_TEXT_X, 38, hw.WIDTH - _TEXT_X - 2, 123)
        display.set_pen(hw.WHITE)
        display.text("Artist:", _TEXT_X, 40, scale=1)
        line1, line2 = textutil.wrap_two_lines(state["artist"] or "Unknown Artist",
                                               _CHARS_PER_LINE)
        display.text(line1, _TEXT_X, 55, scale=2)
        if line2:
            display.text(line2, _TEXT_X, 75, scale=2)
        display.text("Title:", _TEXT_X, 95, scale=1)
        line1, line2 = textutil.wrap_two_lines(state["title"] or "Unknown Track",
                                               _CHARS_PER_LINE)
        display.text(line1, _TEXT_X, 110, scale=2)
        if line2:
            display.text(line2, _TEXT_X, 130, scale=2)

    def _art_zone_locked(self, state):
        art = self.app.art
        art.maybe_start(state["album"], self.app.ha.art_url(state["picture"]))
        art.draw_locked()

    def _zone_volume_locked(self, state):
        display = hw.display
        display.set_pen(hw.BLACK)
        display.rectangle(3, 161, hw.WIDTH - 6, 32)
        volume = state["volume"] or 0
        display.set_pen(hw.WHITE)
        display.text("Volume: %d%%" % int(volume * 100), hw.WIDTH // 2 - 35, 167, scale=1)
        display.set_pen(hw.GRAY)
        display.rectangle(20, 182, hw.WIDTH - 40, 10)
        display.set_pen(hw.WHITE)
        display.rectangle(20, 182, int((hw.WIDTH - 40) * volume), 10)

    async def handle(self, ev):
        app = self.app
        if ev == EV_A_SHORT:
            await app.media_command("media_play_pause")
        elif ev == EV_A_LONG:
            app.art.cancel()
            await app.media_command("media_next_track")
            app.force_poll = True
        elif ev == EV_B_LONG:
            app.art.cancel()
            await app.media_command("media_previous_track")
            app.force_poll = True
        elif ev == EV_B_SHORT:
            await app.open_menu()
        elif ev == EV_X_TAP:
            await app.media_command("volume_up")
        elif ev == EV_Y_TAP:
            await app.media_command("volume_down")


class MenuScreen(Screen):
    repeat_ms = 200
    ITEMS = ("Select Speaker", "Brightness", "Exit Menu")

    def __init__(self, app):
        super().__init__(app)
        self.index = 0

    def labels(self):
        return ("Select", "Back", "Up", "Down")

    def draw(self):
        hw.display_lock.acquire()
        try:
            display = hw.display
            _frame_locked()
            display.set_pen(hw.WHITE)
            _centered_text_locked("MENU", 20, 2)
            for i, item in enumerate(self.ITEMS):
                y = _LIST_START_Y + i * _LIST_SPACING
                if i == self.index:
                    display.set_pen(hw.GRAY)
                    display.rectangle(20, y - 5, hw.WIDTH - 40, 25)
                display.set_pen(hw.WHITE)
                display.text(item, 30, y, scale=2)
            self._finish_locked()
        finally:
            hw.display_lock.release()

    async def handle(self, ev):
        app = self.app
        if ev in (EV_X_TAP, EV_Y_TAP):
            step = -1 if ev == EV_X_TAP else 1
            self.index = (self.index + step) % len(self.ITEMS)
            self.draw()
        elif ev == EV_A_SHORT:
            item = self.ITEMS[self.index]
            if item == "Select Speaker":
                show_loading("Loading Speakers...")
                if await app.load_speakers():
                    app.set_screen(app.speaker_screen)
                else:
                    self.draw()
            elif item == "Brightness":
                app.set_screen(app.brightness_screen)
            else:  # Exit Menu
                await app.to_playback()
        elif ev == EV_B_SHORT:
            await app.to_playback()


class SpeakerSelectScreen(Screen):
    repeat_ms = 200

    def __init__(self, app):
        super().__init__(app)
        self.index = 0
        self.allow_back = True  # False during boot — no menu to go back to

    def labels(self):
        return ("Select", "Back" if self.allow_back else "", "Up", "Down")

    def draw(self):
        speakers = self.app.speakers
        hw.display_lock.acquire()
        try:
            display = hw.display
            _frame_locked()
            display.set_pen(hw.WHITE)
            _centered_text_locked("SELECT SPEAKER", 20, 2)
            scroll_start = max(0, self.index - (_LIST_VISIBLE - 1))
            scroll_end = min(len(speakers), scroll_start + _LIST_VISIBLE)
            for i in range(scroll_start, scroll_end):
                y = _LIST_START_Y + (i - scroll_start) * _LIST_SPACING
                if i == self.index:
                    display.set_pen(hw.GRAY)
                    display.rectangle(20, y - 5, hw.WIDTH - 40, 25)
                display.set_pen(hw.WHITE)
                display.text(speakers[i]["name"], 30, y, scale=2)
            display.set_pen(hw.WHITE)
            if scroll_start > 0:
                display.text("^", hw.WIDTH - 20, _LIST_START_Y - 20, scale=2)
            if scroll_end < len(speakers):
                display.text("v", hw.WIDTH - 20,
                             _LIST_START_Y + _LIST_VISIBLE * _LIST_SPACING, scale=1)
            self._finish_locked()
        finally:
            hw.display_lock.release()

    async def handle(self, ev):
        app = self.app
        count = len(app.speakers)
        if ev in (EV_X_TAP, EV_Y_TAP) and count:
            step = -1 if ev == EV_X_TAP else 1
            self.index = (self.index + step) % count
            self.draw()
        elif ev == EV_A_SHORT and count:
            await app.select_speaker(app.speakers[self.index]["entity_id"])
        elif ev == EV_B_SHORT and self.allow_back:
            app.set_screen(app.menu_screen)


class BrightnessScreen(Screen):
    repeat_ms = 200

    def labels(self):
        return ("", "Back", "Up", "Down")

    def draw(self):
        hw.display_lock.acquire()
        try:
            display = hw.display
            _frame_locked()
            display.set_pen(hw.WHITE)
            _centered_text_locked("BRIGHTNESS", 20, 2)
            _centered_text_locked("%d%%" % int(self.app.brightness * 100), 60, 2)
            display.set_pen(hw.GRAY)
            display.rectangle(20, 100, hw.WIDTH - 40, 20)
            display.set_pen(hw.WHITE)
            display.rectangle(20, 100, int((hw.WIDTH - 40) * self.app.brightness), 20)
            self._finish_locked()
        finally:
            hw.display_lock.release()

    async def handle(self, ev):
        app = self.app
        if ev == EV_X_TAP:
            app.adjust_brightness(1)
            self.draw()
        elif ev == EV_Y_TAP:
            app.adjust_brightness(-1)
            self.draw()
        elif ev == EV_B_SHORT:
            app.set_screen(app.menu_screen)
