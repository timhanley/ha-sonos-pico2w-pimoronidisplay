# WiFi connection management for the CYW43 chip.
#
# Two connect paths exist deliberately:
#   - connect() is async and yields while waiting, so a mid-session reconnect
#     never freezes the UI or the event loop.
#   - connect_blocking() is for contexts where asyncio is not running: boot
#     (before tasks start) and deep-sleep wake (the sleep loop intentionally
#     blocks the scheduler).
#
# INVARIANT (deep sleep): the chip is fully deactivated on sleep entry, so a
# wake reconnect is always a clean active(True) + connect() — never trust
# isconnected() after a long idle (SDIO transport stall / "F2 not ready").
import time

import asyncio
import network

from app import hw, log
from app.settings import WIFI_SSID, WIFI_PASSWORD

_CONNECT_WAIT_S = 10  # max seconds to wait for association + DHCP


class WiFi:
    def __init__(self, notify=None):
        """notify: optional callable(message) used to show connect progress."""
        self.wlan = network.WLAN(network.STA_IF)
        self.connected = False
        self._connecting = False
        self._notify = notify

    def _begin(self):
        self.wlan.active(True)
        hw.led_error()  # dim red: not connected
        time.sleep(0.1)
        hw.led_connecting()
        if self._notify:
            self._notify("Connecting to WiFi...")
        log.info("Connecting to WiFi...")
        self.wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    def _finish(self):
        if self.wlan.status() != 3:
            hw.led_error()
            self.connected = False
            if self._notify:
                self._notify("WiFi Connection Failed")
            log.error("WiFi connection failed")
            return False
        hw.led_active()
        self.connected = True
        log.info("WiFi connected: %s" % self.wlan.ifconfig()[0])
        return True

    def _wait_done(self):
        status = self.wlan.status()
        return status < 0 or status >= 3

    def connect_blocking(self):
        """Connect with blocking waits. Boot and deep-sleep wake only."""
        self._begin()
        for _ in range(_CONNECT_WAIT_S):
            if self._wait_done():
                break
            time.sleep(1)
        return self._finish()

    async def connect(self):
        """Connect while yielding to the event loop (UI stays responsive)."""
        if self._connecting:
            return False  # another task is already reconnecting
        self._connecting = True
        try:
            self._begin()
            for _ in range(_CONNECT_WAIT_S * 10):
                if self._wait_done():
                    break
                await asyncio.sleep(0.1)
            return self._finish()
        finally:
            self._connecting = False

    def check(self):
        """Cheap link check — updates self.connected, never reconnects."""
        self.connected = self.wlan.isconnected()
        return self.connected

    async def ensure_connected(self):
        """Reconnect (async) if the link is down."""
        if self.wlan.isconnected():
            self.connected = True
            return True
        self.connected = False
        return await self.connect()

    def deactivate(self):
        """Power the chip down cleanly (deep sleep entry)."""
        self.wlan.active(False)
        self.connected = False
