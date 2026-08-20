# jpegdec stub for off-device tests.
JPEG_SCALE_FULL = 0
JPEG_SCALE_HALF = 1
JPEG_SCALE_QUARTER = 2
JPEG_SCALE_EIGHTH = 3


class JPEG:
    def __init__(self, display):
        self.opened = None
        self.decode_calls = []

    def open_file(self, path):
        self.opened = path

    def get_width(self):
        return 600

    def decode(self, x, y, scale):
        self.decode_calls.append((x, y, scale))
