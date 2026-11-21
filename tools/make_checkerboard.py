"""Makes the printable calibration checkerboard for the stereo cameras.

The stereo calibration tool expects 9x6 INNER corners, i.e. 10x7 squares. Squares are 25 mm by default. The page is US Letter
landscape, rendered at 300 DPI so that PRINTING AT 100% (actual size, no "fit to page") gives the exact square size.
A 100 mm ruler is printed under the board: measure it after printing. If it is not 100 mm, the print was scaled; reprint at 100%.

Run:  python tools/make_checkerboard.py            -> calibration_board.pdf (and .png) in the project folder
"""
import os
import sys

import cv2
import numpy as np
from PIL import Image

DPI = 300
MM = DPI / 25.4                      # pixels per millimetre
INNER = (9, 6)                       # inner corners (columns, rows): what the calibration command is given as --board 9 6
SQUARE_MM = 25.0
PAGE_MM = (279.4, 215.9)             # US Letter, landscape


def render(square_mm=SQUARE_MM, inner=INNER):
    """Returns (page image as uint8 array, board top-left in pixels)."""
    cols, rows = inner[0] + 1, inner[1] + 1                       # squares
    page = np.full((round(PAGE_MM[1] * MM), round(PAGE_MM[0] * MM)), 255, np.uint8)
    sq = square_mm * MM
    board_w, board_h = cols * sq, rows * sq
    x0 = (page.shape[1] - board_w) / 2
    y0 = (page.shape[0] - board_h) / 2 - 8 * MM                   # leave room for the ruler underneath
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                xa, ya = round(x0 + c * sq), round(y0 + r * sq)
                xb, yb = round(x0 + (c + 1) * sq), round(y0 + (r + 1) * sq)
                page[ya:yb, xa:xb] = 0
    # 100 mm ruler with end ticks, and a short caption
    ry = round(y0 + board_h + 10 * MM)
    rx0, rx1 = round((page.shape[1] - 100 * MM) / 2), round((page.shape[1] + 100 * MM) / 2)
    cv2.line(page, (rx0, ry), (rx1, ry), 0, 3)
    for x in (rx0, rx1):
        cv2.line(page, (x, ry - 12), (x, ry + 12), 0, 3)
    caption = f"This line must measure 100 mm. Squares are {square_mm:g} mm. Print at 100% / actual size."
    cv2.putText(page, caption, (rx0 - round(45 * MM), ry + 48), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2, cv2.LINE_AA)
    return page, (x0, y0)


def main(out_dir="."):
    page, _ = render()
    found, corners = cv2.findChessboardCornersSB(page, INNER)
    if not found or len(corners) != INNER[0] * INNER[1]:
        sys.exit("self-check failed: OpenCV cannot find the 9x6 inner corners on the generated board")
    pitch = np.median(np.linalg.norm(np.diff(corners.reshape(INNER[1], INNER[0], 2), axis=1), axis=2)) / MM
    print(f"self-check: OpenCV found all {len(corners)} inner corners; measured square pitch {pitch:.2f} mm")
    png, pdf = os.path.join(out_dir, "calibration_board.png"), os.path.join(out_dir, "calibration_board.pdf")
    img = Image.fromarray(page)
    img.save(png, dpi=(DPI, DPI))
    img.save(pdf, "PDF", resolution=DPI)
    print(f"wrote {pdf} and {png}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
