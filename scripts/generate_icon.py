"""Generate alarm system .ico files for server and client.

Uses 4x supersampling for clean anti-aliased edges at every size.
Pillow's ICO append_images is broken in many versions, so we build
the ICO binary manually.
"""
import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw


def _render_bell(size: int, bell_color: str, bg_color: str) -> Image.Image:
    """Render an alarm-bell icon at *size* px with supersampling."""
    # Draw at 4x then downsample for smooth edges
    SS = 4
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = big / 64  # normalised scale factor (design grid = 64)

    # --- Rounded-square background ---
    pad = int(2 * s)
    d.rounded_rectangle(
        [pad, pad, big - pad, big - pad],
        radius=int(14 * s),
        fill=bg_color,
    )

    # --- Bell body (smooth trapezoid via polygon + arcs) ---
    # Coordinates on the 64-unit grid, scaled by `s`
    # The bell widens from the dome down to the rim.

    # Dome (top half-circle)
    dome_cx = 32 * s
    dome_top = 14 * s
    dome_r = 10 * s
    d.ellipse(
        [dome_cx - dome_r, dome_top - dome_r * 0.3,
         dome_cx + dome_r, dome_top + dome_r * 1.4],
        fill=bell_color,
    )

    # Bell body — a tapered shape
    body_top = 18 * s
    body_bot = 44 * s
    top_half = 9 * s   # half-width at top
    bot_half = 18 * s  # half-width at bottom
    cx = 32 * s
    d.polygon(
        [
            (cx - top_half, body_top),
            (cx + top_half, body_top),
            (cx + bot_half, body_bot),
            (cx - bot_half, body_bot),
        ],
        fill=bell_color,
    )

    # Rim (wide rounded bar at the bottom of the bell)
    rim_top = 43 * s
    rim_bot = 49 * s
    rim_half = 21 * s
    d.rounded_rectangle(
        [cx - rim_half, rim_top, cx + rim_half, rim_bot],
        radius=int(3 * s),
        fill=bell_color,
    )

    # --- Subtle highlight (lighter stripe on left side of body) ---
    highlight = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hl_half = 4 * s
    hd.polygon(
        [
            (cx - top_half + 3 * s, body_top + 2 * s),
            (cx - top_half + 3 * s + hl_half, body_top + 2 * s),
            (cx - bot_half + 5 * s + hl_half, body_bot - 2 * s),
            (cx - bot_half + 5 * s, body_bot - 2 * s),
        ],
        fill=(255, 255, 255, 50),
    )
    img = Image.alpha_composite(img, highlight)

    # --- Clapper (small circle hanging below rim) ---
    d2 = ImageDraw.Draw(img)
    clap_r = 3.5 * s
    clap_cy = 53 * s
    d2.ellipse(
        [cx - clap_r, clap_cy - clap_r, cx + clap_r, clap_cy + clap_r],
        fill=bell_color,
    )

    # --- Small knob on top ---
    knob_r = 2.8 * s
    knob_cy = 11 * s
    d2.ellipse(
        [cx - knob_r, knob_cy - knob_r, cx + knob_r, knob_cy + knob_r],
        fill=bell_color,
    )

    # --- Sound waves (two arcs on each side) ---
    wave_color = bell_color
    lw = max(1, int(1.8 * s))
    for sign in (-1, 1):
        for i, offset in enumerate((22, 28)):
            arc_cx = cx + sign * offset * s
            arc_r = 6 * s + i * 3 * s
            # small arc facing outward
            start = 120 if sign == -1 else 300
            end = 240 if sign == -1 else 60
            d2.arc(
                [arc_cx - arc_r, 20 * s - arc_r,
                 arc_cx + arc_r, 20 * s + arc_r],
                start=start, end=end,
                fill=wave_color, width=lw,
            )

    # Downsample with high-quality filter
    return img.resize((size, size), Image.LANCZOS)


def _save_ico(images: list, path: Path) -> None:
    """Write a proper multi-resolution ICO file manually.

    Pillow's ICO save with append_images silently drops extra frames
    in many versions, so we construct the binary ourselves.
    """
    count = len(images)
    # ICO header: reserved(2) + type(2) + image_count(2)
    header = struct.pack("<HHH", 0, 1, count)

    dir_size = 16 * count
    data_offset = 6 + dir_size

    entries: list[bytes] = []
    blobs: list[bytes] = []

    for img in images:
        img = img.convert("RGBA")
        w, h = img.size
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()

        bw = 0 if w >= 256 else w
        bh = 0 if h >= 256 else h
        entries.append(struct.pack("<BBBBHHII",
            bw, bh, 0, 0, 1, 32, len(data), data_offset))
        blobs.append(data)
        data_offset += len(data)

    with open(path, "wb") as f:
        f.write(header)
        for e in entries:
            f.write(e)
        for b in blobs:
            f.write(b)


def create_icon(path: Path, bell_color: str, bg_color: str) -> None:
    """Create a multi-resolution .ico file."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [_render_bell(sz, bell_color, bg_color) for sz in sizes]
    _save_ico(images, path)
    print(f"Created: {path}  ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    assets = Path(__file__).parent.parent / "assets"
    assets.mkdir(exist_ok=True)

    # Server: red bell on dark blue
    create_icon(assets / "alarm_server.ico",
                bell_color="#e94560", bg_color="#16213e")
    # Client: teal/green bell on dark blue
    create_icon(assets / "alarm_client.ico",
                bell_color="#00b894", bg_color="#16213e")
    # Generic (installer exe): red bell on dark blue
    create_icon(assets / "alarm.ico",
                bell_color="#e94560", bg_color="#16213e")
