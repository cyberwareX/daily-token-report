#!/usr/bin/env python3
"""Turn a saved real run (stdout + stderr of daily_token_report.py) into docs/demo.gif, a terminal replay.

  python3 tools/make_gif.py run.out "python3 daily_token_report.py watchlist.txt" docs/demo.gif

Needs Pillow. The text is the saved output, unchanged; only the typing and line timing are simulated.
"""
import sys

from PIL import Image, ImageDraw, ImageFont

W, PAD, LH, FS = 1100, 18, 22, 15
BG, FG, DIM = (18, 20, 24), (220, 223, 228), (130, 136, 146)
COLORS = {"OK": (80, 200, 120), "CAUTION": (240, 190, 60), "DANGER": (240, 90, 90), "NOT CHECKED": (240, 190, 60),
          "CHECK AGAIN": (240, 190, 60)}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def color(line):
    if "| BLOCK " in line or "(worse)" in line:
        return COLORS["DANGER"]
    if "NOT CHECKED" in line or "| LIMIT " in line or "| RECHECK " in line:
        return COLORS["CAUTION"]
    if "| ALLOW " in line:
        return COLORS["OK"]
    if line.startswith(("https://", "Automated")):
        return DIM
    return FG


def frame(lines, rows, font):
    img = Image.new("RGB", (W, PAD * 2 + LH * rows), BG)
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines[-rows:]):
        user = ln.startswith(("$ ", "> "))
        d.text((PAD, PAD + i * LH), ln[:118], font=font, fill=(110, 170, 255) if user else color(ln))
    return img


def main(out_path, cmd, gif_path):
    font = ImageFont.truetype(FONT, FS)
    body = [ln.rstrip("\n") for ln in open(out_path)]
    rows = min(len(body) + 2, 26)
    frames, durs = [], []
    for i in range(0, len(cmd) + 1, 4):                       # typing the command
        frames.append(frame(["$ " + cmd[:i]], rows, font)); durs.append(40)
    shown = ["$ " + cmd]
    frames.append(frame(shown, rows, font)); durs.append(500)
    for ln in body:
        shown.append(ln)
        frames.append(frame(shown, rows, font))
        durs.append(900 if ln.startswith("Rug check:") else (60 if ln else (600 if ln.startswith("> ") else 250)))
    durs[-1] = 4000
    frames[0].save(gif_path, save_all=True, append_images=frames[1:], duration=durs, loop=0, optimize=True)


if __name__ == "__main__":
    main(*sys.argv[1:4])
