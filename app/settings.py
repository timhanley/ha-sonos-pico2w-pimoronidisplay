# Settings — merges required values from the user's config.py with defaults
# for the optional tunables, so behaviour can be adjusted without editing
# application code.
import config

# Required — no defaults, a missing value should fail loudly at boot.
WIFI_SSID = config.WIFI_SSID
WIFI_PASSWORD = config.WIFI_PASSWORD
HA_URL = config.HA_URL
HA_TOKEN = config.HA_TOKEN


def _opt(name, default):
    return getattr(config, name, default)


# Optional tunables.
POLL_INTERVAL = _opt("POLL_INTERVAL", 1.0)          # seconds between HA state polls
SCREEN_SLEEP_TIMEOUT = _opt("SCREEN_SLEEP_TIMEOUT", 60)   # s idle before screen off (WiFi stays up)
DEEP_SLEEP_TIMEOUT = _opt("DEEP_SLEEP_TIMEOUT", 3600)     # s idle before deep sleep (WiFi off)
MIN_BRIGHTNESS = _opt("MIN_BRIGHTNESS", 0.25)       # floor reachable via the Brightness menu
DEFAULT_BRIGHTNESS = _opt("DEFAULT_BRIGHTNESS", 1.0)
HTTP_TIMEOUT = _opt("HTTP_TIMEOUT", 10)             # s per HTTP I/O operation
USE_WEBSOCKET = _opt("USE_WEBSOCKET", True)         # push updates via HA websocket;
                                                    # False = REST polling only
DEBUG = _opt("DEBUG", False)                        # enables log.debug() output
