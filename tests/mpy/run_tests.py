# Off-device test suite, run under the REAL MicroPython interpreter (unix
# port) with hardware modules stubbed. Catches MicroPython-specific breakage
# (API differences, syntax, asyncio semantics) that CPython tests cannot.
#
# Run from the repo root:  make test-mpy
#   (MICROPYPATH="tests/mpy_stubs:." micropython tests/mpy/run_tests.py)
import sys

import asyncio

sys.path.insert(0, "tests/mpy")  # for fake_ha

_failures = []
_count = 0


def check(name, condition):
    global _count
    _count += 1
    if not condition:
        _failures.append(name)
        print("FAIL:", name)


def test_imports():
    # Importing every module under MicroPython is itself the test.
    import app.app          # noqa: F401
    import app.artwork      # noqa: F401
    import app.buttons      # noqa: F401
    import app.ha           # noqa: F401
    import app.hapush       # noqa: F401
    import app.httpc        # noqa: F401
    import app.hw           # noqa: F401
    import app.net          # noqa: F401
    import app.pngthumb     # noqa: F401
    import app.power        # noqa: F401
    import app.screens      # noqa: F401
    import app.textutil     # noqa: F401
    import app.wsclient     # noqa: F401
    check("all modules import under MicroPython", True)


def test_pure_helpers():
    from app.httpc import build_request, parse_url
    from app.textutil import wrap_two_lines
    check("parse_url", parse_url("http://h:8123/a/b") == ("h", 8123, "/a/b"))
    head = build_request("POST", "h", 8123, "/p", {"A": "b"}, 5)
    check("build_request", head.endswith(b"\r\n\r\n") and b"Content-Length: 5" in head)
    check("wrap", wrap_two_lines("Hello Wide World", 10) == ("Hello", "Wide World"))


def test_ha_template():
    from app.ha import HAClient, _state_template
    tpl = _state_template("media_player.kitchen")
    check("template embeds entity", "media_player.kitchen" in tpl)
    check("template requests all fields",
          all(k in tpl for k in ("states(e)", "media_artist", "media_title",
                                 "media_album_name", "volume_level",
                                 "friendly_name", "entity_picture")))
    client = HAClient()
    client.set_entity("media_player.kitchen")
    check("set_entity builds payload", "template" in client._state_tpl)
    check("auth header", client.headers["Authorization"] == "Bearer test-token")


def test_button_queue():
    from app.buttons import Buttons, EV_A_SHORT, EV_X_TAP
    b = Buttons()
    check("empty queue", b.get_events() == ())
    b._push(EV_A_SHORT)
    b._push(EV_X_TAP)
    check("events drain in order", b.get_events() == [EV_A_SHORT, EV_X_TAP])
    check("drained", b.get_events() == ())
    b._push(EV_A_SHORT)
    b.clear()
    check("clear discards", b.get_events() == ())


def test_png_decoder():
    from app.pngthumb import decode_thumbnail

    async def run():
        return await decode_thumbnail("tests/fixtures/test_art.png", 80, 80)

    out = asyncio.run(run())
    check("decode returns cache", out is not None and len(out) == 80 * 80 * 2)
    if out is None:
        return
    # Fixture: left half pure red, right half pure blue.
    # RGB565 stored high-byte-first: red=0xF800 -> F8 00, blue=0x001F -> 00 1F.
    mid = 40 * 80 * 2  # row 40
    left = (out[mid + 10 * 2], out[mid + 10 * 2 + 1])
    right = (out[mid + 70 * 2], out[mid + 70 * 2 + 1])
    check("left half red (byte order)", left == (0xF8, 0x00))
    check("right half blue (byte order)", right == (0x00, 0x1F))


def test_art_pipeline_states():
    from app import artwork
    art = artwork.ArtPipeline({}, lambda: True)
    started = []

    def fake_start(url):  # no real download, but honour the state contract
        started.append(url)
        art.state = artwork.DOWNLOADING

    art._start = fake_start

    check("initial idle", art.state == artwork.IDLE)
    changed = art.maybe_start("Album One", "http://x/art1")
    check("album change detected", changed and started == ["http://x/art1"])
    check("no restart while downloading",
          not art.maybe_start("Album One", "http://x/art1") and len(started) == 1)
    # Back to IDLE with no cache and a URL → retry (v1 behaviour preserved)
    art.state = artwork.IDLE
    art.maybe_start("Album One", "http://x/art1")
    check("idle retry", len(started) == 2)
    changed = art.maybe_start(None, None)
    check("album cleared", changed and art.album is None and len(started) == 2)
    art.reset()
    check("reset clears", art.state == artwork.IDLE and art.album is None)


class FakeHA:
    headers = {}
    connected = True

    def __init__(self):
        self.services = []
        self.state = None

    def set_entity(self, entity_id):
        pass

    def art_url(self, picture):
        return picture

    async def get_state(self):
        return self.state

    async def call_service(self, service, entity_id, retries=1):
        self.services.append(service)
        return True


