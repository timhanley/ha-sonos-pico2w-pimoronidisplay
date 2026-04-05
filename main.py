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
import machine
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
try:
    display.set_update_speed(3)  # Maximum SPI speed for fastest updates
except:
    pass

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
LED_GREEN_ACTIVE = 3    # Green brightness when awake (0-255)
LED_GREEN_SLEEP = 1     # Green brightness during sleep pulse (0-255)
WIFI_RESET_THRESHOLD = 300  # seconds asleep before a full CYW43 chip reset is needed
                             # (APs typically de-auth after several minutes of inactivity)
last_activity_time = 0
is_sleeping = False
sleep_start_time = 0    # time.time() when we entered sleep — used to decide reconnect strategy
_saved_ap_bssid = None  # AP BSSID saved on sleep entry for targeted fast reconnect

# Constants for album art states
ALBUM_ART_IDLE = 0
ALBUM_ART_DOWNLOADING = 1   # HTTP connection active — state poll skipped to protect CYW43
ALBUM_ART_READY = 2
ALBUM_ART_DECODING = 3      # File downloaded, decode in progress — state poll OK
jpeg = jpegdec.JPEG(display)
try:
    import pngdec
    png = pngdec.PNG(display)
except ImportError:
    pngdec = None
    png = None

current_album_art = None
current_album_art_url = None
album_art_state = ALBUM_ART_IDLE
_art_pixel_cache = None      # bytearray of 80×80 RGB565 pixels decoded from PNG, or None
_album_art_task_handle = None  # asyncio Task for the current download/decode, or None
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
force_state_poll = False      # set True after next/prev to skip sleep and force immediate poll

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

# Core 1 lifecycle flag — set True before starting thread, False to stop it
core1_running = False

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
    try:
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

        collect_garbage()
        return status_code
    finally:
        # Always close the socket — including on CancelledError — to free the
        # CYW43 socket slot.  Without this, a cancelled mid-download leaves a
        # leaked TCP connection that blocks subsequent open_connection() calls
        # for several seconds.
        writer.close()

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
# PNG thumbnail decoder — full-image scaling via streaming IDAT decode
# ---------------------------------------------------------------------------

_IDAT_TMP = '/idat.tmp'


@micropython.viper
def _png_unfilter_sub(row: ptr8, n: int, bpp: int):
    i = bpp
    while i < n:
        row[i] = (int(row[i]) + int(row[i - bpp])) & 0xFF
        i += 1


@micropython.viper
def _png_unfilter_up(row: ptr8, prev: ptr8, n: int):
    i = 0
    while i < n:
        row[i] = (int(row[i]) + int(prev[i])) & 0xFF
        i += 1


@micropython.viper
def _png_unfilter_avg(row: ptr8, prev: ptr8, n: int, bpp: int):
    i = 0
    while i < n:
        b = int(prev[i])
        if i >= bpp:
            a = int(row[i - bpp])
        else:
            a = 0
        row[i] = (int(row[i]) + ((a + b) >> 1)) & 0xFF
        i += 1


@micropython.viper
def _png_unfilter_paeth(row: ptr8, prev: ptr8, n: int, bpp: int):
    i = 0
    while i < n:
        b = int(prev[i])
        if i >= bpp:
            a = int(row[i - bpp])
            c = int(prev[i - bpp])
        else:
            a = 0
            c = 0
        p = a + b - c
        pa = p - a
        if pa < 0:
            pa = -pa
        pb = p - b
        if pb < 0:
            pb = -pb
        pc = p - c
        if pc < 0:
            pc = -pc
        if pa <= pb:
            if pa <= pc:
                pr = a
            else:
                pr = c
        else:
            if pb <= pc:
                pr = b
            else:
                pr = c
        row[i] = (int(row[i]) + pr) & 0xFF
        i += 1


