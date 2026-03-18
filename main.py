# Home Assistant Sonos Display Controller / Remote - Raspberry Pi Pico 2 W
# Copyright 2024 Tim Hanley - see LICENCE.txt for details
# https://github.com/timhanley/ha-sonos-pico2w-pimoronidisplay
# v1.2


import gc  # Add garbage collector
import network
import asyncio
import json
import time
import _thread
import math
from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY_2, PEN_RGB565
from pimoroni import RGBLED
from machine import Pin
from io import BytesIO
try:
    import jpegdec
except ImportError:
    import upip
    upip.install('jpegdec')
    import jpegdec

# WiFi and Home Assistant Configuration — see config_example.py
from config import WIFI_SSID, WIFI_PASSWORD, HA_URL, HA_TOKEN

# Initialize display
display = PicoGraphics(display=DISPLAY_PICO_DISPLAY_2, pen_type=PEN_RGB565)
display.set_font("bitmap8")
WIDTH, HEIGHT = display.get_bounds()

# Initialize LED and Buttons
led = RGBLED(26, 27, 28)  # Pins for Pimoroni Pico Display 2.8"
button_a = Pin(12, Pin.IN, Pin.PULL_UP)  # Play/Pause (A)
button_b = Pin(13, Pin.IN, Pin.PULL_UP)  # Menu (B)
button_x = Pin(14, Pin.IN, Pin.PULL_UP)  # Volume Up/Menu Up (X)
button_y = Pin(15, Pin.IN, Pin.PULL_UP)  # Volume Down/Menu Down (Y)

# Create some pens to use for drawing
WHITE = display.create_pen(255, 255, 255)
BLACK = display.create_pen(0, 0, 0)
GRAY = display.create_pen(128, 128, 128)

# Add constants for connection management
WIFI_RETRY_DELAY = 10  # seconds between WiFi reconnection attempts
HA_RETRY_DELAY = 5     # seconds between HA reconnection attempts
MAX_WIFI_RETRIES = 3   # maximum number of WiFi connection attempts
BUTTON_DEBOUNCE_TIME = 0.05  # 0.05 seconds between button presses
BUTTON_FEEDBACK_TIME = 0.2  # How long to show button press feedback in seconds
last_wifi_check = 0
wifi_check_interval = 30

# Add connection state tracking
wifi_connected = False
ha_connected = False

# Initialize button press tracking
last_button_a_press = 0
last_button_x_press = 0
last_button_y_press = 0
button_a_pressed_time = 0
button_x_pressed_time = 0
button_y_pressed_time = 0
button_b_pressed_time = 0

# Menu constants
MENU_ITEMS = [
    "Select Speaker",
    "Brightness",
    "Exit Menu"
]
current_menu_index = 0
in_menu = False

# Sleep mode constants
SLEEP_TIMEOUT = 60  # Time in seconds before sleep mode activates
LED_PULSE_INTERVAL = 2  # Time in seconds between LED pulses
last_activity_time = 0
is_sleeping = False

# Constants for album art states
ALBUM_ART_IDLE = 0
ALBUM_ART_DOWNLOADING = 1
ALBUM_ART_READY = 2
jpeg = jpegdec.JPEG(display)
current_album_art = None
current_album_art_url = None
album_art_state = ALBUM_ART_IDLE
album_art_response = None
album_art_url = None
state_data = None
current_state_data = None
album_art_loading = False
current_download_response = None  # Store the response object globally
current_album_name = None  # Track current album name

# state constants
last_state_update = 0
state_update_interval = 1.0  # Poll interval for state_poll_task

# Speaker constants
available_speakers = []
current_speaker_index = 0
in_speaker_select = False
current_speaker = None  # Store currently selected speaker entity_id
in_speaker_select = False  # Move this here

# Brightness constants
BRIGHTNESS_FILE = "brightness.json"
DEFAULT_BRIGHTNESS = 1.0
BRIGHTNESS_STEP = 0.05  # 5% steps
MIN_BRIGHTNESS = 0.25  # 25% minimum brightness
current_brightness = 1.0  # Track current brightness
in_brightness_screen = False

# Display constants
PADDING = 20  # Padding for text from screen edges

# Add to the constants section around line 51
LONG_PRESS_TIME = 1.0  # seconds to trigger a long press
button_a_press_start = 0  # Track when button A was first pressed
button_b_press_start = 0  # Track when button B was first pressed

# Dual-core display lock
display_lock = _thread.allocate_lock()

# Core 1 → Core 0 action flags (Core 1 sets, Core 0 clears after processing)
button_a_short_pending = False   # short press (play/pause, menu select)
button_a_long_pending = False    # long press (next track)
button_b_short_pending = False   # short press (open/close menu, back)
button_b_long_pending = False    # long press (previous track)
button_x_held = False            # currently held (volume up / menu up repeat)
button_x_tap_pending = False     # new press event
button_y_held = False            # currently held (volume down / menu down repeat)
button_y_tap_pending = False     # new press event
any_button_pressed = False       # for wake-from-sleep detection

def collect_garbage():
    gc.collect()
    gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())

def safe_display_update():
    display.update()

def get_ha_headers():
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

# ---------------------------------------------------------------------------
# Async HTTP helpers
# ---------------------------------------------------------------------------

