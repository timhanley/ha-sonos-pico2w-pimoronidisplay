# v2.0.0 Hardware Test Plan

The v2 rewrite (commits `4fe6afd`…`bce5e9a`) has been syntax-checked, linted,
and unit-tested, but has **never run on the device**. Work through this
checklist on the Pico before trusting it or pushing/tagging the release.

Deploy first:

```
make deploy                      # main.py + app/  (close Thonny first)
mpremote cp config.py :config.py # only if config.py is not already on the device
make console                     # watch boot output
```

Keep the console open for the whole session — any `ERROR:` lines or
tracebacks are findings even when the screen looks right.

## 1. Boot

- [ ] Cold boot: LED red → orange → green; "Connecting to WiFi..." then
      "Loading Speakers..." appears
- [ ] Speaker list appears with correct names; X/Y move the highlight
      (buttons must work here — this exercises the Core 1 start-order invariant)
- [ ] Holding X or Y auto-repeats through the list at ~5 steps/second
- [ ] A selects; playback screen appears with state, artist/title, volume

## 2. Playback screen

- [ ] Status line shows "Playing - <speaker>" (or Paused/Idle)
- [ ] A short press toggles play/pause; screen updates within ~1 s
- [ ] A long press (≥1 s): blue blob on A, next track, screen updates promptly
      (force_poll — should feel faster than the 1 s poll)
- [ ] B long press: previous track
- [ ] X/Y single taps change volume by one step; bar and % update
- [ ] Holding X/Y repeats volume at ~3 steps/second (v1 had a bug making this
      ~1/s — v2 should feel noticeably faster)
- [ ] Long artist/title names wrap to two lines, no overflow into the border
- [ ] Green feedback blob appears instantly on every press (Core 1 alive)

## 3. Album art (test BOTH formats)

- [ ] JPEG source (e.g. Spotify): "Loading..." placeholder → art appears
- [ ] PNG source (e.g. Apple Music/Tidal/Amazon): art appears; buttons stay
      responsive during the multi-second decode
- [ ] Track change within the same album: no art flicker/re-download
- [ ] Album change: placeholder then new art
- [ ] Track with no artwork: "No Art" placeholder (v1 could show a stuck
      "Loading...")
- [ ] Open the menu while art is downloading, wait ~10 s, exit menu: art
      appears on the playback screen and NEVER paints over the menu (new
      FILE_READY deferral path — regression risk, test deliberately)
- [ ] After art is shown, enter and exit the menu: art redraws instantly
      (memcpy cache path — no flash re-read, no visible decode pause)

## 4. Menu / navigation

- [ ] B opens the menu with "Select Speaker" highlighted (top item — v1 bug
      fixed; reopen after moving the highlight and confirm it resets)
- [ ] Select Speaker → list loads → choosing another speaker switches state
- [ ] Brightness: X/Y adjust in 5% steps, floor 25%, ceiling 100%; backlight
      changes live; setting survives a reboot (brightness.json)
- [ ] B backs out: speaker-select → menu, brightness → menu, menu → playback
- [ ] Button captions match each screen (Select/Back/Up/Down vs playback set)

## 5. Sleep tiers

Screen sleep (default 60 s idle — temporarily set `SCREEN_SLEEP_TIMEOUT = 20`
in config.py for faster testing):

- [ ] Screen blanks; LED blinks green at 1 s cadence
- [ ] Change track from another Sonos controller while asleep, then press a
      button: screen wakes INSTANTLY showing the new track (poll continued)
- [ ] The wake press is discarded (does not toggle play/pause)

Deep sleep (set `DEEP_SLEEP_TIMEOUT = 60` temporarily):

- [ ] LED cadence slows to 2 s blink; console shows "deep sleep: WiFi off"
- [ ] Press a button: backlight on immediately with cached state, then
      "Connecting to WiFi..." (~3-5 s), then fresh state within ~1 s
- [ ] Wake press discarded; buttons work normally after wake (Core 1
      restarted); volume/track controls respond
- [ ] Let it deep-sleep OVERNIGHT, then wake: must reconnect cleanly (this is
      the CYW43 "F2 not ready" stall scenario the chip-off design prevents)
- [ ] Restore real timeout values in config.py afterwards

## 6. Failure modes

- [ ] Stop HA (or firewall it): within ~3 s of polling, "Home Assistant
      Unavailable" appears once (no flashing); restart HA: playback screen
      returns within a few polls
- [ ] Press play/pause while HA is down: "HA Connection Error" for ~1 s, then
      the screen restores (v1 left the error up)
- [ ] Power off the WiFi AP: "WiFi Disconnected"; power it back on: device
      reconnects within ~30 s (wifi_check_task) without a UI freeze
- [ ] Wrong HA_TOKEN in config: boot reaches "Cannot Reach HA" gracefully

## 7. Soak

- [ ] Leave playing with art-changing tracks for 2+ hours: no crash, no
      memory errors on console, no display tearing/white bars
- [ ] Rapid random button mashing across screens for a minute: no lockup, no
      tearing (display_lock scope), no missed/double actions

## Known first-flash risk points

If something breaks immediately, look here first and capture the console
traceback:

1. `asyncio.wait_for(ThreadSafeFlag.wait(), …)` in `app/app.py _wait_events`
   — flag/timeout semantics across MicroPython versions.
2. The `/api/template` state poll in `app/ha.py` — confirm the response
   parses (needs HA's template API; check any 4xx in the console).
3. `memoryview(display)` snapshot for JPEG art in `app/artwork.py` — if the
   firmware lacks the buffer protocol it should silently fall back to
   flash redraws ('jpeg_file' path), not crash.

When everything passes: update TESTING.md status below, then push and tag
`v2.0.0`.

**Status: NOT YET TESTED ON HARDWARE**
