# machine stub for off-device tests. Pin values default to 1 (not pressed,
# pull-up); tests may set pin.state to simulate presses.
_pins = {}


class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 2

    def __init__(self, pin_id, mode=IN, pull=None):
        self.state = 1
        _pins[pin_id] = self

    def value(self):
        return self.state


_freq = 150_000_000


def freq(value=None):
    global _freq
    if value is None:
        return _freq
    _freq = value