async def async_request(method, url, headers=None, json_data=None):
    """Async HTTP request. Returns (status_code, parsed_json_or_None)."""
    collect_garbage()
    url_no_proto = url[7:]  # strip 'http://'
    slash_pos = url_no_proto.find('/')
    if slash_pos == -1:
        host_port, path = url_no_proto, '/'
    else:
        host_port, path = url_no_proto[:slash_pos], url_no_proto[slash_pos:]
    host, port = (host_port.rsplit(':', 1)[0], int(host_port.rsplit(':', 1)[1])) if ':' in host_port else (host_port, 80)

    body_bytes = json.dumps(json_data).encode() if json_data is not None else b''
    req = f'{method} {path} HTTP/1.0\r\nHost: {host}:{port}\r\n'
    if headers:
        req += ''.join(f'{k}: {v}\r\n' for k, v in headers.items())
    if body_bytes:
        req += f'Content-Length: {len(body_bytes)}\r\n'
    req += '\r\n'

    reader, writer = await asyncio.open_connection(host, port)
    writer.write(req.encode() + body_bytes)
    await writer.drain()

    response = b''
    while True:
        chunk = await reader.read(512)
        if not chunk:
            break
        response += chunk
    writer.close()
    collect_garbage()

    header_end = response.find(b'\r\n\r\n')
    if header_end == -1:
        return None, None
    status_code = int(response[:response.find(b'\r\n')].decode().split(' ')[1])
    try:
        return status_code, json.loads(response[header_end + 4:])
    except:
        return status_code, None


async def async_request_to_file(url, headers, filename):
    """Async HTTP GET, streams response body to file. Returns status_code."""
    collect_garbage()
    url_no_proto = url[7:]
    slash_pos = url_no_proto.find('/')
    if slash_pos == -1:
        host_port, path = url_no_proto, '/'
    else:
        host_port, path = url_no_proto[:slash_pos], url_no_proto[slash_pos:]
    host, port = (host_port.rsplit(':', 1)[0], int(host_port.rsplit(':', 1)[1])) if ':' in host_port else (host_port, 80)

    req = f'GET {path} HTTP/1.0\r\nHost: {host}:{port}\r\n'
    if headers:
        req += ''.join(f'{k}: {v}\r\n' for k, v in headers.items())
    req += '\r\n'

    reader, writer = await asyncio.open_connection(host, port)
    writer.write(req.encode())
    await writer.drain()

    # Read status line
    status_line = await reader.readline()
    status_code = int(status_line.decode().split(' ')[1])

    # Skip remaining headers
    while True:
        line = await reader.readline()
        if line in (b'\r\n', b'', b'\n'):
            break

    with open(filename, 'wb') as f:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            f.write(chunk)
            await asyncio.sleep(0)

    writer.close()
    collect_garbage()
    return status_code

# ---------------------------------------------------------------------------
# Async HA functions
# ---------------------------------------------------------------------------

async def get_sonos_state_async():
    global ha_connected
    if not check_wifi_connection() or not current_speaker:
        return None
    try:
        status, data = await async_request('GET', f'{HA_URL}/api/states/{current_speaker}', get_ha_headers())
        if status == 200:
            ha_connected = True
            return data
        ha_connected = False
        return None
    except:
        ha_connected = False
        return None


