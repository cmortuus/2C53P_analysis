"""Annotated wiring pictures for INTERNAL_BUILD.md: the scope-side taps on OpenScope's board photo,
and a top view of the Pi Zero 2 W in the same orientation as the bench (SD card up, header right)."""
import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "internal_build")
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
F = lambda s: ImageFont.truetype(BOLD, s)

RED, BLK, BLU, GRN, YEL, WHT = (230, 40, 40), (25, 25, 25), (40, 110, 230), (30, 170, 80), (250, 200, 40), (255, 255, 255)


def marker(d, x, y, n, col, r=70):
    d.ellipse([x - r, y - r, x + r, y + r], outline=col, width=16)
    d.ellipse([x + r - 10, y - r - 90, x + r + 90, y - r + 10], fill=col)
    d.text((x + r + 40, y - r - 40), str(n), font=F(80), fill=WHT, anchor="mm")


def scope():
    src = os.path.join(ROOT, "third_party", "OpenScope-2C53T", "docs", "images", "3v3_source.jpeg")
    im = Image.open(src).convert("RGB")
    k = im.width / 1500.0                       # positions picked on the 1500-px-wide view
    d = ImageDraw.Draw(im)
    marker(d, 622 * k, 75 * k, 1, RED)          # battery + lead (silkscreen "+")
    marker(d, 472 * k, 75 * k, 2, YEL)          # battery - lead (silkscreen "-")
    marker(d, 425 * k, 965 * k, 3, BLU, 60)     # 3V3 square pad
    marker(d, 735 * k, 965 * k, 4, YEL, 60)     # GND pad
    leg = [(RED, "1  Battery +  ->  fuse  ->  regulator VIN"),
           (YEL, "2  Battery -  ->  regulator GND  ->  Pi GND (pin 6)"),
           (BLU, "3  3V3 (square pad)  ->  10k  ->  regulator EN"),
           (YEL, "4  GND pad: same ground as 2 (spare)"),
           (WHT, "Do not use SWDIO / SWCLK."),
           (WHT, "Not in this photo: USB D+ / D- and VBUS (at the USB-C port)."),
           ((170, 170, 170), "Photo: OpenScope-2C53T docs (GPL-3), board V1.4. Check your silkscreen matches.")]
    lh = 120
    canvas = Image.new("RGB", (im.width, im.height + lh * len(leg) + 80), BLK)
    canvas.paste(im, (0, 0))
    d = ImageDraw.Draw(canvas)
    for i, (c, t) in enumerate(leg):
        d.text((80, im.height + 60 + i * lh), t, font=F(78 if i < 6 else 60), fill=c)
    canvas = canvas.resize((canvas.width // 2, canvas.height // 2))
    p = os.path.join(OUT, "scope_taps.jpg"); canvas.save(p, quality=88); return p


def pi():
    W, H = 1880, 1560
    im = Image.new("RGB", (W, H), (245, 245, 240))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([200, 120, 860, 1480], 50, fill=(40, 120, 70), outline=(20, 70, 40), width=6)
    d.rounded_rectangle([450, 60, 610, 200], 10, fill=(60, 120, 200)); d.text((530, 130), "SD card", font=F(30), fill=WHT, anchor="mm")
    d.rounded_rectangle([170, 290, 300, 470], 10, fill=(180, 180, 180)); d.text((235, 380), "HDMI", font=F(28), fill=BLK, anchor="mm")
    d.rounded_rectangle([170, 860, 290, 980], 10, fill=GRN, outline=BLK, width=4); d.text((230, 920), "USB", font=F(30), fill=WHT, anchor="mm")
    d.rounded_rectangle([170, 1080, 290, 1200], 10, fill=(180, 180, 180)); d.text((230, 1140), "PWR", font=F(30), fill=BLK, anchor="mm")
    d.rounded_rectangle([430, 1440, 630, 1500], 8, fill=(220, 220, 210)); d.text((530, 1470), "camera", font=F(26), fill=BLK, anchor="mm")
    d.text((320, 920), "USB data port:\nOTG cable here,\nits D+ / D- go\nto the switch", font=F(30), fill=WHT, anchor="lm")
    d.text((320, 1140), "PWR IN: unplug\nafter the build", font=F(30), fill=WHT, anchor="lm")
    xi, xo, y0, dy = 740, 805, 230, 60          # inner (odd) and outer (even) columns, row 1 at the SD end
    d.rectangle([xi - 40, y0 - 40, xo + 40, y0 + 19 * dy + 40], outline=(230, 230, 200), width=3)
    hot = {2: (RED, "Pin 2: 5 V in, from regulator VOUT"),
           6: (BLK, "Pin 6: GND (regulator GND / battery -)"),
           11: (BLU, "Pin 11: GPIO17, from the 15k/22k VBUS divider")}
    for row in range(20):
        for col, x in ((0, xi), (1, xo)):
            pin = row * 2 + 1 + col
            y = y0 + row * dy
            c = hot.get(pin, (None,))[0]
            if pin == 1:
                d.rectangle([x - 16, y - 16, x + 16, y + 16], fill=(220, 190, 90), outline=BLK, width=2)
            else:
                d.ellipse([x - 16, y - 16, x + 16, y + 16], fill=(220, 190, 90), outline=BLK, width=2)
            if c and pin != 1:
                d.ellipse([x - 30, y - 30, x + 30, y + 30], outline=c, width=10)
    ly = {2: 230, 6: 350, 11: 530}
    for pin, (c, t) in hot.items():
        row, col = (pin - 1) // 2, (pin - 1) % 2
        x, y = (xi, xo)[col] + 30, y0 + row * dy
        d.line([x, y, 900, ly[pin]], fill=c if c != BLK else (60, 60, 60), width=5)
        d.text((910, ly[pin]), t, font=F(30), fill=c if c != (200, 200, 200) else (120, 120, 120), anchor="lm")
    d.text((910, 700), "Header holes, counted from the SD-card end:\n outer row (board edge) = even pins 2, 4, 6 ...\n inner row = odd pins 1, 3, 5 ...\n pin 1 has the square pad\n\nPin 2 = outer row, 1st hole\nPin 6 = outer row, 3rd hole\nPin 11 = inner row, 6th hole\n(right beside the CH1 probe's pin 12)", font=F(28), fill=BLK)
    d.text((910, 1200), "Never connect pin 2 to the\nscope's USB-C VBUS.", font=F(32), fill=RED)
    p = os.path.join(OUT, "pi_pins.png"); im.save(p); return p


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    print(scope()); print(pi())


def own_board(src):
    """The owner's own board (2C53T-V1.4_20250507), USB-C corner: confirmed taps marked."""
    im = Image.open(src).convert("RGB").crop((0, 60, 260, 360)).resize((780, 900), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    def m(x, y, n, col, r=22):
        d.ellipse([x - r, y - r, x + r, y + r], outline=col, width=6)
        d.ellipse([x + r - 4, y - r - 36, x + r + 32, y - r], fill=col)
        d.text((x + r + 14, y - r - 18), str(n), font=F(28), fill=WHT, anchor="mm")
    m(331, 548, 1, RED); m(286, 548, 2, YEL); m(262, 806, 3, BLU, 18); m(350, 806, 4, YEL, 18)
    d.rectangle([215, 190, 340, 340], outline=(255, 120, 0), width=5)
    d.text((40, 160), "USB-C: D+ / D- / VBUS still to locate", font=F(26), fill=(255, 140, 0))
    leg = [(RED, "1 Battery +"), (YEL, "2 Battery -  and  4 GND (same ground)"), (BLU, "3 3V3 (square pad)"), (WHT, "Your board: 2C53T-V1.4_20250507")]
    canvas = Image.new("RGB", (im.width, im.height + 190), BLK); canvas.paste(im, (0, 0))
    d = ImageDraw.Draw(canvas)
    for i, (c, t) in enumerate(leg):
        d.text((30, im.height + 20 + i * 42), t, font=F(32), fill=c)
    p = os.path.join(OUT, "own_board_taps.jpg"); canvas.save(p, quality=90); return p
