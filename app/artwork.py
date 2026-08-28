# Album-art pipeline: download → format detect → decode → 80×80 RGB565 cache.
#
# Both PNG and JPEG normally end in the same pixel cache, so every redraw of
# existing art is a pure memoryview row copy — no flash re-reads, no decoder
# state. (HA's media proxy passes through whatever the streaming service
# provides: Apple Music/Tidal/Amazon send PNG, others JPEG — detect by magic
# bytes, never by URL.)
#
# The pixel cache stores RGB565 high-byte-first (big-endian on wire), which is
# the PicoGraphics framebuffer layout — required for the memcpy fast path.
import gc
import os
import time

import asyncio

from app import hw, httpc, log, pngthumb
from app.settings import HTTP_TIMEOUT

import jpegdec

try:
    import pngdec  # noqa: F401 — presence signals firmware support; decode is ours
except ImportError:
    pngdec = None

# Pipeline states
IDLE = 0
DOWNLOADING = 1   # HTTP connection active — state poll must skip (CYW43 limit)
DECODING = 2      # PNG decode in progress — HTTP free, state poll OK
FILE_READY = 3    # JPEG on flash, decode deferred until playback is visible
READY = 4

ART_X = 20
ART_Y = 40
ART_SIZE = 80

_ART_FILE = "/album_art.jpg"  # historical name — holds PNG or JPEG bytes
_MIN_FREE_FOR_ART = 30000     # skip art entirely when heap is this tight


