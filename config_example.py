# Copy to config.py on the device and fill in your values.
# config.py is gitignored — never commit credentials.

WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
HA_URL = "http://YOUR_HOME_ASSISTANT_IP:8123"
HA_TOKEN = "YOUR_HOME_ASSISTANT_LONG_LIVED_TOKEN"

# Optional tunables (defaults shown — uncomment to override):
# POLL_INTERVAL = 1.0           # seconds between Home Assistant state polls
# SCREEN_SLEEP_TIMEOUT = 60     # s idle before the screen blanks (WiFi stays up)
# DEEP_SLEEP_TIMEOUT = 3600     # s idle before deep sleep (WiFi off)
# MIN_BRIGHTNESS = 0.25         # brightness floor in the Brightness menu
# DEFAULT_BRIGHTNESS = 1.0
# HTTP_TIMEOUT = 10             # seconds per HTTP operation
# USE_WEBSOCKET = True          # push updates via HA's websocket API;
#                               # set False to use REST polling only
# DEBUG = False                 # verbose logging on the USB console
