# Hardware singletons: display, pens, RGB LED, buttons, and the dual-core
# display lock.
#
# INVARIANT: display_lock must be held around every framebuffer-build →
# display.update() sequence, not just the update() call. Core 1 writing
# button-label pixels mid-draw otherwise causes white bars and tearing.
import _thread

from machine import Pin
from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY_2, PEN_RGB565
from pimoroni import RGBLED

display = PicoGraphics(display=DISPLAY_PICO_DISPLAY_2, pen_type=PEN_RGB565)
display.set_font("bitmap8")
WIDTH, HEIGHT = display.get_bounds()
try:
    display.set_update_speed(3)  # maximum SPI speed
except (AttributeError, ValueError):
    pass

led = RGBLED(26, 27, 28)  # Pimoroni Pico Display 2.8" LED pins

button_a = Pin(12, Pin.IN, Pin.PULL_UP)  # Play/Pause / Select
button_b = Pin(13, Pin.IN, Pin.PULL_UP)  # Menu / Back
button_x = Pin(14, Pin.IN, Pin.PULL_UP)  # Volume Up / Menu Up
button_y = Pin(15, Pin.IN, Pin.PULL_UP)  # Volume Down / Menu Down

# Pens are created once — create_pen() allocates, so never call it in a draw path.
WHITE = display.create_pen(255, 255, 255)
BLACK = display.create_pen(0, 0, 0)
GRAY = display.create_pen(128, 128, 128)
GREEN = display.create_pen(0, 128, 0)   # short-press feedback blob
BLUE = display.create_pen(0, 64, 192)   # long-press feedback blob

display_lock = _thread.allocate_lock()

# LED brightness levels (0-255)
LED_GREEN_ACTIVE = 3   # awake
LED_GREEN_SLEEP = 1    # sleep pulse


def led_active():
    led.set_rgb(0, LED_GREEN_ACTIVE, 0)


def led_sleep_pulse():
    led.set_rgb(0, LED_GREEN_SLEEP, 0)


def led_off():
    led.set_rgb(0, 0, 0)


def led_error():
    led.set_rgb(16, 0, 0)


def led_connecting():
    led.set_rgb(16, 8, 0)