class ArtPipeline:
    def __init__(self, headers, playback_active):
        """headers: HA auth headers for the download.
        playback_active: callable → True when the playback screen is on and
        awake, i.e. it is safe to paint the art zone of the framebuffer."""
        self._headers = headers
        self._playback_active = playback_active
        self._jpeg = jpegdec.JPEG(hw.display)
        self.state = IDLE
        self.album = None      # album name the current art belongs to
        self._url = None
        self._cache = None     # 80×80 RGB565 bytearray, or None
        self._fmt = None       # 'cache' or 'jpeg_file' (no-buffer-protocol fallback)
        self._jpeg_scale = 0   # scale used by the jpeg_file fallback
        self._task = None

    def cancel(self):
        """Cancel any in-progress download/decode and clear current art."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.state = IDLE
        self._url = None
        self._cache = None
        self._fmt = None

    def reset(self):
        """cancel() plus forget the album — forces a fresh download on the
        next draw (used on deep-sleep entry, where art may go stale)."""
        self.cancel()
        self.album = None

    def maybe_start(self, album, url):
        """Ensure art matches (album, url); start a download when needed.
        Safe to call with display_lock held (only schedules a task).
        Returns True if the album changed (caller should show the placeholder)."""
        changed = album != self.album
        if changed:
            self.cancel()
            self.album = album
            if url:
                self._start(url)
        elif self.state == IDLE and url and self._cache is None and self._fmt is None:
            self._start(url)
        return changed

    def _start(self, url):
        if gc.mem_free() < _MIN_FREE_FOR_ART:
            gc.collect()
            if gc.mem_free() < _MIN_FREE_FOR_ART:
                log.debug("art skipped: low memory")
                return
        self._url = url
        self.state = DOWNLOADING
        self._task = asyncio.create_task(self._run(url))

    async def _run(self, url):
        try:
            try:
                os.remove(_ART_FILE)
            except OSError:
                pass
            status = await httpc.request_to_file(
                url, self._headers, _ART_FILE, timeout=HTTP_TIMEOUT)
            if status != 200:
                log.error("art download failed: HTTP %s" % status)
                self.state = IDLE
                return
            with open(_ART_FILE, "rb") as f:
                magic = f.read(4)
            if magic[:4] == b"\x89PNG":
                # HTTP connection is closed now — DECODING lets the state poll resume.
                self.state = DECODING
                t0 = time.ticks_ms()
                cache = await pngthumb.decode_thumbnail(_ART_FILE, ART_SIZE, ART_SIZE)
                log.debug("png decode %d ms" % time.ticks_diff(time.ticks_ms(), t0))
                if cache is None:
                    log.error("PNG thumbnail decode failed")
                    self.state = IDLE
                    return
                self._cache = cache
                self._fmt = "cache"
                self.state = READY
                self._show_if_visible()
            elif magic[:2] == b"\xff\xd8":
                if self._playback_active():
                    hw.display_lock.acquire()
                    try:
                        self._finish_jpeg_locked()
                        hw.display.update()
                    finally:
                        hw.display_lock.release()
                else:
                    # Decoding paints the framebuffer, which currently shows a
                    # different screen — defer until the playback draw path.
                    self.state = FILE_READY
            else:
                log.error("art: unsupported format %r" % magic)
                self.state = IDLE
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network/filesystem errors — keep the UI alive
            log.error("art task failed: %r" % e)
            self.state = IDLE
            self._cache = None
            self._fmt = None
        finally:
            self._task = None

    def _show_if_visible(self):
        """Paint finished art immediately so it appears without waiting for
        the next state poll. No-op when another screen is showing."""
        if not self._playback_active():
            return
        hw.display_lock.acquire()
        try:
            self.draw_locked()
            hw.display.update()
        finally:
            hw.display_lock.release()

    def _finish_jpeg_locked(self):
        """Decode the downloaded JPEG into the framebuffer, then snapshot the
        art region into the pixel cache. display_lock MUST be held."""
        self._jpeg.open_file(_ART_FILE)
        # Pick a scale for ~80px output; fall back to a file-size proxy
        # (~400 bytes/px) when get_width() is unavailable.
        try:
            src_w = self._jpeg.get_width()
        except AttributeError:
            src_w = os.stat(_ART_FILE)[6] // 400
        if src_w >= 640:
            scale = jpegdec.JPEG_SCALE_EIGHTH
        elif src_w >= 320:
            scale = getattr(jpegdec, "JPEG_SCALE_QUARTER", 2)
        elif src_w >= 160:
            scale = jpegdec.JPEG_SCALE_HALF
        else:
            scale = 0  # JPEG_SCALE_FULL — source already small
        hw.display.set_pen(hw.BLACK)
        hw.display.rectangle(ART_X, ART_Y, ART_SIZE, ART_SIZE)
        hw.display.set_clip(ART_X, ART_Y, ART_SIZE, ART_SIZE)
        self._jpeg.decode(ART_X, ART_Y, scale)
        hw.display.remove_clip()
        try:
            self._snapshot_to_cache()
            self._fmt = "cache"
        except TypeError:
            # Display lacks the buffer protocol — redraw from flash instead.
            self._fmt = "jpeg_file"
            self._jpeg_scale = scale
        self.state = READY

    def _snapshot_to_cache(self):
        """Copy the just-decoded art region out of the framebuffer."""
        fb = memoryview(hw.display)
        row_bytes = ART_SIZE * 2
        cache = bytearray(ART_SIZE * row_bytes)
        for row in range(ART_SIZE):
            src = ((ART_Y + row) * hw.WIDTH + ART_X) * 2
            cache[row * row_bytes:(row + 1) * row_bytes] = fb[src:src + row_bytes]
        self._cache = cache

    def draw_locked(self):
        """Draw the art zone for the current pipeline state. Lock MUST be held."""
        if self.state in (DOWNLOADING, DECODING):
            self._placeholder_locked("Loading...")
        elif self.state == FILE_READY:
            self._finish_jpeg_locked()  # paints the framebuffer as it decodes
        elif self.state == READY and self._fmt == "cache" and self._cache is not None:
            self._draw_cache_locked()
        elif self.state == READY and self._fmt == "jpeg_file":
            self._jpeg.open_file(_ART_FILE)
            hw.display.set_clip(ART_X, ART_Y, ART_SIZE, ART_SIZE)
            self._jpeg.decode(ART_X, ART_Y, self._jpeg_scale)
            hw.display.remove_clip()
        else:
            self._placeholder_locked("No Art")

    def _draw_cache_locked(self):
        fb = memoryview(hw.display)
        cache = self._cache
        row_bytes = ART_SIZE * 2
        for row in range(ART_SIZE):
            dst = ((ART_Y + row) * hw.WIDTH + ART_X) * 2
            fb[dst:dst + row_bytes] = cache[row * row_bytes:(row + 1) * row_bytes]

    def _placeholder_locked(self, text):
        display = hw.display
        display.set_pen(hw.GRAY)
        display.rectangle(ART_X, ART_Y, ART_SIZE, ART_SIZE)
        display.set_pen(hw.WHITE)
        text_w = display.measure_text(text, 1)
        display.text(text, ART_X + (ART_SIZE - text_w) // 2,
                     ART_Y + (ART_SIZE - 8) // 2, scale=1)
