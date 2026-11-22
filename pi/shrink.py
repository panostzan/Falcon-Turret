"""Shrinks the planar YUV picture to 3/4 size (640x480 -> 480x360) using only byte slicing, so it needs no numpy and is fast on
the Pi. It keeps 3 of every 4 columns and 3 of every 4 rows (a straight selection, no blending). Used only for the picture sent to
the dashboard; the tracker on the Pi works from the full-size picture."""


def drop_4th_columns(plane):
    """Keep 3 of every 4 bytes of a flat plane whose row width is a multiple of 4, which drops column 3, 7, 11 ... of every row."""
    out = bytearray(len(plane) * 3 // 4)
    out[0::3] = plane[0::4]
    out[1::3] = plane[1::4]
    out[2::3] = plane[2::4]
    return out


def drop_4th_rows(plane, width, rows):
    """Keep 3 of every 4 rows of a plane `rows` tall and `width` wide (drops row 3, 7, 11 ...)."""
    view = memoryview(plane)
    return b"".join(view[r * width:(r + 1) * width] for r in range(rows) if r % 4 != 3)


def shrink_planes(y, u, v, width, height):
    """(Y, U, V) planes of a 4:2:2 picture `width` x `height` (U and V are width/2 wide) -> the same at 3/4 size."""
    return (bytes(drop_4th_rows(drop_4th_columns(y), width * 3 // 4, height)),
            bytes(drop_4th_rows(drop_4th_columns(u), width // 2 * 3 // 4, height)),
            bytes(drop_4th_rows(drop_4th_columns(v), width // 2 * 3 // 4, height)))