@micropython.viper
def _png_accum_row(row: ptr8, acc: ptr8, out_w: int, src_w: int, bpp: int):
    """Accumulate one source row into acc using horizontal box averaging.
    acc: bytearray of out_w*6 bytes — three uint16-LE (R,G,B) per output pixel."""
    x = 0
    while x < out_w:
        sx0 = x * src_w // out_w
        sx1 = (x + 1) * src_w // out_w
        if sx1 == sx0:
            sx1 = sx0 + 1
        r_s = 0
        g_s = 0
        b_s = 0
        sx = sx0
        while sx < sx1:
            pi = sx * bpp
            r_s += int(row[pi])
            g_s += int(row[pi + 1])
            b_s += int(row[pi + 2])
            sx += 1
        cnt = sx1 - sx0
        # Average horizontally, add into accumulator (uint16-LE at acc[x*6..x*6+5])
        ai = x * 6
        r_a = (int(acc[ai]) | (int(acc[ai + 1]) << 8)) + r_s // cnt
        g_a = (int(acc[ai + 2]) | (int(acc[ai + 3]) << 8)) + g_s // cnt
        b_a = (int(acc[ai + 4]) | (int(acc[ai + 5]) << 8)) + b_s // cnt
        acc[ai]     = r_a & 0xFF
        acc[ai + 1] = (r_a >> 8) & 0xFF
        acc[ai + 2] = g_a & 0xFF
        acc[ai + 3] = (g_a >> 8) & 0xFF
        acc[ai + 4] = b_a & 0xFF
        acc[ai + 5] = (b_a >> 8) & 0xFF
        x += 1


@micropython.viper
def _png_finalize_row(acc: ptr8, out: ptr8, out_y: int, out_w: int, box_h: int):
    """Divide acc by box_h, write RGB565 (little-endian) to out row, clear acc."""
    x = 0
    base = out_y * out_w * 2
    while x < out_w:
        ai = x * 6
        r = (int(acc[ai]) | (int(acc[ai + 1]) << 8)) // box_h
        g = (int(acc[ai + 2]) | (int(acc[ai + 3]) << 8)) // box_h
        b = (int(acc[ai + 4]) | (int(acc[ai + 5]) << 8)) // box_h
        if r > 255:
            r = 255
        if g > 255:
            g = 255
        if b > 255:
            b = 255
        pixel = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        oi = base + x * 2
        out[oi]     = pixel >> 8         # high byte first — PicoGraphics framebuffer is big-endian on wire
        out[oi + 1] = pixel & 0xFF
        acc[ai]     = 0
        acc[ai + 1] = 0
        acc[ai + 2] = 0
        acc[ai + 3] = 0
        acc[ai + 4] = 0
        acc[ai + 5] = 0
        x += 1


