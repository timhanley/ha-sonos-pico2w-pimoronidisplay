# RGBLED stub for off-device tests.


class RGBLED:
    def __init__(self, r, g, b):
        self.rgb = (0, 0, 0)

    def set_rgb(self, r, g, b):
        self.rgb = (r, g, b)
