# PNG row unfilter + box-averaging primitives — viper-accelerated copy for
# devices whose MicroPython has the viper emitter (RP2350 does; some unix
# builds do not). app/pngfilters.py is the pure-Python reference; keep the
# two in sync line-for-line (only decorators and type annotations differ).
#
# Importing this module on a build without viper raises at compile time;
# pngthumb catches that and falls back to the reference implementation.


@micropython.viper
def unfilter_sub(row: ptr8, n: int, bpp: int):
    i = bpp
    while i < n:
        row[i] = (int(row[i]) + int(row[i - bpp])) & 0xFF
        i += 1


@micropython.viper
def unfilter_up(row: ptr8, prev: ptr8, n: int):
    i = 0
    while i < n:
        row[i] = (int(row[i]) + int(prev[i])) & 0xFF
        i += 1


@micropython.viper
def unfilter_avg(row: ptr8, prev: ptr8, n: int, bpp: int):
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
def unfilter_paeth(row: ptr8, prev: ptr8, n: int, bpp: int):
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
def accum_row(row: ptr8, acc: ptr8, out_w: int, src_w: int, bpp: int):
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
def finalize_row(acc: ptr8, out: ptr8, out_y: int, out_w: int, box_h: int):
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
