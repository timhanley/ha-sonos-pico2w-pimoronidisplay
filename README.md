# Home Assistant Sonos Display Controller / Remote - Raspberry Pi Pico 2 W

A MicroPython application for controlling Sonos speakers via Home Assistant using a Pimoroni Pico Display Pack 2.

![Main Interface](screenshots/main.jpg)

## Features

- WiFi connectivity status
- Speaker selection
- Play/Pause control
- Volume control
- Album art display
- Brightness control
- Sleep mode with wake on button press

## Screenshots

### Main Playback Screen
![Main Playback](screenshots/main.jpg)

### Menu
![Menu](screenshots/menu.jpg)

### Speaker Selection
![Speaker Selection](screenshots/speakers.jpg)

### Brightness Control
![Brightness](screenshots/brightness.jpg)

## Hardware Requirements

- Raspberry Pi Pico W
- Pimoroni Pico Display Pack 2

## Setup

1. Copy main.py and the LICENCE file to your Pico W which needs to be running the Pimoroni micropython https://github.com/pimoroni/pimoroni-pico-rp2350/releases or have the pico display pack libraries installed on vanilla micropython.   
2. Update the WiFi and Home Assistant configuration in main.py
3. Ensure Home Assistant has the Sonos integration installed and properly configured
4. Reset the Pico W to start the application

## Usage

- Button A: Play/Pause
- Button B: Menu
- Button X: Volume Up / Menu Up
- Button Y: Volume Down / Menu Down

## License

This project is licensed under a custom non-commercial license. See the LICENSE file for details.
Key points:
- Free for non-commercial use
- Commercial use requires explicit permission
- Attribution required when sharing or modifying
- No warranty provided

## Author

Tim Hanley