async def call_ha_service_async(service, data):
    global ha_connected
    if not check_wifi_connection():
        return False
    for attempt in range(2):
        try:
            status, _ = await async_request('POST', f'{HA_URL}/api/services/media_player/{service}', get_ha_headers(), data)
            if status == 200:
                ha_connected = True
                return True
            display_lock.acquire()
            try:
                display.set_pen(BLACK)
                display.clear()
                display.set_pen(WHITE)
                error_text = "HA Connection Error"
                display.text(error_text, (WIDTH - len(error_text) * 8) // 2, HEIGHT // 2, scale=1)
                display.update()
            finally:
                display_lock.release()
        except:
            display_lock.acquire()
            try:
                display.set_pen(BLACK)
                display.clear()
                display.set_pen(WHITE)
                error_text = "HA Connection Error"
                display.text(error_text, (WIDTH - len(error_text) * 8) // 2, HEIGHT // 2, scale=1)
                display.update()
            finally:
                display_lock.release()
        if attempt == 0:
            await asyncio.sleep(0.5)
    ha_connected = False
    return False


async def get_available_speakers_async():
    global available_speakers
    show_loading_screen("Loading Speakers...")
    if not check_wifi_connection():
        show_loading_screen("WiFi Not Connected")
        await asyncio.sleep(2)
        return False
    try:
        # Ping HA first
        status, _ = await async_request('GET', f'{HA_URL}/api', get_ha_headers())
        if status is None:
            raise Exception("Cannot reach HA")
    except:
        display_lock.acquire()
        try:
            display.set_pen(BLACK)
            display.clear()
            display.set_pen(WHITE)
            error_text = "Cannot Reach Home Assistant"
            display.text(error_text, (WIDTH - len(error_text) * 9) // 2, HEIGHT // 2, scale=2)
            display.update()
        finally:
            display_lock.release()
        await asyncio.sleep(2)
        return False
    try:
        template = """
            {% set devices = states | map(attribute='entity_id') | map('device_id') | unique | reject('eq', None) | list %}
            {%- set ns = namespace(sonos_devices=[]) %}
            {%- for device in devices %}
                {%- if 'sonos' in device_attr(device, 'identifiers') | join %}
                    {%- set entities = device_entities(device) | list %}
                    {%- set ns.sonos_devices = ns.sonos_devices + [{'device_id': device, 'device_name': device_attr(device, 'name'), 'entities': entities}] %}
                {%- endif %}
            {%- endfor %}
            {{ ns.sonos_devices | tojson }}
        """
        status, result = await async_request('POST', f'{HA_URL}/api/template', get_ha_headers(), {"template": template})
        if status != 200 or result is None:
            raise Exception("Template API failed")
        collect_garbage()
        speakers = []
        for device in result:
            for entity in device['entities']:
                if entity.startswith('media_player.'):
                    speakers.append({'entity_id': entity, 'name': device['device_name']})
                    print(f"Found speaker: {device['device_name']} ({entity})")
                    break
        if speakers:
            available_speakers = speakers
            print(f"Total Sonos speakers found: {len(speakers)}")
            return True
        print("No Sonos speakers found")
        show_loading_screen("No Sonos\nSpeakers Found")
        await asyncio.sleep(2)
        return False
    except Exception as e:
        print(f"Error fetching speakers: {e}")
        show_loading_screen("Error Loading\nSpeakers\n" + str(e))
        await asyncio.sleep(2)
        return False
    finally:
        collect_garbage()

# ---------------------------------------------------------------------------
# Album art — async task
# ---------------------------------------------------------------------------

async def album_art_task(url, x, y):
    """Async task: download and decode album art."""
    global album_art_state, current_album_art, current_album_art_url, album_art_url
    album_art_state = ALBUM_ART_DOWNLOADING
    try:
        import os
        try:
            os.remove('album_art.jpg')
        except:
            pass
        status = await async_request_to_file(url, get_ha_headers(), 'album_art.jpg')
        if status == 200:
            jpeg.open_file('album_art.jpg')
            jpeg.decode(x, y, jpegdec.JPEG_SCALE_EIGHTH)
            current_album_art = (x, y, jpegdec.JPEG_SCALE_EIGHTH)
            current_album_art_url = url
            album_art_state = ALBUM_ART_READY
        else:
            album_art_state = ALBUM_ART_IDLE
    except Exception as e:
        print(f"Error downloading album art: {e}")
        album_art_state = ALBUM_ART_IDLE

# ---------------------------------------------------------------------------
# Drawing helpers (unchanged)
# ---------------------------------------------------------------------------

def draw_button_labels(force_update=False):
    """Draw button labels with press feedback"""
    current_time = time.time()

    # Clear button areas
    display.set_pen(BLACK)
    display.rectangle(0, HEIGHT-40, WIDTH, 40)  # Bottom area for buttons

    # Check if buttons are currently pressed or recently pressed
    a_active = (current_time - button_a_pressed_time) < BUTTON_FEEDBACK_TIME
    b_active = (current_time - button_b_pressed_time) < BUTTON_FEEDBACK_TIME
    x_active = (current_time - button_x_pressed_time) < BUTTON_FEEDBACK_TIME
    y_active = (current_time - button_y_pressed_time) < BUTTON_FEEDBACK_TIME

    # Create pens for button feedback
    GREEN = display.create_pen(0, 128, 0)
    BLUE = display.create_pen(0, 64, 192)

    # Long press detected when press_start sentinel is -1
    a_long = (button_a_press_start == -1)
    b_long = (button_b_press_start == -1)

    # Draw button circles with feedback
    button_positions = [
        ("A", a_active, a_long, 30),
        ("B", b_active, b_long, 145),  # Moved B button further right from 135
        ("X", x_active, False, WIDTH-90),
        ("Y", y_active, False, WIDTH-35)
    ]

    # Draw circles and centered labels
    for button, active, long_press, x_pos in button_positions:
        if long_press:
            display.set_pen(BLUE)
        elif active:
            display.set_pen(GREEN)
        else:
            display.set_pen(GRAY)
        display.circle(x_pos, HEIGHT-20, 7)

        # Center the letter in the circle
        display.set_pen(WHITE)
        display.text(button, x_pos-3, HEIGHT-22, scale=1)

    display.set_pen(WHITE)
    # Draw function labels
    if in_menu:
        display.text("Select", 45, HEIGHT-22, scale=1)
        display.text("Back", 160, HEIGHT-22, scale=1)  # Moved label right from 150
        display.text("Up", WIDTH-75, HEIGHT-22, scale=1)
        display.text("Down", WIDTH-25, HEIGHT-22, scale=1)
    else:
        display.text("Play/Pause > Next", 45, HEIGHT-22, scale=1)
        display.text("Menu < Prev", 160, HEIGHT-22, scale=1)  # Moved label right from 150
        display.text("Vol+", WIDTH-75, HEIGHT-22, scale=1)
        display.text("Vol-", WIDTH-25, HEIGHT-22, scale=1)

    if force_update:
        display.update()

def draw_screen(state_data):
    """Draw the main screen with the provided state data"""
    global album_art_loading, current_state_data, album_art_state, current_album_art_url
    global current_album_name, current_album_art

    display_lock.acquire()
    try:
        if state_data is None:
            return

        current_state_data = state_data
        collect_garbage()

        display.set_pen(BLACK)
        display.clear()

        if not wifi_connected:
            display.set_pen(WHITE)
            display.text("WiFi Disconnected", WIDTH//2 - 60, HEIGHT//2, scale=2)
            draw_button_labels()
            display.update()
            return
        elif not ha_connected:
            display.set_pen(WHITE)
            display.text("Home Assistant", WIDTH//2 - 60, HEIGHT//2 - 20, scale=2)
            display.text("Unavailable", WIDTH//2 - 40, HEIGHT//2 + 10, scale=2)
            draw_button_labels()
            display.update()
            return

        # Draw speaker info box
        display.set_pen(GRAY)
        display.rectangle(0, 0, WIDTH, 197)
        display.set_pen(BLACK)
        display.rectangle(2, 2, WIDTH-4, 193)

        # Define character width for text calculations
        char_width = 8  # Bitmap8 at scale 2 is exactly 8 pixels per char

        # Draw play state with speaker name
        state = state_data.get('state', 'unknown')
        if isinstance(state, str):
            state = state[0].upper() + state[1:].lower()
        else:
            state = "Unknown"

        display.set_pen(GRAY)

        speaker_name = state_data.get('attributes', {}).get('friendly_name', '')
        status_text = f"{state} - {speaker_name}"

        # Try centering with smaller offset
        display.text(status_text, 20, 10, scale=2)  # Align with album art x position

        # Artist section - with word wrap
        artist = state_data['attributes'].get('media_artist', 'Unknown Artist')
        display.set_pen(WHITE)
        display.text("Artist:", 110, 40, scale=1)

        # Calculate available space for text
        text_start = 110  # Starting position for text
        available_width = WIDTH - text_start - 20  # Space between start position and right border
        chars_per_line = available_width // char_width  # How many characters fit in available space

        # Split artist if needed
        if len(artist) > chars_per_line:
            # Find last space before limit
            space_pos = artist[:chars_per_line].rfind(' ')
            if space_pos > 0:
                first_line = artist[:space_pos]
                second_line = artist[space_pos + 1:]
                display.text(first_line, text_start, 55, scale=2)
                display.text(second_line, text_start, 75, scale=2)
            else:
                # Force split at chars_per_line
                first_line = artist[:chars_per_line]
                second_line = artist[chars_per_line:]
                display.text(first_line, text_start, 55, scale=2)
                display.text(second_line, text_start, 75, scale=2)
        else:
            display.text(artist, text_start, 55, scale=2)

        # Title section - with proper wrapping
        title = state_data['attributes'].get('media_title', 'Unknown Track')
        display.text("Title:", 110, 95, scale=1)

        # Calculate exact character width and available space for title
        char_width = 8  # Bitmap8 at scale 2 is exactly 8 pixels per char
        text_start = 110  # Starting position for text
        available_width = WIDTH - text_start - 20  # Space between start position and right border
        chars_per_line = available_width // char_width  # How many characters fit in available space

        # Split title if needed
        if len(title) > chars_per_line:
            # Find last space before limit
            space_pos = title[:chars_per_line].rfind(' ')
            if space_pos > 0:
                first_line = title[:space_pos]
                second_line = title[space_pos + 1:]
                display.text(first_line, text_start, 110, scale=2)
                display.text(second_line, text_start, 130, scale=2)
            else:
                # Force split at chars_per_line
                first_line = title[:chars_per_line]
                second_line = title[chars_per_line:]
                display.text(first_line, text_start, 110, scale=2)
                display.text(second_line, text_start, 130, scale=2)
        else:
            display.text(title, text_start, 110, scale=2)

        # Album art handling
        if gc.mem_free() > 30000:

            # Get new album name and compare with stored name
            new_album_name = state_data['attributes'].get('media_album_name')
            album_art_url = get_album_art(state_data)


            # Check if album name changed
            if new_album_name != current_album_name:
                # Clear current art and reset state
                current_album_art = None
                current_album_art_url = None
                album_art_state = ALBUM_ART_IDLE
                current_album_name = new_album_name  # Update stored album name

                # If we have a new URL, start the download as an async task
                if album_art_url:
                    asyncio.create_task(album_art_task(album_art_url, 20, 60))
            elif album_art_state == ALBUM_ART_IDLE and album_art_url and not current_album_art:
                # No album change but we need to load art
                asyncio.create_task(album_art_task(album_art_url, 20, 60))

        # Draw placeholder or current album art
        if album_art_state == ALBUM_ART_DOWNLOADING:
            # Draw loading placeholder
            display.set_pen(GRAY)
            display.rectangle(20, 40, 80, 80)  # This is our reference position
            display.set_pen(WHITE)
            # Center text in the 80x80 box
            text = "Loading..."
            text_width = len(text) * 6  # Adjusted from 8 to 6 pixels per character
            text_height = 8
            text_x = 20 + (80 - text_width) // 2 + 2  # Added small offset for fine-tuning
            text_y = 40 + (80 - text_height) // 2
            display.text(text, text_x, text_y, scale=1)
        elif album_art_state == ALBUM_ART_READY and current_album_art is not None:
            try:
                x, y, scale = current_album_art
                jpeg.open_file('album_art.jpg')
                jpeg.decode(20, 40, scale)  # Changed to use fixed coordinates instead of x, y
            except Exception as e:
                print(f"Error drawing album art: {e}")
                album_art_state = ALBUM_ART_IDLE
                current_album_art = None
                current_album_art_url = None
        else:
            # Draw empty placeholder
            display.set_pen(GRAY)
            display.rectangle(20, 40, 80, 80)
            display.set_pen(WHITE)
            text = "No Art"
            text_width = len(text) * 6  # Adjusted from 8 to 6 pixels per character
            text_height = 8
            text_x = 20 + (80 - text_width) // 2 + 2  # Added small offset for fine-tuning
            text_y = 40 + (80 - text_height) // 2
            display.text(text, text_x, text_y, scale=1)

        # Keep volume section at current position
        volume = state_data['attributes'].get('volume_level', 0)
        vol_text = f"Volume: {int(volume * 100)}%"
        display.set_pen(WHITE)  # Make sure we're using white for the text
        display.text(vol_text, WIDTH//2 - 35, 167, scale=1)

        # Volume bar
        display.set_pen(GRAY)
        display.rectangle(20, 182, WIDTH-40, 10)
        display.set_pen(WHITE)
        vol_width = int((WIDTH-40) * volume)
        display.rectangle(20, 182, vol_width, 10)

        # Draw button labels
        draw_button_labels()

        display.update()
        collect_garbage()

    except Exception as e:
        print(f"Critical error in draw_screen: {e}")
        print(f"Error occurred at line: {e.__traceback__.tb_lineno}")

    finally:
        display_lock.release()

def show_message(message, scale=2):
    """Helper function to show centered messages"""
    display_lock.acquire()
    try:
        display.set_pen(BLACK)
        display.clear()
        display.set_pen(WHITE)

        # Calculate center position using 6 pixels per character instead of 8
        x = WIDTH//2 - (len(message) * 6 * scale)//2
        y = HEIGHT//2 - (8 * scale)

        display.text(message, x, y, scale=scale)
        display.update()
    finally:
        display_lock.release()

def connect_wifi():
    global wifi_connected
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    # Set initial LED state to dim red to show no connection
    led.set_rgb(16, 0, 0)  # Dim red at same brightness level
    time.sleep(0.1)  # Brief pause
    led.set_rgb(16, 8, 0)  # Orange/yellow while connecting, but dimmer

    # Show connecting message
    show_message("Connecting to WiFi...")
    print("Connecting to WiFi...")

    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    # Wait for connection with timeout
    max_wait = 10
    while max_wait > 0:
        if wlan.status() < 0 or wlan.status() >= 3:
            break
        max_wait -= 1
        print("Waiting for WiFi connection...")
        time.sleep(1)

    if wlan.status() != 3:
        led.set_rgb(16, 0, 0)  # Red for failed connection, but dimmer
        wifi_connected = False
        show_message("WiFi Connection Failed")
        print("WiFi connection failed")
        return False

    led.set_rgb(0, 16, 0)  # Green for connected
    wifi_connected = True
    print("WiFi connected successfully")
    return True

def check_wifi_connection():
    global wifi_connected
    wlan = network.WLAN(network.STA_IF)
    if not wlan.isconnected():
        wifi_connected = False
        return connect_wifi()
    wifi_connected = True
    return True

def draw_menu():
    """Draw the menu interface"""
    display_lock.acquire()
    try:
        # Clear the entire screen first
        display.set_pen(BLACK)
        display.clear()

        # Draw border
        display.set_pen(GRAY)
        display.rectangle(0, 0, WIDTH, 197)
        display.set_pen(BLACK)
        display.rectangle(2, 2, WIDTH-4, 193)

        # Draw menu title
        display.set_pen(WHITE)
        display.text("MENU", WIDTH//2 - 20, 20, scale=2)

        # Calculate menu item spacing
        menu_start_y = 60  # Start lower to account for title
        menu_spacing = 30  # Increase spacing between items

        # Draw menu items
        for i, item in enumerate(MENU_ITEMS):
            if i == current_menu_index:
                # Highlight selected item
                display.set_pen(GRAY)
                display.rectangle(20, menu_start_y + (i * menu_spacing) - 5, WIDTH-40, 25)
            display.set_pen(WHITE)
            display.text(item, 30, menu_start_y + (i * menu_spacing), scale=2)

        # Draw button labels
        draw_button_labels()
        display.update()
    finally:
        display_lock.release()

# ---------------------------------------------------------------------------
# Async handler functions
# ---------------------------------------------------------------------------

async def handle_speaker_select_async(button_pressed):
    """Handle speaker selection — async variant (used for 'A' which fetches state)."""
    global current_speaker_index, in_speaker_select, in_menu, current_speaker, current_state_data

    if button_pressed == 'X':  # Up
        if len(available_speakers) > 0:
            current_speaker_index = (current_speaker_index - 1) % len(available_speakers)
            draw_speaker_select()
    elif button_pressed == 'Y':  # Down
        if len(available_speakers) > 0:
            current_speaker_index = (current_speaker_index + 1) % len(available_speakers)
            draw_speaker_select()
    elif button_pressed == 'A':  # Select
        if len(available_speakers) > 0:
            current_speaker = available_speakers[current_speaker_index]['entity_id']
            in_speaker_select = False
            in_menu = False
            # Return to main screen
            new_state = await get_sonos_state_async()
            if new_state:
                current_state_data = new_state
                draw_screen(current_state_data)
    elif button_pressed == 'B':  # Back
        in_speaker_select = False
        draw_menu()


async def handle_menu_navigation_async(button_pressed):
    """Handle menu navigation — async variant."""
    global current_menu_index, in_menu, in_speaker_select, current_state_data, in_brightness_screen, button_b_pressed_time

    if button_pressed == 'X' or button_pressed == 'Y':  # Up/Down
        current_menu_index = (current_menu_index - 1 if button_pressed == 'X' else current_menu_index + 1) % len(MENU_ITEMS)
        draw_menu()
    elif button_pressed == 'A':  # Select
        if MENU_ITEMS[current_menu_index] == "Select Speaker":
            if await get_available_speakers_async():
                in_speaker_select = True
                button_b_pressed_time = 0  # Reset button feedback
                draw_speaker_select()
            else:
                draw_menu()
        elif MENU_ITEMS[current_menu_index] == "Brightness":
            in_brightness_screen = True
            button_b_pressed_time = 0  # Reset button feedback
            draw_brightness_screen()
        elif MENU_ITEMS[current_menu_index] == "Exit Menu":
            in_menu = False
            button_b_pressed_time = 0  # Reset button feedback
            new_state = await get_sonos_state_async()
            if new_state:
                current_state_data = new_state
                draw_screen(new_state)
    elif button_pressed == 'B':  # Back
        in_menu = False
        button_b_pressed_time = 0  # Reset button feedback
        new_state = await get_sonos_state_async()
        if new_state:
            current_state_data = new_state
            draw_screen(new_state)


async def wake_device_async():
    """Wake the device from sleep mode — async variant."""
    global is_sleeping, last_activity_time, current_state_data, current_album_name
    is_sleeping = False
    last_activity_time = time.time()
    display.set_backlight(current_brightness)  # Use saved brightness
    led.set_rgb(0, 16, 0)

    # Redraw the appropriate screen
    if in_speaker_select:
        draw_speaker_select()
    elif in_brightness_screen:  # Add brightness screen check
        draw_brightness_screen()
    elif in_menu:
        draw_menu()
    else:
        new_state = await get_sonos_state_async()
        if new_state:
            current_state_data = new_state
            draw_screen(new_state)

# ---------------------------------------------------------------------------
# Unchanged helper functions
# ---------------------------------------------------------------------------

def pulse_led():
    """Simple blink in sleep mode"""
    current_time = time.time()
    # Blink every second
    if int(current_time) % 2 == 0:
        led.set_rgb(0, 16, 0)  # Dim green on
    else:
        led.set_rgb(0, 0, 0)   # LED off

def enter_sleep_mode():
    """Enter low power sleep mode"""
    global is_sleeping
    is_sleeping = True
    display.set_backlight(0)
    display_lock.acquire()
    try:
        draw_button_labels(True)
    finally:
        display_lock.release()

def update_activity():
    """Update the last activity timestamp"""
    global last_activity_time
    last_activity_time = time.time()

def get_album_art(state_data):
    """Get album art URL from state data"""
    try:
        entity_picture = state_data['attributes'].get('entity_picture')
        if entity_picture:
            if entity_picture.startswith('/'):
                return f"{HA_URL}{entity_picture}"
            return entity_picture
    except:
        return None
    return None

def show_loading_screen(message="Loading..."):
    """Show a loading screen with a message"""
    display_lock.acquire()
    try:
        # Clear the screen and draw border
        display.set_pen(BLACK)
        display.clear()
        display.set_pen(GRAY)
        display.rectangle(0, 0, WIDTH, 197)
        display.set_pen(BLACK)
        display.rectangle(2, 2, WIDTH-4, 193)

        # Draw message
        display.set_pen(WHITE)
        text_width = len(message) * 12  # Approximate width for scale 2
        text_x = (WIDTH - text_width) // 2
        text_y = HEIGHT // 2 - 10
        display.text(message, text_x, text_y, scale=2)
        display.update()
    finally:
        display_lock.release()

def draw_speaker_select():
    """Draw the speaker selection interface"""
    display_lock.acquire()
    try:
        # Clear the screen and draw border
        display.set_pen(BLACK)
        display.clear()
        display.set_pen(GRAY)
        display.rectangle(0, 0, WIDTH, 197)
        display.set_pen(BLACK)
        display.rectangle(2, 2, WIDTH-4, 193)

        # Draw title
        display.set_pen(WHITE)
        display.text("SELECT SPEAKER", WIDTH//2 - 70, 20, scale=2)

        # Calculate spacing and visible items
        start_y = 60
        spacing = 30
        visible_items = 4  # Number of items that fit on screen

        # Calculate scroll position
        scroll_start = max(0, current_speaker_index - (visible_items - 1))
        scroll_end = min(len(available_speakers), scroll_start + visible_items)

        # Draw speakers
        for i in range(scroll_start, scroll_end):
            y_pos = start_y + ((i - scroll_start) * spacing)

            if i == current_speaker_index:
                # Highlight selected speaker
                display.set_pen(GRAY)
                display.rectangle(20, y_pos - 5, WIDTH-40, 25)

            display.set_pen(WHITE)
            display.text(available_speakers[i]['name'], 30, y_pos, scale=2)

        # Draw scroll indicators if needed
        if scroll_start > 0:
            # Draw up arrow
            display.set_pen(WHITE)
            display.text("^", WIDTH-20, start_y - 20, scale=2)

        if scroll_end < len(available_speakers):
            # Draw down arrow
            display.set_pen(WHITE)
            display.text("v", WIDTH-20, start_y + (visible_items * spacing), scale=1)

        # Update button labels for this screen
        draw_speaker_select_buttons()
        display.update()
    finally:
        display_lock.release()

def draw_speaker_select_buttons():
    """Draw button labels for speaker selection"""
    display.set_pen(BLACK)
    display.rectangle(0, HEIGHT-40, WIDTH, 40)

    # Create button labels
    button_positions = [
        ("A", "Select", 30),
        ("B", "Back", 120),
        ("X", "Up", WIDTH-90),
        ("Y", "Down", WIDTH-35)
    ]

    for button, label, x_pos in button_positions:
        # Draw button circles
        display.set_pen(GRAY)
        display.circle(x_pos, HEIGHT-20, 7)

        # Draw button letters
        display.set_pen(WHITE)
        display.text(button, x_pos-3, HEIGHT-22, scale=1)
        display.text(label, x_pos + (15 if label != "Back" else 10), HEIGHT-22, scale=1)

def handle_speaker_select(button_pressed):
    """Handle speaker selection navigation for X/Y (no HA calls — sync is fine)."""
    global current_speaker_index, in_speaker_select

    if button_pressed == 'X':  # Up
        if len(available_speakers) > 0:
            current_speaker_index = (current_speaker_index - 1) % len(available_speakers)
            draw_speaker_select()
    elif button_pressed == 'Y':  # Down
        if len(available_speakers) > 0:
            current_speaker_index = (current_speaker_index + 1) % len(available_speakers)
            draw_speaker_select()

def save_brightness(brightness):
    """Save brightness setting to file"""
    global current_brightness
    current_brightness = brightness
    try:
        with open(BRIGHTNESS_FILE, 'w') as f:
            json.dump({'brightness': brightness}, f)
    except:
        print("Error saving brightness")

def load_brightness():
    """Load brightness setting from file"""
    global current_brightness
    try:
        with open(BRIGHTNESS_FILE, 'r') as f:
            data = json.load(f)
            current_brightness = float(data.get('brightness', DEFAULT_BRIGHTNESS))
            return current_brightness
    except:
        current_brightness = DEFAULT_BRIGHTNESS
        return DEFAULT_BRIGHTNESS

def draw_brightness_screen():
    """Draw the brightness control interface"""
    display_lock.acquire()
    try:
        # Clear the screen and draw border
        display.set_pen(BLACK)
        display.clear()
        display.set_pen(GRAY)
        display.rectangle(0, 0, WIDTH, 197)
        display.set_pen(BLACK)
        display.rectangle(2, 2, WIDTH-4, 193)

        # Draw title
        display.set_pen(WHITE)
        display.text("BRIGHTNESS", WIDTH//2 - 50, 20, scale=2)

        # Use tracked brightness value
        percent = int(current_brightness * 100)

        # Draw percentage
        text = f"{percent}%"
        display.text(text, WIDTH//2 - 20, 60, scale=2)

        # Draw brightness bar
        display.set_pen(GRAY)
        display.rectangle(20, 100, WIDTH-40, 20)
        display.set_pen(WHITE)
        bar_width = int((WIDTH-40) * current_brightness)
        display.rectangle(20, 100, bar_width, 20)

        # Draw button labels
        display.set_pen(BLACK)
        display.rectangle(0, HEIGHT-40, WIDTH, 40)

        # Create button labels
        button_positions = [
            ("B", "Back", 30),
            ("X", "Up", WIDTH-90),
            ("Y", "Down", WIDTH-35)
        ]

        for button, label, x_pos in button_positions:
            display.set_pen(GRAY)
            display.circle(x_pos, HEIGHT-20, 7)
            display.set_pen(WHITE)
            display.text(button, x_pos-3, HEIGHT-22, scale=1)
            display.text(label, x_pos + 15, HEIGHT-22, scale=1)

        display.update()
    finally:
        display_lock.release()

def handle_brightness_control(button_pressed):
    """Handle brightness adjustments"""
    global current_brightness, in_brightness_screen

    if button_pressed == 'X':  # Up
        new_brightness = min(1.0, current_brightness + BRIGHTNESS_STEP)
        display.set_backlight(new_brightness)
        save_brightness(new_brightness)
        draw_brightness_screen()
    elif button_pressed == 'Y':  # Down
        new_brightness = max(MIN_BRIGHTNESS, current_brightness - BRIGHTNESS_STEP)  # Minimum 25%
        display.set_backlight(new_brightness)
        save_brightness(new_brightness)
        draw_brightness_screen()
    elif button_pressed == 'B':  # Back
        in_brightness_screen = False  # Clear brightness screen flag
        draw_menu()  # Return to menu instead of main screen


def button_core():
    """Runs on Core 1. Polls buttons, gives immediate visual feedback, sets action flags."""
    global button_a_pressed_time, button_b_pressed_time
    global button_x_pressed_time, button_y_pressed_time
    global button_a_short_pending, button_a_long_pending
    global button_b_short_pending, button_b_long_pending
    global button_x_held, button_x_tap_pending
    global button_y_held, button_y_tap_pending
    global any_button_pressed, last_activity_time
    global button_a_press_start, button_b_press_start

    a_was_held = False
    b_was_held = False
    x_was_held = False
    y_was_held = False

    while True:
        current_time = time.time()
        any_changed = False

        # --- Button A ---
        if button_a.value() == 0:
            any_button_pressed = True
            if not a_was_held:
                a_was_held = True
                button_a_press_start = current_time
                button_a_pressed_time = current_time
                last_activity_time = current_time
                any_changed = True
            elif button_a_press_start > 0 and (current_time - button_a_press_start) >= LONG_PRESS_TIME:
                button_a_press_start = -1
                button_a_pressed_time = current_time
                button_a_long_pending = True
                any_changed = True
        else:
            if a_was_held:
                if button_a_press_start > 0:
                    button_a_pressed_time = current_time
                    button_a_short_pending = True
                    any_changed = True
                button_a_press_start = 0
                a_was_held = False

        # --- Button B ---
        if button_b.value() == 0:
            any_button_pressed = True
            if not b_was_held:
                b_was_held = True
                button_b_press_start = current_time
                button_b_pressed_time = current_time
                last_activity_time = current_time
                any_changed = True
            elif button_b_press_start > 0 and (current_time - button_b_press_start) >= LONG_PRESS_TIME:
                button_b_press_start = -1
                button_b_pressed_time = current_time
                button_b_long_pending = True
                any_changed = True
        else:
            if b_was_held:
                if button_b_press_start > 0:
                    button_b_pressed_time = current_time
                    button_b_short_pending = True
                    any_changed = True
                button_b_press_start = 0
                b_was_held = False

        # --- Button X ---
        if button_x.value() == 0:
            any_button_pressed = True
            button_x_held = True
            if not x_was_held:
                x_was_held = True
                button_x_pressed_time = current_time
                button_x_tap_pending = True
                last_activity_time = current_time
                any_changed = True
        else:
            button_x_held = False
            x_was_held = False

        # --- Button Y ---
        if button_y.value() == 0:
            any_button_pressed = True
            button_y_held = True
            if not y_was_held:
                y_was_held = True
                button_y_pressed_time = current_time
                button_y_tap_pending = True
                last_activity_time = current_time
                any_changed = True
        else:
            button_y_held = False
            y_was_held = False

        # Immediate visual feedback on any state change
        if any_changed:
            display_lock.acquire()
            draw_button_labels(True)
            display_lock.release()

        time.sleep(0.005)

# ---------------------------------------------------------------------------
# Async tasks
# ---------------------------------------------------------------------------

async def state_poll_task():
    global last_state_update, current_state_data
    while True:
        await asyncio.sleep(state_update_interval)
        if not is_sleeping and not in_menu and not in_speaker_select and not in_brightness_screen:
            new_state = await get_sonos_state_async()
            if new_state and new_state != current_state_data:
                current_state_data = new_state
                draw_screen(current_state_data)
            last_state_update = time.time()


async def wifi_check_task():
    while True:
        await asyncio.sleep(wifi_check_interval)
        check_wifi_connection()

# ---------------------------------------------------------------------------
# Button action loop (Core 0 async)
# ---------------------------------------------------------------------------

async def button_action_loop():
    global button_a_short_pending, button_a_long_pending
    global button_b_short_pending, button_b_long_pending
    global button_x_tap_pending, button_y_tap_pending
    global last_x_repeat, last_y_repeat, current_state_data
    global in_menu, in_speaker_select, in_brightness_screen
    global any_button_pressed, is_sleeping, last_activity_time

    while True:
        current_time = time.time()

        # Handle sleep mode
        if is_sleeping:
            pulse_led()
            if any_button_pressed:
                any_button_pressed = False
                button_a_short_pending = False
                button_a_long_pending = False
                button_b_short_pending = False
                button_b_long_pending = False
                button_x_tap_pending = False
                button_y_tap_pending = False
                await wake_device_async()
            await asyncio.sleep(0.1)
            continue

        # Check sleep timeout
        if current_time - last_activity_time >= SLEEP_TIMEOUT:
            enter_sleep_mode()
            await asyncio.sleep(0.1)
            continue

        collect_garbage()

        # Button B: menu/back (short) or previous track (long, main screen only)
        if button_b_long_pending and not in_menu and not in_speaker_select and not in_brightness_screen:
            button_b_long_pending = False
            update_activity()
            await call_ha_service_async("media_previous_track", {"entity_id": current_speaker})
        elif button_b_short_pending:
            button_b_short_pending = False
            button_b_long_pending = False
            update_activity()
            if not in_menu and not in_speaker_select and not in_brightness_screen:
                in_menu = True
                current_menu_index = 0
                draw_menu()
            elif in_speaker_select:
                in_speaker_select = False
                draw_menu()
            elif in_brightness_screen:
                in_brightness_screen = False
                draw_menu()
            else:
                in_menu = False
                new_state = await get_sonos_state_async()
                if new_state:
                    current_state_data = new_state
                    draw_screen(new_state)

        # Context-specific buttons
        if in_speaker_select:
            if button_a_short_pending:
                button_a_short_pending = False
                update_activity()
                await handle_speaker_select_async('A')
            if button_x_tap_pending or (button_x_held and current_time - last_x_repeat >= 0.2):
                button_x_tap_pending = False
                update_activity()
                handle_speaker_select('X')
                last_x_repeat = current_time
            if button_y_tap_pending or (button_y_held and current_time - last_y_repeat >= 0.2):
                button_y_tap_pending = False
                update_activity()
                handle_speaker_select('Y')
                last_y_repeat = current_time

        elif in_brightness_screen:
            if button_x_tap_pending or (button_x_held and current_time - last_x_repeat >= 0.2):
                button_x_tap_pending = False
                update_activity()
                handle_brightness_control('X')
                last_x_repeat = current_time
            if button_y_tap_pending or (button_y_held and current_time - last_y_repeat >= 0.2):
                button_y_tap_pending = False
                update_activity()
                handle_brightness_control('Y')
                last_y_repeat = current_time

        elif in_menu:
            if button_a_short_pending:
                button_a_short_pending = False
                update_activity()
                await handle_menu_navigation_async('A')
            if button_x_tap_pending or (button_x_held and current_time - last_x_repeat >= 0.2):
                button_x_tap_pending = False
                update_activity()
                await handle_menu_navigation_async('X')
                last_x_repeat = current_time
            if button_y_tap_pending or (button_y_held and current_time - last_y_repeat >= 0.2):
                button_y_tap_pending = False
                update_activity()
                await handle_menu_navigation_async('Y')
                last_y_repeat = current_time

        else:  # Main playback screen
            if button_a_long_pending:
                button_a_long_pending = False
                update_activity()
                await call_ha_service_async("media_next_track", {"entity_id": current_speaker})
            elif button_a_short_pending:
                button_a_short_pending = False
                update_activity()
                await call_ha_service_async("media_play_pause", {"entity_id": current_speaker})

            if button_x_tap_pending or (button_x_held and current_time - last_x_repeat >= 0.3):
                button_x_tap_pending = False
                update_activity()
                await call_ha_service_async("volume_up", {"entity_id": current_speaker})
                last_x_repeat = current_time

            if button_y_tap_pending or (button_y_held and current_time - last_y_repeat >= 0.3):
                button_y_tap_pending = False
                update_activity()
                await call_ha_service_async("volume_down", {"entity_id": current_speaker})
                last_y_repeat = current_time

        await asyncio.sleep(0.005)

# ---------------------------------------------------------------------------
# Async main entry point
# ---------------------------------------------------------------------------

async def async_main():
    global current_speaker, in_speaker_select, is_sleeping, album_art_response
    global last_button_a_press, last_button_x_press, last_button_y_press
    global button_a_pressed_time, button_x_pressed_time, button_y_pressed_time
    global current_menu_index, in_menu, last_activity_time
    global current_state_data, last_state_update, last_wifi_check
    global button_b_pressed_time, album_art_loading, album_art_state
    global current_album_art, current_album_art_url
    global state_data, in_brightness_screen, button_a_press_start
    global button_b_press_start
    global button_a_short_pending, button_a_long_pending
    global button_b_short_pending, button_b_long_pending
    global button_x_held, button_x_tap_pending
    global button_y_held, button_y_tap_pending
    global any_button_pressed
    global last_x_repeat, last_y_repeat

    # Initialize all global variables
    current_speaker = None
    in_speaker_select = False
    is_sleeping = False
    album_art_response = None
    current_menu_index = 0
    in_menu = False
    in_brightness_screen = False
    album_art_loading = False
    album_art_state = ALBUM_ART_IDLE
    current_album_art = None
    current_album_art_url = None
    state_data = None
    current_state_data = None
    button_a_press_start = 0  # Add initialization for long press tracking
    button_b_press_start = 0
    last_x_repeat = 0
    last_y_repeat = 0

    # Initialize timing variables with current time
    current_time = time.time()
    last_button_a_press = current_time
    last_button_x_press = current_time
    last_button_y_press = current_time
    button_a_pressed_time = current_time
    button_x_pressed_time = current_time
    button_y_pressed_time = current_time
    button_b_pressed_time = current_time
    last_activity_time = current_time
    last_state_update = current_time
    last_wifi_check = current_time

    # Check initial WiFi status and set LED accordingly
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        led.set_rgb(0, 16, 0)  # Green if already connected
    else:
        led.set_rgb(16, 0, 0)  # Red if not connected yet
        # Try to connect to WiFi
        if not connect_wifi():
            # If WiFi connection fails, show message and return
            show_message("WiFi Disconnected")
            return

    # Initialize display with default brightness
    display.set_backlight(load_brightness())

    # Show loading message before fetching speakers
    show_message("Loading Speakers...")

    # Start Core 1 button polling thread early so buttons work during speaker selection
    _thread.start_new_thread(button_core, ())

    # Only proceed with speaker selection if WiFi is connected
    if await get_available_speakers_async():
        in_speaker_select = True
        draw_speaker_select()
    else:
        show_message("No Speakers Found")
        return

    # Wait for speaker selection before proceeding
    while not current_speaker:
        if button_a_short_pending:
            button_a_short_pending = False
            await handle_speaker_select_async('A')
        if button_x_tap_pending:
            button_x_tap_pending = False
            handle_speaker_select('X')
        if button_y_tap_pending:
            button_y_tap_pending = False
            handle_speaker_select('Y')
        await asyncio.sleep(0.01)

    # Launch background tasks
    asyncio.create_task(state_poll_task())
    asyncio.create_task(wifi_check_task())

    # Run the main button action loop
    await button_action_loop()


def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
