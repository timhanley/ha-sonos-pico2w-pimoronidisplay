# Streaming PNG thumbnail decoder — full-image downscaling via box averaging.
#
# pngdec's C decode() is upscale-only and open_RAM needs more heap than is
# free after the framebuffer, so this decodes IDAT with deflate.DeflateIO and
# viper-accelerated unfilter/accumulate loops instead.
#
# INVARIANTS:
#   - deflate.DeflateIO only accepts C-level streams (real files, io.BytesIO)
#     — a Python wrapper object with read() raises "stream operation not
#     supported". Single-IDAT: seek the source file to the data and hand it
#     over directly. Multi-IDAT: concatenate into BytesIO, flash tmp file as
#     a MemoryError fallback only.
#   - Output pixels are RGB565 stored high-byte-first (big-endian on wire),
#     matching the PicoGraphics framebuffer layout for direct memcpy.
import gc

import asyncio

from app import log

_IDAT_TMP = "/idat.tmp"


@micropython.viper
def _unfilter_sub(row: ptr8, n: int, bpp: int):
    i = bpp
    while i < n:
        row[i] = (int(row[i]) + int(row[i - bpp])) & 0xFF
        i += 1


@micropython.viper
def _unfilter_up(row: ptr8, prev: ptr8, n: int):
    i = 0
    while i < n:
        row[i] = (int(row[i]) + int(prev[i])) & 0xFF
        i += 1


@micropython.viper
def _unfilter_avg(row: ptr8, prev: ptr8, n: int, bpp: int):
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
def _unfilter_paeth(row: ptr8, prev: ptr8, n: int, bpp: int):
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
def _accum_row(row: ptr8, acc: ptr8, out_w: int, src_w: int, bpp: int):
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
        ai = x * 6
        r_a = (int(acc[ai]) | (int(acc[ai + 1]) << 8)) + r_s // cnt
        g_a = (int(acc[ai + 2]) | (int(acc[ai + 3]) << 8)) + g_s // cnt
        b_a = (int(acc[ai + 4]) | (int(acc[ai + 5]) << 8)) + b_s // cnt
        acc[ai] = r_a & 0xFF
        acc[ai + 1] = (r_a >> 8) & 0xFF
        acc[ai + 2] = g_a & 0xFF
        acc[ai + 3] = (g_a >> 8) & 0xFF
        acc[ai + 4] = b_a & 0xFF
        acc[ai + 5] = (b_a >> 8) & 0xFF
        x += 1


@micropython.viper
def _finalize_row(acc: ptr8, out: ptr8, out_y: int, out_w: int, box_h: int):
    """Divide acc by box_h, write RGB565 (high byte first) to out row, clear acc."""
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
        out[oi] = pixel >> 8
        out[oi + 1] = pixel & 0xFF
        acc[ai] = 0
        acc[ai + 1] = 0
        acc[ai + 2] = 0
        acc[ai + 3] = 0
        acc[ai + 4] = 0
        acc[ai + 5] = 0
        x += 1


async def decode_thumbnail(path, out_w=80, out_h=80):
    """Decode a PNG file to an out_w×out_h RGB565 bytearray, or None on failure.

    Yields to the asyncio event loop every 5 rows so buttons stay responsive.
    """
    try:
        import deflate
    except ImportError:
        log.error("PNG decode: deflate module not available")
        return None
    gc.collect()
    f = None
    use_tmp = False
    try:
        f = open(path, "rb")
        if f.read(8)[:4] != b"\x89PNG":
            return None
        hdr = f.read(8)
        if hdr[4:8] != b"IHDR":
            return None
        ihdr = f.read(13)
        iw = (ihdr[0] << 24) | (ihdr[1] << 16) | (ihdr[2] << 8) | ihdr[3]
        ih = (ihdr[4] << 24) | (ihdr[5] << 16) | (ihdr[6] << 8) | ihdr[7]
        bit_depth = ihdr[8]
        color_type = ihdr[9]
        interlace = ihdr[12]
        f.read(4)  # IHDR CRC
        if bit_depth != 8 or interlace != 0:
            log.error("PNG: unsupported bit_depth=%d interlace=%d" % (bit_depth, interlace))
            return None
        if color_type == 2:
            bpp = 3
        elif color_type == 6:
            bpp = 4
        else:
            log.error("PNG: unsupported color type %d" % color_type)
            return None

        # Scan chunk headers to locate IDAT segments (seeks past data + CRC).
        idat_segs = []  # (file_offset_of_data, data_len)
        while True:
            chdr = f.read(8)
            if len(chdr) < 8:
                break
            dlen = (chdr[0] << 24) | (chdr[1] << 16) | (chdr[2] << 8) | chdr[3]
            ctype = chdr[4:8]
            if ctype == b"IEND":
                break
            elif ctype == b"IDAT":
                idat_segs.append((f.tell(), dlen))
            f.seek(dlen + 4, 1)
        if not idat_segs:
            log.error("PNG: no IDAT chunks found")
            return None

        # Prepare the ZLIB source stream.
        if len(idat_segs) == 1:
            f.seek(idat_segs[0][0])
            zlib = deflate.DeflateIO(f, deflate.ZLIB)
        else:
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
                buf = bytearray(4096)
                with open(_IDAT_TMP, "wb") as tmp:
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
                f = open(_IDAT_TMP, "rb")
                use_tmp = True
            zlib = deflate.DeflateIO(f, deflate.ZLIB)

        # Decode rows with box averaging.
        row_stride = iw * bpp
        out = bytearray(out_w * out_h * 2)
        row = bytearray(row_stride)
        prev = bytearray(row_stride)
        acc = bytearray(out_w * 6)
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
                _unfilter_sub(row, row_stride, bpp)
            elif filter_type == 2:
                _unfilter_up(row, prev, row_stride)
            elif filter_type == 3:
                _unfilter_avg(row, prev, row_stride, bpp)
            elif filter_type == 4:
                _unfilter_paeth(row, prev, row_stride, bpp)
            _accum_row(row, acc, out_w, iw, bpp)
            row_in_box += 1
            if src_y + 1 >= (out_y + 1) * ih // out_h or src_y + 1 >= ih:
                _finalize_row(acc, out, out_y, out_w, row_in_box)
                out_y += 1
                row_in_box = 0
            row, prev = prev, row
            if src_y % 5 == 4:
                await asyncio.sleep(0)
        if out_y < out_h:
            log.error("PNG: only decoded %d/%d rows" % (out_y, out_h))
        return out
    finally:
        # try/finally (no except) — required for await asyncio.sleep(0) to work
        # correctly in MicroPython. Exceptions propagate to the caller.
        if f:
            f.close()
        if use_tmp:
            try:
                import os
                os.remove(_IDAT_TMP)
            except OSError:
                pass
