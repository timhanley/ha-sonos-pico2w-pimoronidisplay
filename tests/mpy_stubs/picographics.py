# PicoGraphics stub for off-device tests. Records draw calls so tests can
# assert on rendering activity. Deliberately does NOT implement the buffer
# protocol — memoryview(display) raises TypeError, which exercises the
# artwork 'jpeg_file' fallback path.
DISPLAY_PICO_DISPLAY_2 = "DISPLAY_PICO_DISPLAY_2"
PEN_RGB565 = "PEN_RGB565"


class PicoGraphics:
    def __init__(self, display=None, pen_type=None):
        self.calls = []           # (method, args) tuples, in order
        self.update_count = 0
        self.backlight = None
        self._pens = 0

    def _rec(self, name, *args):
        self.calls.append((name, args))
        if len(self.calls) > 400:  # keep soak tests from measuring the stub
            self.calls = self.calls[-200:]

    def get_bounds(self):
        return 320, 240

    def set_font(self, font):
        self._rec("set_font", font)

    def set_update_speed(self, speed):
        self._rec("set_update_speed", speed)

    def create_pen(self, r, g, b):
        self._pens += 1
        return self._pens

    def set_pen(self, pen):
        self._rec("set_pen", pen)

    def clear(self):
        self._rec("clear")

    def rectangle(self, x, y, w, h):
        self._rec("rectangle", x, y, w, h)

    def circle(self, x, y, r):
        self._rec("circle", x, y, r)

    def pixel(self, x, y):
        self._rec("pixel", x, y)

    def text(self, text, x, y, wordwrap=None, scale=1):
        self._rec("text", text, x, y, scale)

    def measure_text(self, text, scale=1):
        return len(text) * 6 * scale

    def set_clip(self, x, y, w, h):
        self._rec("set_clip", x, y, w, h)

    def remove_clip(self):
        self._rec("remove_clip")

    def update(self):
        self.update_count += 1

    def set_backlight(self, value):
        self.backlight = value