def _mkstate(**kw):
    base = {"state": "playing", "artist": "Artist", "title": "Title",
            "album": None, "volume": 0.5, "name": "Kitchen", "picture": None}
    base.update(kw)
    return base


def test_app_smoke():
    from app import hw
    from app.app import App
    from app.buttons import EV_A_SHORT, EV_B_SHORT, EV_X_TAP, EV_Y_TAP

    async def run():
        app = App()
        fake = FakeHA()
        app.ha = fake
        app.current_speaker = "media_player.kitchen"
        app.state = _mkstate()

        # Full playback draw
        before = hw.display.update_count
        app.playback_screen.draw()
        check("playback draws", hw.display.update_count > before)
        check("visible tuple set", app.playback_screen.prev_visible is not None)

        # Identical visible state → no redraw
        before = hw.display.update_count
        app.playback_screen.update(_mkstate())
        check("no redraw when unchanged", hw.display.update_count == before)

        # Volume change → zone redraw
        app.playback_screen.update(_mkstate(volume=0.7))
        check("volume zone redraw", hw.display.update_count == before + 1)

        # Playback controls dispatch to HA
        await app.screen.handle(EV_A_SHORT)
        await app.screen.handle(EV_X_TAP)
        await app.screen.handle(EV_Y_TAP)
        check("services called",
              fake.services == ["media_play_pause", "volume_up", "volume_down"])

        # Menu: opens at top item, B returns to playback
        app.menu_screen.index = 2
        await app.screen.handle(EV_B_SHORT)
        check("menu opens at top (v1 bug fixed)",
              app.screen is app.menu_screen and app.menu_screen.index == 0)
        await app.screen.handle(EV_X_TAP)
        check("menu wraps up", app.menu_screen.index == 2)
        fake.state = _mkstate(title="New")
        await app.screen.handle(EV_B_SHORT)
        check("B exits menu with fresh state",
              app.screen is app.playback_screen and app.state["title"] == "New")

        # Brightness clamping
        app.brightness = 0.30
        app.adjust_brightness(-1)
        app.adjust_brightness(-1)
        check("brightness floor", abs(app.brightness - 0.25) < 1e-6)
        for _ in range(20):
            app.adjust_brightness(1)
        check("brightness ceiling", app.brightness == 1.0)
        check("backlight follows", hw.display.backlight == 1.0)

        # Screen sleep: enter blanks, wake redraws and discards the press
        app.power.enter_screen_sleep()
        check("screen sleep blanks", hw.display.backlight == 0)
        app.buttons._push(EV_A_SHORT)
        before = hw.display.update_count
        app.power.wake_screen_sleep()
        check("wake restores backlight", hw.display.backlight == app.brightness)
        check("wake redraws", hw.display.update_count > before)
        check("wake press discarded", app.buttons.get_events() == ())
        check("no accidental service from wake",
              fake.services[-1] != "media_play_pause" or len(fake.services) == 3)

        # Disconnect screen: drawn once per outage, not repeatedly
        app.wifi.connected = False
        before = hw.display.update_count
        app.playback_screen.update(None)
        app.playback_screen.update(None)
        check("error screen drawn once", hw.display.update_count == before + 1)

    asyncio.run(run())


def test_hapush_diff_merge():
    # Pure diff-merge logic — no sockets.
    from app.hapush import HAPush
    push = HAPush()
    changed = push._handle_event({"a": {"media_player.k": {
        "s": "playing",
        "a": {"media_artist": "A", "media_title": "T", "volume_level": 0.5,
              "friendly_name": "K", "irrelevant_attr": 123}}}})
    check("snapshot changed set", changed == {"media_player.k"})
    st = push.states["media_player.k"]
    check("snapshot mapped", st["state"] == "playing" and st["artist"] == "A"
          and st["volume"] == 0.5 and st["picture"] is None)
    check("irrelevant attrs dropped", "irrelevant_attr" not in st)

    push._handle_event({"c": {"media_player.k": {"+": {"a": {"media_title": "T2"}}}}})
    check("diff merges title", st["title"] == "T2" and st["artist"] == "A")
    push._handle_event({"c": {"media_player.k": {
        "+": {"s": "paused"}, "-": {"a": ["media_artist"]}}}})
    check("diff state + removal", st["state"] == "paused" and st["artist"] is None)


