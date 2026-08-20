# network stub for off-device tests.
STA_IF = 0
AP_IF = 1


class WLAN:
    def __init__(self, interface):
        self._active = False
        self._connected = False

    def active(self, state=None):
        if state is None:
            return self._active
        self._active = state
        if not state:
            self._connected = False

    def connect(self, ssid, password):
        self._connected = True

    def isconnected(self):
        return self._connected

    def status(self):
        return 3 if self._connected else 0

    def ifconfig(self):
        return ("192.0.2.50", "255.255.255.0", "192.0.2.1", "192.0.2.1")
