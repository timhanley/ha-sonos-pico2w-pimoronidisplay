# Streaming PNG thumbnail decoder — full-image downscaling via box averaging.
#
# pngdec's C decode() is upscale-only and open_RAM needs more heap than is
# free after the framebuffer, so this decodes IDAT with deflate.DeflateIO and
# per-row unfilter/accumulate primitives (viper-accelerated where the
# firmware supports it — see app/pngfilters_viper.py / app/pngfilters.py).
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

try:
    from app import pngfilters_viper as _filters
except (ImportError, SyntaxError, ValueError):
    # This MicroPython build lacks the viper emitter — use the (much slower)
    # pure-Python reference implementation.
    from app import pngfilters as _filters
    log.info("PNG: viper emitter unavailable, using pure-Python filters")

_IDAT_TMP = "/idat.tmp"


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
    # Threshold GC fires constantly during the row loop's allocation churn
    # and multiplies decode time; disable it for the decode, restore after.
    # (A genuinely full heap still auto-collects on allocation failure.)
    gc_thr = gc.threshold()
    gc.threshold(-1)
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
        log.debug("png %dx%d bpp=%d segs=%d idat=%d" %
                  (iw, ih, bpp, len(idat_segs),
                   sum(dlen for _, dlen in idat_segs)))

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
                        await asyncio.sleep(0)
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
                            # flash-to-flash copy of a whole image — without
                            # this yield it stalls the event loop for seconds
                            await asyncio.sleep(0)
                f.close()
                f = open(_IDAT_TMP, "rb")
                use_tmp = True
            zlib = deflate.DeflateIO(f, deflate.ZLIB)

        # Decode rows with box averaging.
        row_stride = iw * bpp
        out = bytearray(out_w * out_h * 2)
        row = bytearray(row_stride)
        prev = bytearray(row_stride)
        row_mv = memoryview(row)
        fbuf = bytearray(1)
        acc = bytearray(out_w * 6)
        out_y = 0
        row_in_box = 0
        for src_y in range(ih):
            if out_y >= out_h:
                break
            # readinto everywhere: zlib.read() allocates a fresh buffer per
            # call, and that churn (plus the GC it triggers) dominated decode
            # time on-device — 32 ms/row against ~2 ms of actual filter work.
            if zlib.readinto(fbuf) != 1:
                break
            filter_type = fbuf[0]
            offset = 0
            while offset < row_stride:
                got = zlib.readinto(row_mv[offset:] if offset else row_mv)
                if not got:
                    break
                offset += got
            if offset < row_stride:
                break
            if filter_type == 1:
                _filters.unfilter_sub(row, row_stride, bpp)
            elif filter_type == 2:
                _filters.unfilter_up(row, prev, row_stride)
            elif filter_type == 3:
                _filters.unfilter_avg(row, prev, row_stride, bpp)
            elif filter_type == 4:
                _filters.unfilter_paeth(row, prev, row_stride, bpp)
            _filters.accum_row(row, acc, out_w, iw, bpp)
            row_in_box += 1
            if src_y + 1 >= (out_y + 1) * ih // out_h or src_y + 1 >= ih:
                _filters.finalize_row(acc, out, out_y, out_w, row_in_box)
                out_y += 1
                row_in_box = 0
            row, prev = prev, row
            row_mv = memoryview(row)
            if src_y % 5 == 4:
                await asyncio.sleep(0)
        if out_y < out_h:
            log.error("PNG: only decoded %d/%d rows" % (out_y, out_h))
        return out
    finally:
        # try/finally (no except) — required for await asyncio.sleep(0) to work
        # correctly in MicroPython. Exceptions propagate to the caller.
        gc.threshold(gc_thr)
        if f:
            f.close()
        if use_tmp:
            try:
                import os
                os.remove(_IDAT_TMP)
            except OSError:
                pass
