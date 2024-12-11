# Home Assistant Sonos Display Controller / Remote - Raspberry Pi Pico 2 W
# Copyright 2024 Tim Hanley - see LICENCE.txt for details
# https://github.com/timhanley/ha-sonos-pico2w-pimoronidisplay
# v1.2


import gc  # Add garbage collector
import network
import urequests
import json
import time
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

# WiFi and Home Assistant Configuration
#WIFI_SSID = "YOUR_WIFI_SSD"
#WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
#HA_URL = "http://YOUR_HOME_ASSISTANT_IP ADDRESS:8123"
#HA_TOKEN = "YOUR_HOME_ASSISTANT_LONG_LIVED_TOKEN"

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
state_update_interval = 0.2  # Reduce from 1 to 0.2 seconds for snappier updates

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
LONG_PRESS_TIME = 0.6  # Reduce from 0.8 to 0.6 seconds to make it easier to control
button_a_press_start = 0  # Track when button A was first pressed
button_b_press_start = 0  # Track when button B was first pressed

def collect_garbage():
    gc.collect()
    gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())

def get_ha_headers():
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

def get_sonos_state():
    global ha_connected
    if not check_wifi_connection() or not current_speaker:
        return None
        
    try:
        collect_garbage()
        response = urequests.get(
            f"{HA_URL}/api/states/{current_speaker}",
            headers=get_ha_headers()
        )
        data = response.json()
        response.close()
        ha_connected = True
        return data
    except Exception as e:
        ha_connected = False
        return None
    finally:
        collect_garbage()