async def png_decode_thumbnail(path, out_w=80, out_h=80):
    """Decode a PNG to an out_w×out_h RGB565 bytearray using box averaging.

    For single-IDAT PNGs (common for album art) decompresses directly from the
    source file after seeking — no temp file write needed. Falls back to
    /idat.tmp only when multiple IDAT chunks are present.
    Yields to the asyncio event loop every row so buttons stay responsive.
    Returns bytearray on success, None on failure."""
    try:
        import deflate
    except ImportError:
        print("PNG decode: deflate module not available")
        return None
    gc.collect()
    f = None
    use_tmp = False
    try:
        f = open(path, 'rb')
        if f.read(8)[:4] != b'\x89PNG':
            return None
        # Parse IHDR
        hdr = f.read(8)
        if hdr[4:8] != b'IHDR':
            return None
        ihdr = f.read(13)
        iw = (ihdr[0] << 24) | (ihdr[1] << 16) | (ihdr[2] << 8) | ihdr[3]
        ih = (ihdr[4] << 24) | (ihdr[5] << 16) | (ihdr[6] << 8) | ihdr[7]
        bit_depth = ihdr[8]
        color_type = ihdr[9]
        interlace = ihdr[12]
        f.read(4)  # IHDR CRC
        if bit_depth != 8 or interlace != 0:
            print(f"PNG: unsupported bit_depth={bit_depth} interlace={interlace}")
            return None
        if color_type == 2:
            bpp = 3
        elif color_type == 6:
            bpp = 4
        else:
            print(f"PNG: unsupported color type {color_type}")
            return None

        # Scan all chunks to find IDAT locations (reads only 8-byte headers, seeks past data)
        idat_segs = []   # list of (file_offset_of_data, data_len)
        while True:
            chdr = f.read(8)
            if len(chdr) < 8:
                break
            dlen = (chdr[0] << 24) | (chdr[1] << 16) | (chdr[2] << 8) | chdr[3]
            ctype = chdr[4:8]
            if ctype == b'IEND':
                break
            elif ctype == b'IDAT':
                idat_segs.append((f.tell(), dlen))
            f.seek(dlen + 4, 1)   # seek past data + CRC (SEEK_CUR)
        if not idat_segs:
            print("PNG: no IDAT chunks found")
            return None

        # Prepare the ZLIB source
        if len(idat_segs) == 1:
            # Single IDAT: seek back to data start and decompress in-place — no temp file
            f.seek(idat_segs[0][0])
            zlib = deflate.DeflateIO(f, deflate.ZLIB)
        else:
            # Multiple IDAT: concatenate payloads.
            # Try BytesIO (RAM) first — avoids flash write entirely.
            # Fall back to /idat.tmp if a MemoryError occurs.
            total_idat = sum(dlen for _, dlen in idat_segs)
            bio = None
            try:
                from io import BytesIO
                idat_data = bytearray(total_idat)
                idx = 0
                for seg_start, seg_len in idat_segs:
                    f.seek(seg_start)
                    remaining = seg_len
                    while remaining > 0:
                        n = min(remaining, 4096)
                        got = f.readinto(memoryview(idat_data)[idx:idx + n])
                        if not got:
                            break
                        idx += got
                        remaining -= got
                bio = BytesIO(idat_data)
                del idat_data
                gc.collect()
            except (MemoryError, ImportError):
                bio = None
            if bio is not None:
                f.close()
                f = bio
            else:
                # Flash fallback: write to /idat.tmp
                buf = bytearray(4096)
                with open(_IDAT_TMP, 'wb') as tmp:
                    for seg_start, seg_len in idat_segs:
                        f.seek(seg_start)
                        remaining = seg_len
                        while remaining > 0:
                            n = min(remaining, len(buf))
                            got = f.readinto(memoryview(buf)[:n])
                            if not got:
                                break
                            tmp.write(memoryview(buf)[:got])
                            remaining -= got
                f.close()
                f = open(_IDAT_TMP, 'rb')
                use_tmp = True
            zlib = deflate.DeflateIO(f, deflate.ZLIB)

        # Decode rows with box averaging
        row_stride = iw * bpp
        out = bytearray(out_w * out_h * 2)
        row = bytearray(row_stride)
        prev = bytearray(row_stride)
        acc = bytearray(out_w * 6)   # 3× uint16-LE per output pixel (R, G, B sums)
        out_y = 0
        row_in_box = 0
        for src_y in range(ih):
            if out_y >= out_h:
                break
            fb = zlib.read(1)
            if not fb:
                break
            filter_type = fb[0]
            offset = 0
            while offset < row_stride:
                chunk = zlib.read(row_stride - offset)
                if not chunk:
                    break
                clen = len(chunk)
                row[offset:offset + clen] = chunk
                offset += clen
            if offset < row_stride:
                break
            if filter_type == 1:
                _png_unfilter_sub(row, row_stride, bpp)
            elif filter_type == 2:
                _png_unfilter_up(row, prev, row_stride)
            elif filter_type == 3:
                _png_unfilter_avg(row, prev, row_stride, bpp)
            elif filter_type == 4:
                _png_unfilter_paeth(row, prev, row_stride, bpp)
            _png_accum_row(row, acc, out_w, iw, bpp)
            row_in_box += 1
            if src_y + 1 >= (out_y + 1) * ih // out_h or src_y + 1 >= ih:
                _png_finalize_row(acc, out, out_y, out_w, row_in_box)
                out_y += 1
                row_in_box = 0
            row, prev = prev, row
            if src_y % 5 == 4:
                await asyncio.sleep(0)  # yield every 5 rows — buttons respond within ~200ms
        if out_y < out_h:
            print(f"PNG: only decoded {out_y}/{out_h} rows")
        return out
    finally:
        # try/finally (no except) — required for await asyncio.sleep(0) to work
        # correctly in MicroPython. Exceptions propagate to album_art_task's handler.
        if f:
            f.close()
        if use_tmp:
            try:
                import os
                os.remove(_IDAT_TMP)
            except:
                pass


def _draw_art_cache(x, y, w, h):
    """Draw the PNG pixel cache to the display framebuffer.
    Cache stores pixels as [high_byte, low_byte] per pixel — big-endian wire order,
    matching both the direct framebuffer layout and the byte-swapped pen value.
    Fast path: copy rows directly via memoryview(display).
    Fallback: set_pen/pixel loop."""
    if _art_pixel_cache is None:
        return
    cache = _art_pixel_cache
    try:
        fb = memoryview(display)
        for row in range(h):
            src = row * w * 2
            dst = ((y + row) * WIDTH + x) * 2
            fb[dst:dst + w * 2] = cache[src:src + w * 2]
        return
    except TypeError:
        pass  # display doesn't support buffer protocol — use fallback
    idx = 0
    for py in range(h):
        for px in range(w):
            pen = cache[idx] | (cache[idx + 1] << 8)   # little-endian read
            display.set_pen(pen)
            display.pixel(x + px, y + py)
            idx += 2


