# Pure text helpers — no hardware dependencies, unit-testable under CPython.


def wrap_two_lines(text, chars_per_line):
    """Split text into up to two lines, breaking at the last space that fits.

    bitmap8 is a fixed-width font (8px per char at scale 2), so a character
    budget is exact. Returns (line1, line2_or_None); anything beyond two
    lines is truncated, matching the display layout's two text rows.
    """
    if len(text) <= chars_per_line:
        return text, None
    space_pos = text[:chars_per_line].rfind(" ")
    if space_pos > 0:
        return text[:space_pos], text[space_pos + 1:]
    return text[:chars_per_line], text[chars_per_line:]