def test_hapush_ping_ids_increase():
    # Regression: every command on one connection needs a fresh, increasing
    # id. The second idle keepalive ping used to resend id 2 — real HA
    # answers {'code': 'id_reuse'} and the connection drops every ~60 s.
    import json
    from app.hapush import HAPush

    sent = []

    class FakeWS:
        # Idle line: recv times out until a ping is sent, then delivers the
        # matching pong once. Enough to drive wait_update through two full
        # idle→ping→pong cycles without waiting real seconds.
        def __init__(self):
            self._owed_pong = False

        async def send(self, text):
            msg = json.loads(text)
            sent.append(msg)
            if msg.get("type") == "ping":
                self._owed_pong = True

        async def recv(self, timeout):
            if self._owed_pong:
                self._owed_pong = False
                return json.dumps({"id": sent[-1]["id"], "type": "pong"})
            raise asyncio.TimeoutError

    push = HAPush()
    ws = FakeWS()
    push._ws = ws

    def two_pings_answered():
        pings = [m for m in sent if m.get("type") == "ping"]
        return len(pings) >= 2 and not ws._owed_pong

    asyncio.run(push.wait_update(two_pings_answered))
    ids = [m["id"] for m in sent]
    check("ping ids strictly increase",
          len(ids) >= 2 and all(b > a for a, b in zip(ids, ids[1:])))


def test_websocket_end_to_end():
    # Real TCP loopback: wsclient + hapush against a scripted RFC 6455 server.
    from app.hapush import HAPush
    from fake_ha import PORT, FakeHAServer

    async def run():
        server = FakeHAServer()
        await server.start()
        try:
            push = HAPush()
            push._host, push._port = "127.0.0.1", PORT
            await push.connect(["media_player.kitchen", "media_player.den"],
                               timeout=5)
            check("auth sent", server.received[0]["type"] == "auth"
                  and server.received[0]["access_token"] == "test-token")

            changed = await push.wait_update(lambda: False)
            # The server only emits events after processing the subscribe, so
            # it is guaranteed to have been received by now.
            check("subscribe sent",
                  server.received[1]["type"] == "subscribe_entities"
                  and server.received[1]["entity_ids"] ==
                  ["media_player.kitchen", "media_player.den"])
            check("snapshot event", changed ==
                  {"media_player.kitchen", "media_player.den"})
            kitchen = push.states["media_player.kitchen"]
            check("snapshot fields", kitchen["state"] == "playing"
                  and kitchen["artist"] == "Artist A"
                  and kitchen["picture"] == "/img/1.png"
                  and push.states["media_player.den"]["name"] == "Den")

            changed = await push.wait_update(lambda: False)
            check("diff event", changed == {"media_player.kitchen"}
                  and kitchen["artist"] == "Artist B"
                  and kitchen["title"] == "Song Two")

            await push.wait_update(lambda: False)
            check("state diff + removal applied",
                  kitchen["state"] == "paused" and kitchen["picture"] is None)

            check("should_stop honoured",
                  await push.wait_update(lambda: True) is None)

            # Next wait: client auto-pongs the server's ping, then the server
            # closes — the client must surface that as OSError.
            raised = False
            try:
                await push.wait_update(lambda: False)
            except OSError:
                raised = True
            check("close surfaces as OSError", raised)
            check("client answered ws ping", server.pong_payload == b"keepalive")
            check("server script completed", server.done)
            push.close()
        finally:
            server.close()

    asyncio.run(run())


def test_soak():
    # Thousands of randomized update cycles: no leak, no crash, no runaway.
    import gc
    from app import hw
    from app.app import App
    from app.buttons import EV_B_SHORT

    lcg = [12345]

    def rnd(n):
        lcg[0] = (lcg[0] * 1103515245 + 12345) & 0x7FFFFFFF
        return lcg[0] % n

    async def run():
        app = App()
        fake = FakeHA()
        fake.state = _mkstate()
        app.ha = fake
        app.current_speaker = "media_player.kitchen"
        app.state = _mkstate()
        app.playback_screen.draw()

        gc.collect()
        base = gc.mem_alloc()
        for i in range(2000):
            state = _mkstate(volume=rnd(100) / 100.0,
                             title="Track %d" % rnd(40),
                             artist="Artist %d" % rnd(15),
                             state="playing" if rnd(4) else "paused")
            app.playback_screen.update(state)
            if i % 250 == 249:  # periodic menu round-trip
                await app.screen.handle(EV_B_SHORT)
                await app.screen.handle(EV_B_SHORT)
        gc.collect()
        growth = gc.mem_alloc() - base
        check("soak: heap growth bounded (%d bytes)" % growth, growth < 16384)
        check("soak: rendering active", hw.display.update_count > 1500)

    asyncio.run(run())


def main():
    for test in (test_imports, test_pure_helpers, test_ha_template,
                 test_button_queue, test_png_decoder,
                 test_art_pipeline_states, test_app_smoke,
                 test_hapush_diff_merge, test_hapush_ping_ids_increase,
                 test_websocket_end_to_end,
                 test_soak):
        test()
    try:  # adjust_brightness persists to cwd — don't leave it behind
        import os
        os.remove("brightness.json")
    except OSError:
        pass
    print("-" * 40)
    if _failures:
        print("%d/%d checks FAILED" % (len(_failures), _count))
        raise SystemExit(1)
    print("all %d checks passed (MicroPython)" % _count)


main()