# ---------------------------------------------------------------------------
# Album art — cancellation helper + async task
# ---------------------------------------------------------------------------

def _cancel_album_art():
    """Cancel any in-progress album art download/decode and reset state to IDLE.
    Always clears current art — safe to call in any state, including READY."""
    global _album_art_task_handle, album_art_state, current_album_art
    global current_album_art_url, _art_pixel_cache
    if _album_art_task_handle is not None:
        try:
            _album_art_task_handle.cancel()
        except:
            pass
        _album_art_task_handle = None
    album_art_state = ALBUM_ART_IDLE
    current_album_art = None
    current_album_art_url = None
    _art_pixel_cache = None


async def album_art_task(url, x, y):
    """Download album art, decode to 80×80 pixel cache (PNG) or decode JPEG directly."""
    global album_art_state, current_album_art, current_album_art_url, album_art_url
    global _art_pixel_cache, _album_art_task_handle
    album_art_state = ALBUM_ART_DOWNLOADING
    try:
        import os
        try:
            os.remove('/album_art.jpg')
        except:
            pass
        status = await async_request_to_file(url, get_ha_headers(), '/album_art.jpg')
        if status == 200:
            import os as _os
            fsize = _os.stat('/album_art.jpg')[6]
            with open('/album_art.jpg', 'rb') as f:
                magic = f.read(4)
            if magic[:4] == b'\x89PNG':
                # Switch to DECODING so state_poll_task resumes (HTTP is now free)
                album_art_state = ALBUM_ART_DECODING
                # Decode PNG to 80×80 pixel cache — full-image scaling, no crop
                cache = await png_decode_thumbnail('/album_art.jpg', 80, 80)
                if cache is None:
                    print("PNG thumbnail decode failed")
                    album_art_state = ALBUM_ART_IDLE
                    return
                _art_pixel_cache = cache
                display_lock.acquire()
                try:
                    _draw_art_cache(x, y, 80, 80)
                    current_album_art = (x, y, 80, 'png_cache')
                    current_album_art_url = url
                    album_art_state = ALBUM_ART_READY
                    display.update()
                except Exception as e:
                    print(f"Album art draw error: {e}")
                    album_art_state = ALBUM_ART_IDLE
                    current_album_art = None
                    _art_pixel_cache = None
                finally:
                    display_lock.release()
            elif magic[:2] == b'\xff\xd8':
                display_lock.acquire()
                try:
                    jpeg.open_file('/album_art.jpg')
                    # Pick scale to produce ~80px output. Use get_width() where
                    # available, fall back to file-size proxy (~400 bytes/px).
                    try:
                        src_w = jpeg.get_width()
                    except AttributeError:
                        src_w = fsize // 400
                    if src_w >= 640:
                        scale = jpegdec.JPEG_SCALE_EIGHTH
                    elif src_w >= 320:
                        scale = getattr(jpegdec, 'JPEG_SCALE_QUARTER', 2)
                    elif src_w >= 160:
                        scale = jpegdec.JPEG_SCALE_HALF
                    else:
                        scale = 0  # JPEG_SCALE_FULL — source already small
                    display.set_pen(BLACK)
                    display.rectangle(x, y, 80, 80)
                    display.set_clip(x, y, 80, 80)
                    jpeg.decode(x, y, scale)
                    display.remove_clip()
                    if album_art_state == ALBUM_ART_DOWNLOADING:  # not cancelled during decode
                        current_album_art = (x, y, scale, 'jpeg')
                        current_album_art_url = url
                        album_art_state = ALBUM_ART_READY
                        display.update()
                    else:
                        # Cancelled while jpeg.decode() was running — clear the painted area
                        display.set_pen(BLACK)
                        display.rectangle(x, y, 80, 80)
                except Exception as e:
                    print(f"Album art decode error: {e}")
                    album_art_state = ALBUM_ART_IDLE
                    current_album_art = None
                finally:
                    display_lock.release()
            else:
                print(f"Album art: unsupported format {magic}")
                album_art_state = ALBUM_ART_IDLE
        else:
            album_art_state = ALBUM_ART_IDLE
    except Exception as e:
        print(f"Error in album_art_task: {e}")
        album_art_state = ALBUM_ART_IDLE
        current_album_art = None
        _art_pixel_cache = None
    finally:
        _album_art_task_handle = None

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
    # Draw function labels — must match the active screen context
    if in_menu or in_speaker_select:
        display.text("Select", 45, HEIGHT-22, scale=1)
        display.text("Back", 160, HEIGHT-22, scale=1)
        display.text("Up", WIDTH-75, HEIGHT-22, scale=1)
        display.text("Down", WIDTH-25, HEIGHT-22, scale=1)
    elif in_brightness_screen:
        display.text("Back", 160, HEIGHT-22, scale=1)
        display.text("Up", WIDTH-75, HEIGHT-22, scale=1)
        display.text("Down", WIDTH-25, HEIGHT-22, scale=1)
    else:
        display.text("Play/Pause > Next", 45, HEIGHT-22, scale=1)
        display.text("Menu < Prev", 160, HEIGHT-22, scale=1)
        display.text("Vol+", WIDTH-75, HEIGHT-22, scale=1)
        display.text("Vol-", WIDTH-25, HEIGHT-22, scale=1)

    if force_update:
        display.update()