def call_ha_service(service, data):
    global ha_connected
    if not check_wifi_connection():
        return False
        
    retries = 3
    while retries > 0:
        try:
            collect_garbage()
            response = urequests.post(
                f"{HA_URL}/api/services/media_player/{service}",
                headers=get_ha_headers(),
                json=data
            )
            
            if response.status_code == 200:
                response.close()
                ha_connected = True
                return True
            else:
                response.close()
                # Show error on screen
                display.set_pen(BLACK)
                display.clear()
                
                # Draw centered text
                display.set_pen(WHITE)
                error_text = "HA Connection Error"
                text_width = len(error_text) * 8  # Assuming 8 pixels per character
                text_x = (WIDTH - text_width) // 2
                text_y = HEIGHT // 2  # Center vertically
                display.text(error_text, text_x, text_y, scale=1)
                display.update()
                time.sleep(1)
                
            retries -= 1
            if retries > 0:
                time.sleep(1)
                
        except Exception as e:
            # Show error on screen
            display.set_pen(BLACK)
            display.clear()
            
            # Draw centered text
            display.set_pen(WHITE)
            error_text = "HA Connection Error"
            text_width = len(error_text) * 8  # Assuming 8 pixels per character
            text_x = (WIDTH - text_width) // 2
            text_y = HEIGHT // 2  # Center vertically
            display.text(error_text, text_x, text_y, scale=1)
            display.update()
            time.sleep(1)
            
            retries -= 1
            if retries > 0:
                time.sleep(1)
        finally:
            collect_garbage()
    
    ha_connected = False
    return False

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
    
    # Create green pen for active buttons
    GREEN = display.create_pen(0, 128, 0)
    
    # Draw button circles with feedback
    button_positions = [
        ("A", a_active, 30),
        ("B", b_active, 145),  # Moved B button further right from 135
        ("X", x_active, WIDTH-90),
        ("Y", y_active, WIDTH-35)
    ]
    
    # Draw circles and centered labels
    for button, active, x_pos in button_positions:
        display.set_pen(GREEN if active else GRAY)
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
                
                # If we have a new URL, start the download
                if album_art_url:
                    load_album_art(album_art_url, 20, 40)
            elif album_art_state == ALBUM_ART_IDLE and album_art_url and not current_album_art:
                # No album change but we need to load art
                load_album_art(album_art_url, 20, 40)
        
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
        print(f"Error drawing state data: {e}")
        print(f"Error occurred at line: {e.__traceback__.tb_lineno}")  # Debug line number
        display.text("Display Error", 10, HEIGHT//2, scale=1)
        
        # Draw button labels
        draw_button_labels()
        
        display.update()
        collect_garbage()
        
    except Exception as e:
        print(f"Critical error in draw_screen: {e}")
        print(f"Error occurred at line: {e.__traceback__.tb_lineno}")  # Debug line number

def show_message(message, scale=2):
    """Helper function to show centered messages"""
    display.set_pen(BLACK)
    display.clear()
    display.set_pen(WHITE)
    
    # Calculate center position using 6 pixels per character instead of 8
    x = WIDTH//2 - (len(message) * 6 * scale)//2
    y = HEIGHT//2 - (8 * scale)
    
    display.text(message, x, y, scale=scale)
    display.update()

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

def handle_menu_navigation(button_pressed):
    """Handle menu navigation"""
    global current_menu_index, in_menu, in_speaker_select, current_state_data, in_brightness_screen, button_b_pressed_time  # Add button_b_pressed_time
    
    if button_pressed == 'X' or button_pressed == 'Y':  # Up/Down
        current_menu_index = (current_menu_index - 1 if button_pressed == 'X' else current_menu_index + 1) % len(MENU_ITEMS)
        draw_menu()
    elif button_pressed == 'A':  # Select
        if MENU_ITEMS[current_menu_index] == "Select Speaker":
            # Show loading screen before fetching speakers
            show_loading_screen("Loading Speakers...")
            if get_available_speakers():
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
            new_state = get_sonos_state()
            if new_state:
                current_state_data = new_state
                draw_screen(new_state)
    elif button_pressed == 'B':  # Back
        in_menu = False
        button_b_pressed_time = 0  # Reset button feedback
        new_state = get_sonos_state()
        if new_state:
            current_state_data = new_state
            draw_screen(new_state)

def pulse_led():
    """Simple blink in sleep mode"""
    current_time = time.time()
    # Blink every second
    if int(current_time) % 2 == 0:
        led.set_rgb(0, 16, 0)  # Dim green on
    else:
        led.set_rgb(0, 0, 0)   # LED off

def wake_device():
    """Wake the device from sleep mode"""
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
        new_state = get_sonos_state()
        if new_state:
            current_state_data = new_state
            draw_screen(new_state)

def enter_sleep_mode():
    """Enter low power sleep mode"""
    global is_sleeping
    is_sleeping = True
    display.set_backlight(0)
    # Clear any button feedback
    draw_button_labels(True)

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

def load_album_art(url, x, y):
    """Start album art download"""
    global album_art_state, album_art_response, album_art_url
    
    try:
        # Clean up any existing download
        if album_art_response:
            album_art_response.close()
        
        # Remove existing art file
        try:
            import os
            os.remove('album_art.jpg')
        except:
            pass
            
        # Start new download
        album_art_response = urequests.get(url, stream=True)
        album_art_url = url
        album_art_state = ALBUM_ART_DOWNLOADING
        
    except Exception as e:
        print(f"Error loading album art: {e}")
        album_art_state = ALBUM_ART_IDLE

def process_album_art(x, y):
    """Process album art download"""
    global album_art_state, album_art_response, current_album_art, current_album_art_url, album_art_url
    global current_album_name
    
    try:
        if album_art_response and album_art_state == ALBUM_ART_DOWNLOADING:
            # Read a larger chunk
            chunk = album_art_response.raw.read(4096)  # Increased chunk size
            
            if chunk:
                # Append to file if there's data
                with open('album_art.jpg', 'ab') as f:
                    f.write(chunk)
                return  # Exit and continue in next loop iteration
            else:
                # No more data - we're done downloading
                album_art_response.close()
                album_art_response = None
                
                # Decode and display
                jpeg.open_file('album_art.jpg')
                jpeg.decode(x, y, jpegdec.JPEG_SCALE_EIGHTH)
                current_album_art = (x, y, jpegdec.JPEG_SCALE_EIGHTH)
                current_album_art_url = album_art_url
                album_art_state = ALBUM_ART_READY
                
    except Exception as e:
        print(f"Error processing album art: {e}")
        album_art_state = ALBUM_ART_IDLE
        if album_art_response:
            album_art_response.close()
            album_art_response = None

def show_loading_screen(message="Loading..."):
    """Show a loading screen with a message"""
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

def get_available_speakers():
    """Fetch all Sonos speakers from Home Assistant using template"""
    global available_speakers
    
    # Show loading screen
    show_loading_screen("Loading Speakers...")
    
    # Check WiFi connection first
    if not check_wifi_connection():
        show_loading_screen("WiFi Not Connected")
        time.sleep(2)
        return False
    
    try:
        # Try to ping HA first
        try:
            response = urequests.get(f"{HA_URL}/api", headers=get_ha_headers())
            response.close()
        except Exception as e:
            print(f"Cannot Reach Home Assistant: {e}")
            # Show error using same style as other errors
            display.set_pen(BLACK)
            display.clear()
            display.set_pen(WHITE)
            error_text = "Cannot Reach Home Assistant"
            text_width = len(error_text) * 9
            text_x = (WIDTH - text_width) // 2
            text_y = HEIGHT // 2
            display.text(error_text, text_x, text_y, scale=2)
            display.update()
            time.sleep(2)
            return False
        
        # Template to get Sonos devices and their entities
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
        
        # Make template API request
        response = urequests.post(
            f"{HA_URL}/api/template",
            headers=get_ha_headers(),
            json={"template": template}
        )
        
        result = response.json()  # Parse JSON response
        response.close()
        collect_garbage()
        
        # Process results
        speakers = []
        for device in result:
            # Find the media_player entity for this device
            for entity in device['entities']:
                if entity.startswith('media_player.'):
                    speakers.append({
                        'entity_id': entity,
                        'name': device['device_name']
                    })
                    print(f"Found speaker: {device['device_name']} ({entity})")  # Debug
                    break  # Only need one media_player entity per device
        
        if speakers:
            available_speakers = speakers
            print(f"Total Sonos speakers found: {len(speakers)}")  # Debug
            return True
        
        print("No Sonos speakers found")  # Debug
        show_loading_screen("No Sonos\nSpeakers Found")
        time.sleep(2)
        return False
        
    except Exception as e:
        print(f"Error fetching speakers: {e}")
        print(f"Error type: {type(e)}")
        show_loading_screen("Error Loading\nSpeakers\n" + str(e))
        time.sleep(2)
        return False
    finally:
        collect_garbage()

def draw_speaker_select():
    """Draw the speaker selection interface"""
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
    """Handle speaker selection navigation and selection"""
    global current_speaker_index, in_speaker_select, in_menu, current_speaker
    
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
            new_state = get_sonos_state()
            if new_state:
                current_state_data = new_state
                draw_screen(current_state_data)
    elif button_pressed == 'B':  # Back
        in_speaker_select = False
        draw_menu()

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

def main():
    global current_speaker, in_speaker_select, is_sleeping, album_art_response
    global last_button_a_press, last_button_x_press, last_button_y_press
    global button_a_pressed_time, button_x_pressed_time, button_y_pressed_time
    global current_menu_index, in_menu, last_activity_time
    global current_state_data, last_state_update, last_wifi_check
    global button_b_pressed_time, album_art_loading, album_art_state
    global current_album_art, current_album_art_url
    global state_data, in_brightness_screen, button_a_press_start
    global button_b_press_start

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
    
    # Only proceed with speaker selection if WiFi is connected
    if get_available_speakers():
        in_speaker_select = True
        draw_speaker_select()
    else:
        show_message("No Speakers Found")
        return
    
    # Wait for speaker selection before proceeding
    while not current_speaker:
        # Handle speaker selection buttons
        if button_a.value() == 0:  # Select
            handle_speaker_select('A')
            time.sleep(0.2)
        if button_x.value() == 0:  # Up
            handle_speaker_select('X')
            time.sleep(0.2)
        if button_y.value() == 0:  # Down
            handle_speaker_select('Y')
            time.sleep(0.2)
        time.sleep(0.01)
    
    try:
        while True:
            current_time = time.time()
            
            # Handle sleep mode first
            if is_sleeping:
                pulse_led()
                # Check for any button press to wake
                if (button_a.value() == 0 or button_b.value() == 0 or 
                    button_x.value() == 0 or button_y.value() == 0):
                    wake_device()
                    # Get fresh state immediately when waking
                    if not in_menu:
                        new_state = get_sonos_state()
                        if new_state:
                            current_state_data = new_state
                            draw_screen(new_state)
                    time.sleep(0.2)  # Debounce
                time.sleep(0.1)  # Longer sleep while in sleep mode
                continue  # Skip all other processing while sleeping
            
            # Only do these operations when awake
            if current_time - last_activity_time >= SLEEP_TIMEOUT:
                enter_sleep_mode()
                continue
            
            # Process album art only when awake and not processing buttons
            if album_art_state == ALBUM_ART_DOWNLOADING and not (
                button_a.value() == 0 or 
                button_b.value() == 0 or 
                button_x.value() == 0 or 
                button_y.value() == 0
            ):
                process_album_art(20, 60)
            
            # Normal screen updates when awake
            if current_time - last_state_update >= state_update_interval:
                if not in_menu and not in_speaker_select and not in_brightness_screen:
                    new_state = get_sonos_state()
                    if new_state:
                        # Only update if state has changed
                        if new_state != current_state_data:
                            current_state_data = new_state
                            draw_screen(current_state_data)
                last_state_update = current_time
            
            # Normal operation mode
            if current_time - last_wifi_check >= wifi_check_interval:
                check_wifi_connection()
                last_wifi_check = current_time
            
            # Add before button handling section
            collect_garbage()  # Free up memory before processing buttons
            
            # Handle button presses
            if button_b.value() == 0:  # Menu/Previous
                update_activity()
                if not in_menu and not in_speaker_select and not in_brightness_screen:
                    # In main screen - handle long press for previous track
                    if button_b_press_start == 0:  # Button just pressed
                        button_b_press_start = current_time
                        button_b_pressed_time = current_time  # Set initial press time
                        draw_button_labels(True)  # Show feedback immediately
                    elif current_time - button_b_press_start >= LONG_PRESS_TIME and button_b_press_start > 0:
                        # Long press detected - previous track (only trigger once)
                        button_b_press_start = -1  # Set to -1 to prevent menu on release and prevent multiple triggers
                        button_b_pressed_time = current_time  # Keep feedback active
                        if call_ha_service("media_previous_track", {"entity_id": current_speaker}):
                            time.sleep(0.05)
                        draw_button_labels(True)
                elif button_b_press_start == 0:  # First press in menu/other screens
                    button_b_press_start = current_time
                    button_b_pressed_time = current_time  # Set initial press time
                    draw_button_labels(True)  # Show feedback immediately
                time.sleep(0.1)
            elif button_b.value() == 1 and button_b_press_start != 0:  # Button released and was pressed
                if button_b_press_start > 0:  # Was a short press
                    if not in_menu and not in_speaker_select and not in_brightness_screen:
                        button_b_pressed_time = current_time  # Set feedback for transition
                        draw_button_labels(True)  # Show feedback before transition
                        time.sleep(BUTTON_FEEDBACK_TIME)  # Wait for feedback to show
                        in_menu = True
                        current_menu_index = 0
                        button_b_pressed_time = 0  # Clear feedback before drawing menu
                        draw_menu()
                    elif in_speaker_select:
                        button_b_pressed_time = 0  # Reset feedback
                        in_speaker_select = False
                        draw_menu()
                    elif in_brightness_screen:
                        button_b_pressed_time = 0  # Reset feedback
                        in_brightness_screen = False
                        draw_menu()
                    else:
                        button_b_pressed_time = 0  # Reset feedback
                        in_menu = False
                        new_state = get_sonos_state()
                        if new_state:
                            current_state_data = new_state
                            draw_screen(new_state)
                button_b_press_start = 0  # Reset press start in all cases
            
            # Handle other buttons based on current screen
            if in_speaker_select:
                if button_a.value() == 0:  # Select
                    update_activity()
                    handle_speaker_select('A')
                    time.sleep(0.2)
                if button_x.value() == 0:  # Up
                    update_activity()
                    handle_speaker_select('X')
                    time.sleep(0.2)
                if button_y.value() == 0:  # Down
                    update_activity()
                    handle_speaker_select('Y')
                    time.sleep(0.2)
            elif in_brightness_screen:
                if button_x.value() == 0:  # Up
                    update_activity()
                    handle_brightness_control('X')
                    time.sleep(0.2)
                if button_y.value() == 0:  # Down
                    update_activity()
                    handle_brightness_control('Y')
                    time.sleep(0.2)
            elif in_menu:  # Regular menu controls
                if button_a.value() == 0:  # Select
                    update_activity()
                    handle_menu_navigation('A')
                    time.sleep(0.2)
                if button_x.value() == 0:  # Up
                    update_activity()
                    handle_menu_navigation('X')
                    time.sleep(0.2)
                if button_y.value() == 0:  # Down
                    update_activity()
                    handle_menu_navigation('Y')
                    time.sleep(0.2)
            else:  # Main playback screen
                if button_a.value() == 0:  # Play/Pause or Next Track
                    update_activity()
                    if button_a_press_start == 0:  # Button just pressed
                        button_a_press_start = current_time
                        button_a_pressed_time = current_time  # Set initial press time
                        draw_button_labels(True)
                    elif current_time - button_a_press_start >= LONG_PRESS_TIME and button_a_press_start > 0:
                        # Long press detected - skip track (only trigger once)
                        button_a_press_start = -1  # Set to -1 to prevent play/pause on release
                        button_a_pressed_time = current_time
                        draw_button_labels(True)
                        if call_ha_service("media_next_track", {"entity_id": current_speaker}):
                            time.sleep(0.02)
                    time.sleep(0.05)
                elif button_a.value() == 1:  # Button released
                    if button_a_press_start > 0:  # Was a short press
                        # Short press - play/pause
                        button_a_pressed_time = current_time
                        draw_button_labels(True)
                        if call_ha_service("media_play_pause", {"entity_id": current_speaker}):
                            time.sleep(0.05)
                    button_a_press_start = 0  # Reset press start in all cases
                elif button_a_press_start == -1:  # Button released after long press
                    button_a_press_start = 0  # Just reset the press start
                
                if button_x.value() == 0:  # Volume Up
                    update_activity()
                    button_x_pressed_time = current_time
                    draw_button_labels(True)
                    if call_ha_service("volume_up", {"entity_id": current_speaker}):
                        time.sleep(0.02)
                
                if button_y.value() == 0:  # Volume Down
                    update_activity()
                    button_y_pressed_time = current_time
                    draw_button_labels(True)
                    if call_ha_service("volume_down", {"entity_id": current_speaker}):
                        time.sleep(0.02)
            
            time.sleep(0.005)  # Main loop sleep 0.005
    finally:
        # Cleanup
        if album_art_response:
            album_art_response.close()
        try:
            import os
            os.remove('album_art.jpg')
        except:
            pass

if __name__ == "__main__":
    main()