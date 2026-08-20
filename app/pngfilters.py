# PNG row unfilter + box-averaging primitives — pure-Python REFERENCE
# implementation. app/pngfilters_viper.py is the viper-accelerated copy used
# on-device; keep the two in sync line-for-line (only decorators and type
# annotations differ). This module is the fallback when the running
# MicroPython lacks the viper emitter, and is what off-device tests exercise.


def unfilter_sub(row, n, bpp):
    i = bpp
    while i < n:
        row[i] = (row[i] + row[i - bpp]) & 0xFF
        i += 1


def unfilter_up(row, prev, n):
    i = 0
    while i < n:
        row[i] = (row[i] + prev[i]) & 0xFF
        i += 1


def unfilter_avg(row, prev, n, bpp):
    i = 0
    while i < n:
        b = prev[i]
        a = row[i - bpp] if i >= bpp else 0
        row[i] = (row[i] + ((a + b) >> 1)) & 0xFF
        i += 1


def unfilter_paeth(row, prev, n, bpp):
    i = 0
    while i < n:
        b = prev[i]
        if i >= bpp:
            a = row[i - bpp]
            c = prev[i - bpp]
        else:
            a = 0
            c = 0
        p = a + b - c
        pa = p - a if p >= a else a - p
        pb = p - b if p >= b else b - p
        pc = p - c if p >= c else c - p
        if pa <= pb and pa <= pc:
            pr = a
        elif pb <= pc:
            pr = b
        else:
            pr = c
        row[i] = (row[i] + pr) & 0xFF
        i += 1


def accum_row(row, acc, out_w, src_w, bpp):
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
            r_s += row[pi]
            g_s += row[pi + 1]
            b_s += row[pi + 2]
            sx += 1
        cnt = sx1 - sx0
        ai = x * 6
        r_a = (acc[ai] | (acc[ai + 1] << 8)) + r_s // cnt
        g_a = (acc[ai + 2] | (acc[ai + 3] << 8)) + g_s // cnt
        b_a = (acc[ai + 4] | (acc[ai + 5] << 8)) + b_s // cnt
        acc[ai] = r_a & 0xFF
        acc[ai + 1] = (r_a >> 8) & 0xFF
        acc[ai + 2] = g_a & 0xFF
        acc[ai + 3] = (g_a >> 8) & 0xFF
        acc[ai + 4] = b_a & 0xFF
        acc[ai + 5] = (b_a >> 8) & 0xFF
        x += 1


def finalize_row(acc, out, out_y, out_w, box_h):
    """Divide acc by box_h, write RGB565 (high byte first) to out row, clear acc."""
    x = 0
    base = out_y * out_w * 2
    while x < out_w:
        ai = x * 6
        r = (acc[ai] | (acc[ai + 1] << 8)) // box_h
        g = (acc[ai + 2] | (acc[ai + 3] << 8)) // box_h
        b = (acc[ai + 4] | (acc[ai + 5] << 8)) // box_h
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