def draw_screen(state_data):
    """Draw the main screen with the provided state data"""
    global album_art_loading, current_state_data, album_art_state, current_album_art_url
    global current_album_name, current_album_art, _album_art_task_handle

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
                    _cancel_album_art()
                    _album_art_task_handle = asyncio.create_task(album_art_task(album_art_url, 20, 40))
                    album_art_state = ALBUM_ART_DOWNLOADING
            elif album_art_state == ALBUM_ART_IDLE and album_art_url and not current_album_art:
                # No album change but we need to load art
                _cancel_album_art()
                _album_art_task_handle = asyncio.create_task(album_art_task(album_art_url, 20, 40))
                album_art_state = ALBUM_ART_DOWNLOADING

        # Draw placeholder or current album art
        if album_art_state in (ALBUM_ART_DOWNLOADING, ALBUM_ART_DECODING):
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
                x, y, scale, fmt = current_album_art
                if fmt == 'png_cache':
                    _draw_art_cache(x, y, scale, scale)
                else:
                    jpeg.open_file('/album_art.jpg')
                    display.set_clip(x, y, 80, 80)
                    jpeg.decode(x, y, scale)
                    display.remove_clip()
            except Exception as e:
                display.remove_clip()
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

def draw_screen_smart(state_data, old_visible, new_visible):
    """Zone-based redraw: only repaint changed regions without display.clear().
    Preserves unchanged zones (e.g. album art) in the framebuffer between updates."""
    global current_state_data, album_art_state, current_album_art_url
    global current_album_name, current_album_art, _album_art_task_handle

    display_lock.acquire()
    try:
        if state_data is None:
            return

        current_state_data = state_data

        # Fall back to full clear for connectivity errors
        if not wifi_connected:
            display.set_pen(BLACK)
            display.clear()
            display.set_pen(WHITE)
            display.text("WiFi Disconnected", WIDTH//2 - 60, HEIGHT//2, scale=2)
            draw_button_labels()
            display.update()
            return
        elif not ha_connected:
            display.set_pen(BLACK)
            display.clear()
            display.set_pen(WHITE)
            display.text("Home Assistant", WIDTH//2 - 60, HEIGHT//2 - 20, scale=2)
            display.text("Unavailable", WIDTH//2 - 40, HEIGHT//2 + 10, scale=2)
            draw_button_labels()
            display.update()
            return

        char_width = 8
        text_start = 110
        available_width = WIDTH - text_start - 20
        chars_per_line = available_width // char_width

        # Status zone: state[0] or friendly_name[5] changed
        if new_visible[0] != old_visible[0] or new_visible[5] != old_visible[5]:
            display.set_pen(BLACK)
            display.rectangle(3, 3, WIDTH-6, 33)
            state = state_data.get('state', 'unknown')
            if isinstance(state, str):
                state = state[0].upper() + state[1:].lower()
            else:
                state = "Unknown"
            speaker_name = state_data.get('attributes', {}).get('friendly_name', '')
            display.set_pen(GRAY)
            display.text(f"{state} - {speaker_name}", 20, 10, scale=2)

        # Text zone: media_artist[1] or media_title[2] changed
        if new_visible[1] != old_visible[1] or new_visible[2] != old_visible[2]:
            display.set_pen(BLACK)
            display.rectangle(110, 38, WIDTH-112, 123)
            display.set_pen(WHITE)

            artist = state_data['attributes'].get('media_artist', 'Unknown Artist')
            display.text("Artist:", 110, 40, scale=1)
            if len(artist) > chars_per_line:
                space_pos = artist[:chars_per_line].rfind(' ')
                if space_pos > 0:
                    display.text(artist[:space_pos], text_start, 55, scale=2)
                    display.text(artist[space_pos + 1:], text_start, 75, scale=2)
                else:
                    display.text(artist[:chars_per_line], text_start, 55, scale=2)
                    display.text(artist[chars_per_line:], text_start, 75, scale=2)
            else:
                display.text(artist, text_start, 55, scale=2)

            title = state_data['attributes'].get('media_title', 'Unknown Track')
            display.text("Title:", 110, 95, scale=1)
            if len(title) > chars_per_line:
                space_pos = title[:chars_per_line].rfind(' ')
                if space_pos > 0:
                    display.text(title[:space_pos], text_start, 110, scale=2)
                    display.text(title[space_pos + 1:], text_start, 130, scale=2)
                else:
                    display.text(title[:chars_per_line], text_start, 110, scale=2)
                    display.text(title[chars_per_line:], text_start, 130, scale=2)
            else:
                display.text(title, text_start, 110, scale=2)

        # Album art zone: media_album_name[3] changed
        if new_visible[3] != old_visible[3]:
            new_album_name = state_data['attributes'].get('media_album_name')
            album_art_url = get_album_art(state_data)
            if new_album_name != current_album_name:
                current_album_art = None
                current_album_art_url = None
                album_art_state = ALBUM_ART_IDLE
                current_album_name = new_album_name
                # Show loading placeholder
                display.set_pen(GRAY)
                display.rectangle(20, 40, 80, 80)
                display.set_pen(WHITE)
                text = "Loading..."
                display.text(text, 20 + (80 - len(text)*6)//2 + 2, 40 + (80-8)//2, scale=1)
                if album_art_url:
                    _cancel_album_art()
                    _album_art_task_handle = asyncio.create_task(album_art_task(album_art_url, 20, 40))
                    album_art_state = ALBUM_ART_DOWNLOADING
            elif album_art_state == ALBUM_ART_IDLE and album_art_url and not current_album_art:
                _cancel_album_art()
                _album_art_task_handle = asyncio.create_task(album_art_task(album_art_url, 20, 40))
                album_art_state = ALBUM_ART_DOWNLOADING

        # Volume zone: volume_level[4] changed
        if new_visible[4] != old_visible[4]:
            display.set_pen(BLACK)
            display.rectangle(3, 161, WIDTH-6, 32)
            volume = state_data['attributes'].get('volume_level', 0)
            display.set_pen(WHITE)
            display.text(f"Volume: {int(volume * 100)}%", WIDTH//2 - 35, 167, scale=1)
            display.set_pen(GRAY)
            display.rectangle(20, 182, WIDTH-40, 10)
            display.set_pen(WHITE)
            display.rectangle(20, 182, int((WIDTH-40) * volume), 10)

        # Always redraw button labels and push frame
        draw_button_labels()
        display.update()
        collect_garbage()

    except Exception as e:
        print(f"Error in draw_screen_smart: {e}")

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

def connect_wifi(bssid=None):
    """Connect to WiFi. Pass bssid (bytes) for a faster targeted reconnect that
    skips AP scanning — only use when the chip has NOT been reset (active(False))."""
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

    if bssid:
        wlan.connect(WIFI_SSID, WIFI_PASSWORD, bssid=bssid)  # skip scan, connect direct
    else:
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

    led.set_rgb(0, LED_GREEN_ACTIVE, 0)  # Green for connected
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
    led.set_rgb(0, LED_GREEN_ACTIVE, 0)

    # Redraw the appropriate screen
    if in_speaker_select:
        draw_speaker_select()
    elif in_brightness_screen:  # Add brightness screen check
        draw_brightness_screen()
    elif in_menu:
        draw_menu()
    else:
        # Draw cached state immediately — no network calls here.
        # asyncio.open_connection() can block the MicroPython interpreter at the
        # C/CYW43 level if the WiFi stack is still recovering, making wait_for()
        # ineffective and freezing the device. state_poll_task will fetch fresh
        # state within state_update_interval (1 s) once asyncio is running.
        if current_state_data:
            draw_screen(current_state_data)
        else:
            show_loading_screen("Waking...")

# ---------------------------------------------------------------------------
# Unchanged helper functions
# ---------------------------------------------------------------------------

def pulse_led():
    """Simple blink in sleep mode — 1s on, 1s off cycle"""
    if (time.ticks_ms() // 1000) % 2 == 0:
        led.set_rgb(0, LED_GREEN_SLEEP, 0)
    else:
        led.set_rgb(0, 0, 0)

def enter_sleep_mode():
    """Enter low power sleep mode"""
    global is_sleeping, sleep_start_time, _saved_ap_bssid
    is_sleeping = True
    sleep_start_time = time.time()
    # Save the AP BSSID so we can reconnect directly (skip scanning) on short wakes.
    try:
        _saved_ap_bssid = network.WLAN(network.STA_IF).config('bssid')
    except:
        _saved_ap_bssid = None
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
    """Get album art URL from state data."""
    try:
        entity_picture = state_data['attributes'].get('entity_picture')
        if entity_picture:
            return f"{HA_URL}{entity_picture}" if entity_picture.startswith('/') else entity_picture
    except:
        pass
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

        draw_button_labels()
        display.update()
    finally:
        display_lock.release()


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

        draw_button_labels()
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
    global core1_running

    a_was_held = False
    b_was_held = False
    x_was_held = False
    y_was_held = False
    # ms-precision timestamps for long press duration — avoids integer time.time()
    # truncation causing false long presses when buttons are pressed twice quickly.
    a_press_start_ms = 0
    b_press_start_ms = 0
    long_press_ms = int(LONG_PRESS_TIME * 1000)

    while core1_running:
        current_time = time.time()
        any_changed = False

        # --- Button A ---
        if button_a.value() == 0:
            any_button_pressed = True
            if not a_was_held:
                a_was_held = True
                button_a_press_start = 1  # sentinel: held (>0), actual timing via a_press_start_ms
                a_press_start_ms = time.ticks_ms()
                button_a_pressed_time = current_time
                last_activity_time = current_time
                any_changed = True
            elif button_a_press_start > 0 and time.ticks_diff(time.ticks_ms(), a_press_start_ms) >= long_press_ms:
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
                button_b_press_start = 1  # sentinel: held (>0), actual timing via b_press_start_ms
                b_press_start_ms = time.ticks_ms()
                button_b_pressed_time = current_time
                last_activity_time = current_time
                any_changed = True
            elif button_b_press_start > 0 and time.ticks_diff(time.ticks_ms(), b_press_start_ms) >= long_press_ms:
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

def visible_state(state_data):
    """Extract only the fields that affect what's drawn on screen.
    Used to avoid redrawing (and re-decoding JPEG) when only media_position changes."""
    if not state_data:
        return None
    a = state_data.get('attributes', {})
    return (
        state_data.get('state'),
        a.get('media_artist'),
        a.get('media_title'),
        a.get('media_album_name'),
        a.get('volume_level'),
        a.get('friendly_name'),
    )

async def state_poll_task():
    global last_state_update, current_state_data, force_state_poll
    prev_visible = None
    elapsed = 0.0
    while True:
        await asyncio.sleep(0.1)
        elapsed += 0.1
        if not force_state_poll and elapsed < state_update_interval:
            continue
        force_state_poll = False  # clear flag; poll fires immediately, no stale-state side effects
        elapsed = 0.0
        # Not on main screen — reset so next entry gets a full redraw
        if is_sleeping or in_menu or in_speaker_select or in_brightness_screen:
            prev_visible = None
            continue
        # Album art is downloading/decoding — skip this poll to avoid a second
        # concurrent HTTP connection overwhelming the CYW43 WiFi chip.
        if album_art_state == ALBUM_ART_DOWNLOADING:
            continue
        new_state = await get_sonos_state_async()
        if new_state:
            new_visible = visible_state(new_state)
            old_visible = visible_state(current_state_data)
            if prev_visible is None:
                # First draw after entering main screen — full redraw
                current_state_data = new_state
                draw_screen(current_state_data)
                prev_visible = new_visible
            elif new_visible != old_visible:
                # Something visible changed — zone-based update
                current_state_data = new_state
                draw_screen_smart(current_state_data, prev_visible, new_visible)
                prev_visible = new_visible
            else:
                current_state_data = new_state  # keep state fresh, no redraw
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
    global any_button_pressed, is_sleeping, last_activity_time, core1_running
    global force_state_poll

    while True:
        current_time = time.time()

        # Handle sleep mode
        if is_sleeping:
            # Stop Core 1 so it doesn't conflict with lightsleep
            core1_running = False
            time.sleep(0.02)  # give Core 1 time to exit its 5ms polling loop

            # Apply low power settings
            wlan = network.WLAN(network.STA_IF)
            wlan.config(pm=0xa11142)  # standard WiFi power save (aggressive mode
                                      # causes AP de-auth after long sleeps)
            original_freq = machine.freq()
            machine.freq(48_000_000)  # reduce CPU from 150MHz to 48MHz

            # Sleep loop — blocks asyncio intentionally, nothing to do while asleep
            # time.sleep_ms used instead of machine.lightsleep: lightsleep pauses
            # the PWM timer that drives the RGB LED, causing erratic flickering.
            # Power saving from 48MHz CPU + aggressive WiFi PM is still active.
            # Poll every 50ms (not 200ms) so brief taps are reliably detected.
            _wake_poll = 0
            while is_sleeping:
                time.sleep_ms(50)
                _wake_poll += 1
                if _wake_poll % 4 == 0:   # pulse LED at the same 200ms cadence
                    pulse_led()
                if (button_a.value() == 0 or button_b.value() == 0 or
                        button_x.value() == 0 or button_y.value() == 0):
                    break  # wake on any button press

            # Restore power settings
            machine.freq(original_freq)
            wlan.config(pm=0xa11142)  # restore default WiFi power save

            # Turn the screen and LED on immediately so the user gets visual
            # feedback at once — before any (potentially slow) network activity.
            display.set_backlight(current_brightness)
            led.set_rgb(0, LED_GREEN_ACTIVE, 0)

            # Wait for the wake button to be fully released before restarting
            # Core 1 — otherwise Core 1 catches the release and fires the action.
            while (button_a.value() == 0 or button_b.value() == 0 or
                    button_x.value() == 0 or button_y.value() == 0):
                time.sleep_ms(5)
            time.sleep_ms(50)  # debounce

            # Discard any button actions triggered by the wake press
            button_a_short_pending = False
            button_a_long_pending = False
            button_b_short_pending = False
            button_b_long_pending = False
            button_x_tap_pending = False
            button_y_tap_pending = False
            any_button_pressed = False

            # Choose reconnect strategy based on how long we were asleep.
            # APs de-auth idle devices after several minutes; the CYW43 driver
            # keeps reporting isconnected()==True regardless, so we cannot trust it.
            # Strategy:
            #   short sleep (< WIFI_RESET_THRESHOLD): skip chip reset — just
            #     reconnect directly to the saved BSSID (skips AP scan, ~1-2 s
            #     faster). If already still connected, skip entirely.
            #   long sleep (>= WIFI_RESET_THRESHOLD): full chip reset to clear
            #     stale TCP socket state, then normal reconnect (~2-3 s).
            sleep_duration = time.time() - sleep_start_time
            if sleep_duration >= WIFI_RESET_THRESHOLD:
                show_loading_screen("Reconnecting WiFi...")
                wlan.active(False)
                time.sleep_ms(500)
                connect_wifi()  # full reset — don't pass bssid, chip state was wiped
                time.sleep_ms(500)  # let the TCP stack stabilise
            elif not wlan.isconnected():
                show_loading_screen("Reconnecting WiFi...")
                connect_wifi(bssid=_saved_ap_bssid)  # fast: target AP directly, skip scan
                time.sleep_ms(200)  # brief settle
            # else: still connected (short sleep) — no reconnect needed at all

            # Restart Core 1
            core1_running = True
            _thread.start_new_thread(button_core, ())
            await asyncio.sleep(0.05)  # give Core 1 time to start

            # Wake display and redraw
            await wake_device_async()
            continue

        # Check sleep timeout — suppress while album art is loading so the decode
        # isn't interrupted and left invisible behind a dark screen.
        if (current_time - last_activity_time >= SLEEP_TIMEOUT and
                album_art_state not in (ALBUM_ART_DOWNLOADING, ALBUM_ART_DECODING)):
            enter_sleep_mode()
            await asyncio.sleep(0.1)
            continue

        # Skip GC during album art download/decode — collect_garbage() takes ~40ms
        # and runs on every asyncio yield, adding ~16s to a 400-row decode.
        if album_art_state not in (ALBUM_ART_DOWNLOADING, ALBUM_ART_DECODING):
            collect_garbage()

        # Button B: menu/back (short) or previous track (long, main screen only)
        if button_b_long_pending and not in_menu and not in_speaker_select and not in_brightness_screen:
            button_b_long_pending = False
            update_activity()
            _cancel_album_art()
            await call_ha_service_async("media_previous_track", {"entity_id": current_speaker})
            force_state_poll = True
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
                _cancel_album_art()
                await call_ha_service_async("media_next_track", {"entity_id": current_speaker})
                force_state_poll = True
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
    global any_button_pressed, core1_running
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
        led.set_rgb(0, LED_GREEN_ACTIVE, 0)  # Green if already connected
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
    core1_running = True
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
