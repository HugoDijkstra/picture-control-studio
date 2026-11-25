#!/usr/bin/env python3
"""Picture Control Studio — native (GTK 4 / libadwaita) editor for Nikon Custom Picture Controls.

Edits recipes.toml next to this file (autosaved, with undo/redo), previews looks on photos
(D810 NEFs are developed with LibRaw's dcraw_emu), compares all looks at once, exports
finished JPEGs and a camera-settings cheat sheet, and installs .NCP files onto the SD card.
"""
import copy, datetime, io, math, os, pathlib, re, shutil, struct, subprocess, sys, threading, time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402
import cairo  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageFilter, ImageOps  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)            # running from a packaged release (PyInstaller)
BUNDLE = pathlib.Path(getattr(sys, "_MEIPASS", HERE))
sys.path.insert(0, str(HERE))
import ncp  # noqa: E402

APP_ID = "local.PictureControlStudio"
VERSION = "2.0"


def _recipes_path():
    """Source checkout: recipes.toml next to this file. Packaged app: the user's config folder,
    seeded with the bundled recipes on first run."""
    if os.environ.get("PCS_RECIPES"):
        return pathlib.Path(os.environ["PCS_RECIPES"])
    if not FROZEN:
        return HERE / "recipes.toml"
    cfg = pathlib.Path(GLib.get_user_config_dir()) / "picture-control-studio"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "recipes.toml"
    if not path.exists() and (BUNDLE / "recipes.toml").exists():
        shutil.copy(BUNDLE / "recipes.toml", path)
    return path


RECIPES = _recipes_path()
PREVIEW_MAX = 1400
THUMB_MAX = 360
PHOTO_EXT = (".nef", ".jpg", ".jpeg", ".png", ".tif", ".tiff")
BASES = ["standard", "neutral", "vivid", "flat", "monochrome", "portrait", "landscape"]
FILTERS = ["off", "yellow", "orange", "red", "green"]
TONINGS = ["bw", "sepia", "cyanotype", "red", "yellow", "green", "blue-green", "blue", "purple-blue", "red-purple"]
TONING_LABELS = ["B&W", "Sepia", "Cyanotype", "Red", "Yellow", "Green", "Blue green", "Blue", "Purple blue", "Red purple"]
WB_PRESETS = ["auto", "daylight", "cloudy", "shade", "incandescent", "fluorescent", "flash", "kelvin"]
PRESET_CURVES = {
    "Linear": [[0, 0], [255, 255]],
    "S-curve": [[0, 0], [64, 52], [128, 128], [192, 204], [255, 255]],
    "Fade": [[0, 24], [64, 70], [128, 128], [192, 188], [255, 238]],
}

# ================================================================ preview maths
# Approximate Nikon base tone curves, measured against D810 camera JPEGs. Applied to RAW only:
# JPEGs already have the camera's tone baked in.
BASE_TONE = {
    "neutral": [[0, 0], [24, 21], [56, 70], [88, 120], [120, 158], [152, 192], [200, 232], [255, 255]],
    "standard": [[0, 0], [24, 18], [56, 76], [88, 130], [120, 167], [152, 202], [200, 239], [255, 255]],
    "vivid": [[0, 0], [24, 16], [56, 78], [88, 136], [120, 174], [152, 210], [200, 244], [255, 255]],
    "portrait": [[0, 0], [24, 20], [56, 74], [88, 126], [120, 164], [152, 198], [200, 236], [255, 255]],
}
BASE_TONE["landscape"] = BASE_TONE["monochrome"] = BASE_TONE["standard"]
BASE_TONE["flat"] = [[0, 0], [24, 26], [56, 72], [88, 114], [120, 148], [152, 180], [200, 222], [255, 250]]
BASE_SAT = {"standard": 1.0, "neutral": 0.86, "vivid": 1.3, "portrait": 0.92, "landscape": 1.12, "flat": 0.72}
FILTER_W = {"off": (.299, .587, .114), "yellow": (.42, .53, .05), "orange": (.55, .42, .03),
            "red": (.7, .3, 0), "green": (.22, .72, .06)}
TINT = {"sepia": (1, .87, .68), "cyanotype": (.68, .84, 1), "red": (1, .72, .72), "yellow": (1, .97, .68),
        "green": (.78, 1, .78), "blue-green": (.7, .98, .92), "blue": (.75, .82, 1),
        "purple-blue": (.84, .76, 1), "red-purple": (1, .74, .9)}
LUMA = np.array([.299, .587, .114], np.float32)
_curve_cache = {}


def curve_table(points):
    key = tuple(tuple(p) for p in points)
    if key not in _curve_cache:
        if len(_curve_cache) > 256:
            _curve_cache.clear()
        f = ncp.spline(points)
        _curve_cache[key] = np.array([f(i) for i in range(256)], np.float32)
    return _curve_cache[key]


_table_cache = {}


def recipe_table(r):
    """Exactly the tone curve written to the .NCP (incl. gamma / imported tables), as 256 floats 0..255."""
    verbatim = bool(r.get("lut")) and r.get("lut_sig") == ncp.lut_signature(r)
    key = (ncp.lut_signature(r), hash(tuple(r["lut"])) if verbatim else None)
    if key not in _table_cache:
        if len(_table_cache) > 256:
            _table_cache.clear()
        lut = np.array(ncp.tone_lut(r), np.float32) * (255 / 32767)
        _table_cache[key] = lut[np.rint(np.arange(256) * 256 / 255).astype(int)]
    return _table_cache[key]


def colour_matrix(base, saturation, hue):
    sat = BASE_SAT.get(base, 1.0) * (1 + 0.16 * (0 if str(saturation) == "auto" else int(saturation)))
    a = -int(hue) * 3 * math.pi / 180
    c, s = math.cos(a), math.sin(a)
    H = np.array([
        [.213 + c * .787 - s * .213, .715 - c * .715 - s * .715, .072 - c * .072 + s * .928],
        [.213 - c * .213 + s * .143, .715 + c * .285 + s * .140, .072 - c * .072 - s * .283],
        [.213 - c * .213 - s * .787, .715 - c * .715 + s * .715, .072 + c * .928 + s * .072]], np.float32)
    S = sat * np.eye(3, dtype=np.float32) + (1 - sat) * np.tile(LUMA, (3, 1))
    return S @ H


def wb_gains(r):
    """Display-space channel gains for the WB fine-tune (A-B, G-M)."""
    ab, gm = float(r.get("wb_ab", 0) or 0), float(r.get("wb_gm", 0) or 0)
    g = np.array([1.025 ** ab, 1.03 ** gm, 1.025 ** -ab], np.float32)
    g /= float(g @ LUMA)
    return g ** (1 / 2.2)


def luma_blur(src):
    """Large-radius blur of the luma, used for Clarity."""
    lum = Image.fromarray(np.clip(src @ LUMA, 0, 255).astype(np.uint8), "L")
    return np.asarray(lum.filter(ImageFilter.GaussianBlur(max(src.shape[:2]) * 0.012)), np.float32)


def render(src, r, raw_source, blur=None):
    """src: HxWx3 float32 0..255 (display-encoded). Returns HxWx3 uint8."""
    img = src
    gains = wb_gains(r)
    if not np.allclose(gains, 1):
        img = img * gains
    clarity = int(r.get("clarity", 0) or 0)
    if clarity and blur is not None:
        lum = img @ LUMA
        detail = (lum - blur) * (clarity * 0.12)
        mid = 1 - np.abs(lum / 127.5 - 1) ** 2  # protect deep shadows and highlights
        img = img + (detail * np.clip(mid, 0, 1))[..., None]
    lut = recipe_table(r)
    if raw_source:  # base tone first, then the recipe curve
        lut = np.interp(curve_table(BASE_TONE.get(r["base"], BASE_TONE["standard"])), np.arange(256), lut)
    lut8 = np.clip(np.rint(lut), 0, 255).astype(np.uint8)
    if r["base"] == "monochrome":
        y = img @ np.array(FILTER_W.get(r["filter"], FILTER_W["off"]), np.float32)
        lum = lut8[np.clip(y, 0, 255).astype(np.uint8)].astype(np.float32)
        t = TINT.get(r["toning"]) if r["toning"] != "bw" else None
        k = int(r["toning_strength"]) / 7 if t else 0
        if not t:
            return np.repeat(lum[..., None], 3, axis=2).astype(np.uint8)
        # toning shows in shadows and mid-tones; paper white stays nearly white
        amt = (k * (1 - 0.85 * (lum / 255) ** 2))[..., None]
        tint = np.array(t, np.float32) - 1
        return np.clip(lum[..., None] * (1 + tint * amt), 0, 255).astype(np.uint8)
    out = img @ colour_matrix(r["base"], r["saturation"], r["hue"]).T
    return lut8[np.clip(out, 0, 255).astype(np.uint8)]


def base_only(src, base, raw_source):
    if not raw_source:
        return np.clip(src, 0, 255).astype(np.uint8)
    bt = np.clip(np.rint(curve_table(BASE_TONE.get(base, BASE_TONE["standard"]))), 0, 255).astype(np.uint8)
    return bt[np.clip(src, 0, 255).astype(np.uint8)]


def srgb_encode(lin):
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.power(lin, 1 / 2.4) - 0.055) * 255


def shrink(arr, max_side=PREVIEW_MAX):
    h, w = arr.shape[:2]
    f = max(1, math.ceil(max(h, w) / max_side))
    if f == 1:
        return arr.astype(np.float32)
    h2, w2 = h // f, w // f
    return arr[:h2 * f, :w2 * f].astype(np.float32).reshape(h2, f, w2, f, 3).mean(axis=(1, 3))


def thumbnail(src, size=THUMB_MAX):
    im = Image.fromarray(np.clip(src, 0, 255).astype(np.uint8))
    im.thumbnail((size, size), Image.BILINEAR)
    return np.asarray(im, np.float32)


# ================================================================ recipe helpers
def migrate_camera(r):
    """Fill wb / wb_ab / wb_gm / clarity from free-text notes like 'WB daylight, A2 M1; Clarity +1'."""
    if "wb" in r:
        return r
    n = r.get("notes", "") or ""
    r["wb"], ab, gm = "auto", 0.0, 0.0
    m = re.search(r"\bWB\s+(auto|daylight|cloudy|shade|incandescent|fluorescent|flash|(\d{4,5})\s*K)\b", n, re.I)
    if m:
        if m.group(2):
            r["wb"], r["wb_kelvin"] = "kelvin", int(m.group(2))
        else:
            r["wb"] = m.group(1).lower()
    for letter, v in re.findall(r"\b([ABGM])(\d+(?:\.\d+)?)\b", n):
        v = float(v)
        ab += v if letter == "A" else -v if letter == "B" else 0
        gm += v if letter == "G" else -v if letter == "M" else 0
    r["wb_ab"], r["wb_gm"] = ab, gm
    m = re.search(r"Clarity\s*([+\-−]?\d+)", n, re.I)
    r["clarity"] = int(m.group(1).replace("−", "-")) if m else 0
    return r


def fmt_ab(v):
    v = float(v or 0)
    return "0" if v == 0 else f"A{v:g}" if v > 0 else f"B{-v:g}"


def fmt_gm(v):
    v = float(v or 0)
    return "0" if v == 0 else f"G{v:g}" if v > 0 else f"M{-v:g}"


def fmt_wb(r):
    wb = r.get("wb", "auto")
    return f"{int(r.get('wb_kelvin', 5000))} K" if wb == "kelvin" else wb.title()


def fmt_signed(v):
    if str(v) == "auto":
        return "Auto"
    v = float(v)
    return "0" if v == 0 else f"{v:+g}"


# ================================================================ NEF metadata / decoding
def _ifd(d, off, le, base=0):
    fmt16, fmt32 = ("<H", "<I") if le else (">H", ">I")
    u16 = lambda o: struct.unpack_from(fmt16, d, o)[0]
    u32 = lambda o: struct.unpack_from(fmt32, d, o)[0]
    tags, n = {}, u16(base + off)
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 13: 4}
    for i in range(n):
        e = base + off + 2 + 12 * i
        tag, typ, cnt = u16(e), u16(e + 2), u32(e + 4)
        at = e + 8 if sizes.get(typ, 1) * cnt <= 4 else base + u32(e + 8)
        if typ == 3:
            v = [u16(at + 2 * k) for k in range(min(cnt, 64))]
        elif typ in (4, 13):
            v = [u32(at + 4 * k) for k in range(min(cnt, 64))]
        else:
            v = d[at:at + cnt]
        tags[tag] = v
    return tags


def nef_info(path):
    d = pathlib.Path(path).read_bytes()
    info = {"orientation": 1, "model": "", "pc": "", "pc_base": "", "jpeg": None, "width": 0, "height": 0}
    try:
        le = d[:2] == b"II"
        t0 = _ifd(d, struct.unpack_from("<I" if le else ">I", d, 4)[0], le)
        info["orientation"] = t0.get(0x112, [1])[0]
        info["model"] = bytes(t0.get(0x110, b"")).split(b"\0")[0].decode(errors="replace")
        best = None
        for sub in [t0] + [_ifd(d, o, le) for o in t0.get(0x14A, [])]:
            if 0x201 in sub and 0x202 in sub and (not best or sub[0x202][0] > best[1]):
                best = (sub[0x201][0], sub[0x202][0])
            if sub.get(0xFE, [1])[0] == 0 and 0x100 in sub:
                info["width"], info["height"] = sub[0x100][0], sub[0x101][0]
        if best:
            info["jpeg"] = d[best[0]:best[0] + best[1]]
        ex = _ifd(d, t0[0x8769][0], le)
        mn = ex.get(0x927C)
        if mn is not None:
            mo = d.find(bytes(mn[:10]))
            if d[mo:mo + 5] == b"Nikon":
                base = mo + 10
                mle = d[base:base + 2] == b"II"
                mt = _ifd(d, struct.unpack_from("<I" if mle else ">I", d, base + 4)[0], mle, base)
                pc = bytes(mt.get(0x23, b""))
                if len(pc) >= 44:
                    info["pc"] = pc[4:24].split(b"\0")[0].decode(errors="replace").strip()
                    info["pc_base"] = pc[24:44].split(b"\0")[0].decode(errors="replace").strip()
    except Exception:
        pass
    if not info["jpeg"]:  # fall back: largest embedded JPEG
        best = (0, 0)
        for m in re.finditer(rb"\xff\xd8\xff", d):
            e = d.find(b"\xff\xd9", m.start())
            if e > 0 and e - m.start() > best[1] - best[0]:
                best = (m.start(), e + 2)
        info["jpeg"] = d[best[0]:best[1]] or None
    return info


TRANSPOSE = {2: [Image.FLIP_LEFT_RIGHT], 3: [Image.ROTATE_180], 4: [Image.FLIP_TOP_BOTTOM],
             5: [Image.TRANSPOSE], 6: [Image.ROTATE_270], 7: [Image.TRANSVERSE], 8: [Image.ROTATE_90]}


def dcraw(path, half=True):
    """Run LibRaw: camera white balance, sRGB primaries, linear 16-bit. Returns HxWx3 uint16."""
    exe = next((str(p) for p in (BUNDLE / "bin" / "dcraw_emu", BUNDLE / "bin" / "dcraw_emu.exe") if p.exists()), None) \
        or shutil.which("dcraw_emu")
    if not exe:
        raise RuntimeError("LibRaw's dcraw_emu isn't installed (e.g. pacman -S libraw / apt install libraw-bin)")
    args = [exe] + (["-h"] if half else []) + ["-w", "-o", "1", "-4", "-Z", "-", str(path)]
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # no console window on Windows
    p = subprocess.run(args, capture_output=True, timeout=300, **kw)
    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", p.stdout[:64])
    if p.returncode or not m:
        raise RuntimeError(p.stderr.decode(errors="replace").strip() or "LibRaw couldn't decode this file")
    w, h = int(m.group(1)), int(m.group(2))
    return np.frombuffer(p.stdout, ">u2", count=w * h * 3, offset=m.end()).reshape(h, w, 3)


def decode_raw(path):
    return shrink(dcraw(path, half=True)) / 65535.0


def open_image(path, max_side=None):
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def jpeg_from_bytes(data, orientation, max_side=None):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    for op in TRANSPOSE.get(orientation, []):
        im = im.transpose(op)
    if max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def texture(arr):
    h, w = arr.shape[:2]
    arr = np.ascontiguousarray(arr, dtype=np.uint8)
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(arr.tobytes()), w * 3)


# ================================================================ SD cards
def find_cards():
    """Mounted volumes that look like a camera card: [(name, path, Gio.Mount)]."""
    cards = []
    for m in Gio.VolumeMonitor.get().get_mounts():
        root = m.get_root().get_path()
        if root and any((pathlib.Path(root) / d).is_dir() for d in ("DCIM", "NIKON")):
            cards.append((m.get_name(), root, m))
    return cards


def unmounted_removable_volumes():
    out = []
    for v in Gio.VolumeMonitor.get().get_volumes():
        drv = v.get_drive()
        if v.get_mount() is None and v.can_mount() and drv and (drv.is_removable() or drv.is_media_removable()):
            out.append(v)
    return out


def card_pc_dir(root):
    p = pathlib.Path(root) / "NIKON" / "CUSTOMPC"
    return p if p.is_dir() else None


# ================================================================ curve editor
class CurveEditor(Gtk.DrawingArea):
    PAD = 14

    def __init__(self, on_change, on_begin):
        super().__init__(focusable=True, hexpand=True, vexpand=True)
        self.set_size_request(260, 260)
        self.on_change, self.on_begin = on_change, on_begin
        self.points = [[0, 0], [255, 255]]
        self.hist = None
        self.table = None  # actual curve (with gamma) from the window; falls back to the plain spline
        self.sel = self.hover = -1
        self._drag_start = None
        self.readout = None
        self.set_draw_func(self.draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.drag_begin)
        drag.connect("drag-update", self.drag_update)
        drag.connect("drag-end", lambda *a: self.on_change(final=True))
        self.add_controller(drag)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self.clicked)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda c, x, y: self._set_hover(self.hit(x, y)))
        motion.connect("leave", lambda *a: self._set_hover(-1))
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.key)
        self.add_controller(keys)

    def set_points(self, pts):
        self.points = [list(p) for p in pts]
        self.sel = min(self.sel, len(self.points) - 1)
        self.queue_draw()
        self.update_readout()

    def _box(self):
        w, h = self.get_width(), self.get_height()
        side = min(w, h) - 2 * self.PAD
        return (w - side) / 2, (h - side) / 2, side

    def to_px(self, x, y):
        ox, oy, s = self._box()
        return ox + x / 255 * s, oy + (1 - y / 255) * s

    def from_px(self, px, py):
        ox, oy, s = self._box()
        return round((px - ox) / s * 255), round((1 - (py - oy) / s) * 255)

    def hit(self, px, py):
        best, bd = -1, 11
        for i, (x, y) in enumerate(self.points):
            qx, qy = self.to_px(x, y)
            d = math.hypot(qx - px, qy - py)
            if d < bd:
                best, bd = i, d
        return best

    def move(self, i, x, y):
        lo = self.points[i - 1][0] + 1 if i > 0 else 0
        hi = self.points[i + 1][0] - 1 if i < len(self.points) - 1 else 255
        self.points[i] = [max(lo, min(hi, int(x))), max(0, min(255, int(y)))]

    def drag_begin(self, g, x, y):
        self.grab_focus()
        i = self.hit(x, y)
        self._drag_start = None
        if i < 0:
            if len(self.points) >= 20:
                return
            px, py = self.from_px(x, y)
            if not 0 <= px <= 255 or any(p[0] == px for p in self.points):
                return
            self.on_begin()
            i = next((k for k, p in enumerate(self.points) if p[0] > px), len(self.points))
            self.points.insert(i, [px, max(0, min(255, py))])
        else:
            self.on_begin()
        self.sel = i
        self._drag_start = (x, y)
        self.changed()

    def drag_update(self, g, dx, dy):
        if self._drag_start is None or self.sel < 0:
            return
        x, y = self.from_px(self._drag_start[0] + dx, self._drag_start[1] + dy)
        self.move(self.sel, x, y)
        self.changed()

    def clicked(self, g, n, x, y):
        i = self.hit(x, y)
        if i >= 0 and (n == 2 or g.get_current_button() == 3):
            self.remove(i)

    def remove(self, i):
        if len(self.points) <= 2:
            return
        self.on_begin()
        del self.points[i]
        self.sel = -1
        self.changed(final=True)

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.set_cursor_from_name("grab" if i >= 0 else "crosshair")
            self.queue_draw()
            self.update_readout()

    def key(self, c, keyval, code, state):
        if self.sel < 0:
            return False
        st = 5 if state & Gdk.ModifierType.SHIFT_MASK else 1
        mv = {Gdk.KEY_Left: (-st, 0), Gdk.KEY_Right: (st, 0), Gdk.KEY_Up: (0, st), Gdk.KEY_Down: (0, -st)}
        if keyval in mv:
            self.on_begin("curve-key")
            x, y = self.points[self.sel]
            self.move(self.sel, x + mv[keyval][0], y + mv[keyval][1])
            self.changed(final=True)
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            self.remove(self.sel)
            return True
        if keyval == Gdk.KEY_Tab:
            self.sel = (self.sel + 1) % len(self.points)
            self.queue_draw()
            self.update_readout()
            return True
        return False

    def changed(self, final=False):
        self.queue_draw()
        self.update_readout()
        self.on_change(final=final)

    def update_readout(self):
        if not self.readout:
            return
        i = self.sel if self.sel >= 0 else self.hover
        if 0 <= i < len(self.points):
            x, y = self.points[i]
            self.readout.set_text(f"Point {i + 1}/{len(self.points)} · in {x} → out {y}")
        else:
            self.readout.set_text(f"{len(self.points)}/20 points · click to add · drag · double-click removes")

    def draw(self, area, cr, w, h):
        sm = Adw.StyleManager.get_default()
        dark = sm.get_dark()
        fg = (0.93, 0.92, 0.91) if dark else (0.11, 0.11, 0.10)
        acc = sm.get_accent_color_rgba() if hasattr(sm, "get_accent_color_rgba") else None
        accent = (acc.red, acc.green, acc.blue) if acc else (0.21, 0.52, 0.89)
        ox, oy, s = self._box()
        cr.set_source_rgba(*fg, 0.04)
        cr.rectangle(ox, oy, s, s)
        cr.fill()
        if self.hist is not None:
            mx = max(self.hist[2:254].max(), 1)
            cr.set_source_rgba(*accent, 0.18)
            cr.move_to(ox, oy + s)
            for i in range(256):
                cr.line_to(ox + i / 255 * s, oy + s - min(1, self.hist[i] / mx) * s * 0.9)
            cr.line_to(ox + s, oy + s)
            cr.close_path()
            cr.fill()
        cr.set_source_rgba(*fg, 0.10)
        cr.set_line_width(1)
        for i in range(5):
            v = i / 4 * s
            cr.move_to(ox + v, oy)
            cr.line_to(ox + v, oy + s)
            cr.move_to(ox, oy + v)
            cr.line_to(ox + s, oy + v)
        cr.stroke()
        cr.set_dash([4, 4])
        cr.move_to(ox, oy + s)
        cr.line_to(ox + s, oy)
        cr.stroke()
        cr.set_dash([])
        if ncp.validate({"name": "x", "slot": 1, "curve": self.points}):
            return
        f = ncp.spline(self.points)
        table = self.table
        cr.set_source_rgb(*fg)
        cr.set_line_width(2)
        for x in range(256):
            px, py = self.to_px(x, table[x] if table is not None else f(x))
            (cr.line_to if x else cr.move_to)(px, py)
        cr.stroke()
        for i, (x, y) in enumerate(self.points):
            px, py = self.to_px(x, y)
            cr.arc(px, py, 6.5 if i == self.sel else 5, 0, 2 * math.pi)
            cr.set_source_rgb(*(accent if i == self.sel else (0.12, 0.12, 0.12) if dark else (1, 1, 1)))
            cr.fill_preserve()
            cr.set_source_rgb(*(accent if i in (self.sel, self.hover) else fg))
            cr.set_line_width(2)
            cr.stroke()


# ================================================================ small widget helpers
def scale_row(title, lo, hi, step=1, digits=0, marks=1):
    row = Adw.ActionRow(title=title)
    sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
    sc.set_digits(digits)
    sc.set_round_digits(digits)
    sc.set_draw_value(False)
    sc.set_hexpand(True)
    sc.set_size_request(150, -1)
    if marks:
        v = lo
        while v <= hi + 1e-9:
            sc.add_mark(v, Gtk.PositionType.BOTTOM, None)
            v += marks
    val = Gtk.Label(width_chars=5, xalign=1)
    val.add_css_class("numeric")
    row.add_suffix(sc)
    row.add_suffix(val)
    return row, sc, val


def caption(text="", dim=True, **kw):
    lab = Gtk.Label(label=text, xalign=kw.pop("xalign", 0), **kw)
    lab.add_css_class("caption")
    if dim:
        lab.add_css_class("dim-label")
    return lab


# ================================================================ main window
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Picture Control Studio", default_width=1480, default_height=920)
        self.app = app
        self.recipes, self.sel, self.loading = [], 0, False
        self.history, self.future, self._cp = [], [], (None, 0.0)
        self.src = self.raw_lin = self.jpeg_src = self.image_src = None
        self.nef, self.mode, self.exposure = None, "none", 0.0
        self.photo_path, self.siblings, self.group_mode = None, [], False
        self.compare, self.split = False, 0.5
        self._blur = (None, None)
        self._save_id = self._render_id = self._looks_id = 0
        self.looks_dirty = True

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(self.build_header())
        body = Gtk.Box()
        body.append(self.build_sidebar())
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        paned = Gtk.Paned(hexpand=True, shrink_start_child=False, shrink_end_child=False)
        paned.set_start_child(self.build_editor())
        paned.set_end_child(self.build_preview())
        paned.set_position(500)
        body.append(paned)
        self.toasts.set_child(body)
        view.set_content(self.toasts)
        self.set_content(view)
        self.install_actions()
        self.load_recipes()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *a: self.curve.queue_draw())

    # ------------------------------------------------------------ layout
    def build_header(self):
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Picture Control Studio", subtitle="")
        header.set_title_widget(self.title)
        open_btn = Gtk.Button(label="Open…", tooltip_text="Import .NCP / .NP2 files or a recipes.toml")
        open_btn.connect("clicked", lambda *a: self.on_open())
        header.pack_start(open_btn)
        ur = Gtk.Box()
        ur.add_css_class("linked")
        ur.append(Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Undo (Ctrl+Z)", action_name="win.undo"))
        ur.append(Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text="Redo (Ctrl+Shift+Z)", action_name="win.redo"))
        header.pack_start(ur)

        inst = Gtk.Button(label="Install to SD card", action_name="win.install",
                          tooltip_text="Write the ticked recipes to NIKON/CUSTOMPC on the camera card (Ctrl+I)")
        inst.add_css_class("suggested-action")
        header.pack_end(inst)
        menu = Gio.Menu()
        sec1 = Gio.Menu()
        sec1.append("Load Photo…", "win.photo")
        sec1.append("Export Photo with Look…", "win.export-photo")
        sec1.append("Export Selected Photos with Look…", "win.export-batch")
        menu.append_section(None, sec1)
        sec2 = Gio.Menu()
        sec2.append("Card Contents…", "win.card")
        sec2.append("Export Recipes to Folder…", "win.export")
        sec2.append("Export Current Picture Control File…", "win.export-one")
        sec2.append("Camera Settings Cheat Sheet (PDF)…", "win.cheatsheet")
        menu.append_section(None, sec2)
        sec3 = Gio.Menu()
        sec3.append("Open Recipes Folder", "win.reveal")
        sec3.append("Keyboard Shortcuts", "win.shortcuts")
        sec3.append("About Picture Control Studio", "win.about")
        menu.append_section(None, sec3)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu",
                                       primary=True))
        return header

    def build_sidebar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=260)
        bar = Gtk.Box(spacing=4, margin_top=8, margin_start=8, margin_end=8)
        for icon, tip, act in [("list-add-symbolic", "New recipe (Ctrl+N)", "win.new"),
                               ("edit-copy-symbolic", "Duplicate (Ctrl+D)", "win.duplicate"),
                               ("user-trash-symbolic", "Delete", "win.delete")]:
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=act)
            b.add_css_class("flat")
            bar.append(b)
        bar.append(Gtk.Box(hexpand=True))
        allb = Gtk.Button(label="All", tooltip_text="Tick / untick all for installing")
        allb.add_css_class("flat")
        allb.connect("clicked", lambda *a: self.on_all())
        bar.append(allb)
        box.append(bar)
        self.search = Gtk.SearchEntry(placeholder_text="Search recipes", margin_start=8, margin_end=8, margin_top=6,
                                      margin_bottom=4)
        self.search.connect("search-changed", lambda *a: self.listbox.invalidate_filter())
        box.append(self.search)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self.on_row_selected)
        self.listbox.set_filter_func(self.filter_row)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.listbox)
        box.append(sw)
        self.foot = caption(wrap=True, margin_top=6, margin_bottom=8, margin_start=12, margin_end=12)
        box.append(self.foot)
        return box

    def build_editor(self):
        outer = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, width_request=420)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12, margin_bottom=18,
                      margin_start=16, margin_end=16)
        self.curve = CurveEditor(self.on_curve_changed, self.on_curve_begin)
        frame = Gtk.AspectFrame(ratio=1, obey_child=False, child=self.curve)
        frame.set_size_request(-1, 330)
        col.append(frame)
        cbar = Gtk.Box(spacing=6)
        self.curve.readout = caption(hexpand=True, ellipsize=3)
        cbar.append(self.curve.readout)
        pres = Gtk.Box()
        pres.add_css_class("linked")
        for name, pts in PRESET_CURVES.items():
            b = Gtk.Button(label=name)
            b.connect("clicked", lambda _b, p=pts: self.set_field("curve", [list(x) for x in p]))
            pres.append(b)
        cbar.append(pres)
        col.append(cbar)

        g = Adw.PreferencesGroup(title="Picture Control", description="Written to the file")
        self.g_pc = g
        self.e_name = Adw.EntryRow(title="Name (shown on the camera, max 19)")
        if hasattr(self.e_name, "set_max_length"):
            self.e_name.set_max_length(19)
        self.e_name.connect("changed", lambda e: self.set_field("name", e.get_text()))
        self.e_slot = Adw.SpinRow.new_with_range(1, 99, 1)
        self.e_slot.set_title("Slot")
        self.e_slot.connect("notify::value", lambda r, _p: self.set_field("slot", int(r.get_value())))
        self.e_base = Adw.ComboRow(title="Base Picture Control", model=Gtk.StringList.new([b.title() for b in BASES]))
        self.e_base.connect("notify::selected", lambda r, _p: self.set_field("base", BASES[r.get_selected()]))
        self.r_gamma, self.s_gamma, self.v_gamma = scale_row("Gamma", 0.5, 2.0, 0.01, 2, marks=0.25)
        self.r_gamma.set_subtitle("Bends the whole curve: above 1 opens the mid-tones")
        self.s_gamma.connect("value-changed", lambda s: self.set_field("gamma", round(s.get_value(), 2)))
        self.r_clar, self.s_clar, self.v_clar = scale_row("Clarity", -5, 5)
        self.s_clar.connect("value-changed", lambda s: self.set_field("clarity", int(s.get_value())))
        self.r_sharp, self.s_sharp, self.v_sharp = scale_row("Sharpening", 0, 9)
        self.a_sharp = Gtk.ToggleButton(label="Auto", valign=Gtk.Align.CENTER)
        self.r_sharp.add_suffix(self.a_sharp)
        self.s_sharp.connect("value-changed", lambda s: self.set_field("sharpening", int(s.get_value())))
        self.a_sharp.connect("toggled", lambda b: self.set_field("sharpening", "auto" if b.get_active() else int(self.s_sharp.get_value())))
        self.r_sat, self.s_sat, self.v_sat = scale_row("Saturation", -3, 3)
        self.a_sat = Gtk.ToggleButton(label="Auto", valign=Gtk.Align.CENTER)
        self.r_sat.add_suffix(self.a_sat)
        self.s_sat.connect("value-changed", lambda s: self.set_field("saturation", int(s.get_value())))
        self.a_sat.connect("toggled", lambda b: self.set_field("saturation", "auto" if b.get_active() else int(self.s_sat.get_value())))
        self.r_hue, self.s_hue, self.v_hue = scale_row("Hue", -3, 3)
        self.r_hue.set_subtitle("+ pushes reds toward yellow")
        self.s_hue.connect("value-changed", lambda s: self.set_field("hue", int(s.get_value())))
        self.e_filter = Adw.ComboRow(title="Filter effect", model=Gtk.StringList.new([f.title() for f in FILTERS]))
        self.e_filter.connect("notify::selected", lambda r, _p: self.set_field("filter", FILTERS[r.get_selected()]))
        self.e_toning = Adw.ComboRow(title="Toning", model=Gtk.StringList.new(TONING_LABELS))
        self.e_toning.connect("notify::selected", lambda r, _p: self.set_field("toning", TONINGS[r.get_selected()]))
        self.r_str, self.s_str, self.v_str = scale_row("Toning strength", 1, 7)
        self.s_str.connect("value-changed", lambda s: self.set_field("toning_strength", int(s.get_value())))
        for w in (self.e_name, self.e_slot, self.e_base, self.r_gamma, self.r_sharp, self.r_clar, self.r_sat, self.r_hue,
                  self.e_filter, self.e_toning, self.r_str):
            g.add(w)
        col.append(g)

        g2 = Adw.PreferencesGroup(title="On the Camera",
                                  description="White balance can't be stored in a Picture Control — set it when you "
                                              "pick the look. The preview simulates the fine-tune.")
        self.e_wb = Adw.ComboRow(title="White balance", model=Gtk.StringList.new([w.title() for w in WB_PRESETS]))
        self.e_wb.connect("notify::selected", lambda r, _p: self.set_field("wb", WB_PRESETS[r.get_selected()]))
        self.e_kelvin = Adw.SpinRow.new_with_range(2500, 10000, 100)
        self.e_kelvin.set_title("Colour temperature (K)")
        self.e_kelvin.connect("notify::value", lambda r, _p: self.set_field("wb_kelvin", int(r.get_value())))
        self.r_ab, self.s_ab, self.v_ab = scale_row("Fine-tune  B ← → A", -6, 6, 0.5, 1, marks=1)
        self.s_ab.connect("value-changed", lambda s: self.set_field("wb_ab", round(s.get_value() * 2) / 2))
        self.r_gm, self.s_gm, self.v_gm = scale_row("Fine-tune  M ← → G", -6, 6, 0.25, 2, marks=1)
        self.s_gm.connect("value-changed", lambda s: self.set_field("wb_gm", round(s.get_value() * 4) / 4))
        self.e_notes = Adw.EntryRow(title="Notes")
        self.e_notes.connect("changed", lambda e: self.set_field("notes", e.get_text()))
        for w in (self.e_wb, self.e_kelvin, self.r_ab, self.r_gm, self.e_notes):
            g2.add(w)
        col.append(g2)
        self.errors = Gtk.Label(xalign=0, wrap=True)
        self.errors.add_css_class("error")
        col.append(self.errors)
        outer.set_child(col)
        return outer

    def build_preview(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=380)
        bar = Gtk.Box(spacing=8, margin_top=8, margin_bottom=4, margin_start=12, margin_end=12)
        self.pages = Gtk.Stack(vexpand=True, hexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        bar.append(Gtk.StackSwitcher(stack=self.pages))
        self.seg = Gtk.Box()
        self.seg.add_css_class("linked")
        self.b_raw = Gtk.ToggleButton(label="RAW", tooltip_text="Sensor data with a Nikon-like base tone — judges the recipe on its own")
        self.b_jpg = Gtk.ToggleButton(label="Camera JPEG", group=self.b_raw,
                                      tooltip_text="The JPEG inside the NEF — has the look you shot with baked in")
        self.b_raw.connect("toggled", lambda b: b.get_active() and self.set_mode("raw"))
        self.b_jpg.connect("toggled", lambda b: b.get_active() and self.set_mode("jpeg"))
        self.seg.append(self.b_raw)
        self.seg.append(self.b_jpg)
        bar.append(self.seg)
        self.b_compare = Gtk.ToggleButton(icon_name="view-dual-symbolic", tooltip_text="Before / after (Ctrl+B)")
        self.b_compare.connect("toggled", lambda b: self.set_compare(b.get_active()))
        bar.append(self.b_compare)
        bar.append(Gtk.Box(hexpand=True))
        self.spinner = Gtk.Spinner()
        bar.append(self.spinner)
        nav = Gtk.Box()
        nav.add_css_class("linked")
        nav.append(Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Previous photo (Alt+←)",
                              action_name="win.prev-photo"))
        nav.append(Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Next photo (Alt+→)",
                              action_name="win.next-photo"))
        bar.append(nav)
        bar.append(Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Load photos — select several to step through them (Ctrl+O)", action_name="win.photo"))
        box.append(bar)

        bar2 = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_bottom=4)
        self.exp_box = Gtk.Box(spacing=6)
        self.exp_box.append(caption("Exposure"))
        self.s_exp = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -2, 2, 0.1)
        self.s_exp.set_value(0)
        self.s_exp.set_size_request(150, -1)
        self.s_exp.set_draw_value(False)
        self.s_exp.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.v_exp = caption("0.0 EV", dim=False, width_chars=7)
        self.v_exp.add_css_class("numeric")
        self.s_exp.connect("value-changed", self.on_exposure)
        rst = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Reset exposure", valign=Gtk.Align.CENTER)
        rst.add_css_class("flat")
        rst.connect("clicked", lambda *a: self.s_exp.set_value(0))
        for w in (self.s_exp, self.v_exp, rst):
            self.exp_box.append(w)
        bar2.append(self.exp_box)
        self.info = caption(wrap=True, hexpand=True, xalign=1)
        bar2.append(self.info)
        box.append(bar2)

        # page 1: preview
        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        status = Adw.StatusPage(icon_name="camera-photo-symbolic", title="Drop a photo here",
                                description="NEF, JPEG, PNG or TIFF. NEFs are developed from the raw data, so the "
                                            "preview starts from a clean slate instead of the look you shot with.")
        pick = Gtk.Button(label="Choose Photo…", halign=Gtk.Align.CENTER, action_name="win.photo")
        pick.add_css_class("pill")
        pick.add_css_class("suggested-action")
        status.set_child(pick)
        self.stack.add_named(status, "empty")
        ov = Gtk.Overlay()
        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN, hexpand=True, vexpand=True,
                                   margin_start=12, margin_end=12, margin_bottom=8)
        ov.set_child(self.picture)
        self.badge = Gtk.Label(label="Original", halign=Gtk.Align.START, valign=Gtk.Align.START,
                               margin_start=24, margin_top=12, visible=False)
        self.badge.add_css_class("osd")
        self.badge.add_css_class("caption-heading")
        ov.add_overlay(self.badge)
        self.badge2 = Gtk.Label(label="Look", halign=Gtk.Align.END, valign=Gtk.Align.START,
                                margin_end=24, margin_top=12, visible=False)
        self.badge2.add_css_class("osd")
        self.badge2.add_css_class("caption-heading")
        ov.add_overlay(self.badge2)
        hold = Gtk.GestureClick()
        hold.connect("pressed", lambda *a: not self.compare and self.show_original(True))
        hold.connect("released", lambda *a: not self.compare and self.show_original(False))
        hold.connect("stopped", lambda *a: not self.compare and self.show_original(False))
        self.picture.add_controller(hold)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda g, x, y: self.drag_split(x))
        drag.connect("drag-update", lambda g, dx, dy: self.drag_split(g.get_start_point()[1] + dx))
        self.picture.add_controller(drag)
        self.stack.add_named(ov, "photo")
        p1 = self.pages.add_titled(self.stack, "preview", "Preview")
        p1.set_icon_name("image-x-generic-symbolic")

        # page 2: all looks
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=2,
                                max_children_per_line=6, row_spacing=12, column_spacing=12,
                                margin_start=12, margin_end=12, margin_top=8, margin_bottom=12,
                                valign=Gtk.Align.START)
        self.flow.connect("child-activated", self.on_look_activated)
        lsw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        lsw.set_child(self.flow)
        self.looks_stack = Gtk.Stack()
        self.looks_stack.add_named(Adw.StatusPage(icon_name="view-grid-symbolic", title="All looks",
                                                  description="Load a photo to see it with every recipe at once."), "empty")
        self.looks_stack.add_named(lsw, "grid")
        p2 = self.pages.add_titled(self.looks_stack, "looks", "All Looks")
        p2.set_icon_name("view-grid-symbolic")
        self.pages.connect("notify::visible-child-name", lambda *a: self.schedule_looks())
        box.append(self.pages)
        box.append(caption("Preview is an approximation — the camera's own rendering will differ. "
                           "Press and hold the photo to see the original.", wrap=True, margin_start=12, margin_bottom=8))
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        box.add_controller(drop)
        self.update_source_controls()
        return box

    def install_actions(self):
        acts = {
            "undo": (self.do_undo, ["<Control>z"]), "redo": (self.do_redo, ["<Control><Shift>z", "<Control>y"]),
            "new": (self.on_new, ["<Control>n"]), "duplicate": (self.on_dup, ["<Control>d"]),
            "delete": (self.on_delete, []), "find": (lambda: self.search.grab_focus(), ["<Control>f"]),
            "photo": (self.pick_photo, ["<Control>o"]), "install": (self.on_install, ["<Control>i"]),
            "export-photo": (self.on_export_photo, ["<Control>e"]), "export": (self.on_export, []),
            "export-batch": (self.on_export_batch, ["<Control><Shift>e"]),
            "export-one": (self.on_export_one, []), "cheatsheet": (self.on_cheatsheet, ["<Control>p"]),
            "card": (self.on_card_contents, []),
            "reveal": (lambda: Gio.AppInfo.launch_default_for_uri(RECIPES.parent.as_uri(), None), []),
            "compare": (lambda: self.b_compare.set_active(not self.b_compare.get_active()), ["<Control>b"]),
            "prev-photo": (lambda: self.step_photo(-1), ["<Alt>Left"]),
            "next-photo": (lambda: self.step_photo(1), ["<Alt>Right"]),
            "view-preview": (lambda: self.pages.set_visible_child_name("preview"), ["<Control>1"]),
            "view-looks": (lambda: self.pages.set_visible_child_name("looks"), ["<Control>2"]),
            "prev-recipe": (lambda: self.step_recipe(-1), ["<Control>Page_Up", "<Alt>Up"]),
            "next-recipe": (lambda: self.step_recipe(1), ["<Control>Page_Down", "<Alt>Down"]),
            "shortcuts": (self.on_shortcuts, ["<Control>question"]), "about": (self.on_about, []),
        }
        self.actions = {}
        for name, (cb, accels) in acts.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, f=cb: f())
            self.add_action(a)
            self.actions[name] = a
            if accels:
                self.app.set_accels_for_action(f"win.{name}", accels)
        self.update_undo_actions()
        self.update_nav_actions()

    # ------------------------------------------------------------ recipes & list
    def load_recipes(self):
        try:
            rs = ncp.load_all(RECIPES)
        except FileNotFoundError:
            rs = [{"slot": 1, "name": "New look"}]
        except Exception as e:
            rs = [{"slot": 1, "name": "New look"}]
            GLib.idle_add(self.toast, f"Couldn't read recipes.toml: {e}")
        migrated = any("wb" not in r for r in rs)
        self.recipes = [self._norm(r) for r in rs]
        self.rebuild_list()
        self.title.set_subtitle(f"{len(self.recipes)} recipes · recipes.toml")
        if migrated:
            self.schedule_save()

    @staticmethod
    def _norm(r):
        r = migrate_camera(ncp.with_defaults(r))
        r.setdefault("install", int(r.get("slot", 99)) <= 9)
        return r

    def cur(self):
        return self.recipes[self.sel]

    def all_errors(self):
        counts = {}
        for r in self.recipes:
            counts[r["slot"]] = counts.get(r["slot"], 0) + 1
        out = []
        for r in self.recipes:
            e = ncp.validate(r)
            if counts[r["slot"]] > 1:
                e.append(f"Slot {r['slot']} is used by more than one recipe.")
            out.append(e)
        return out

    def filter_row(self, row):
        q = self.search.get_text().strip().lower()
        if not q:
            return True
        i = row.get_index()
        if not 0 <= i < len(self.recipes):
            return True
        r = self.recipes[i]
        return q in f"{r['name']} {r.get('notes', '')} {r['base']}".lower()

    def rebuild_list(self):
        self.loading = True
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        self.rows = []
        for r in self.recipes:
            row = Gtk.ListBoxRow()
            b = Gtk.Box(spacing=8)
            cb = Gtk.CheckButton(active=bool(r.get("install")), tooltip_text="Install this one on the card")
            cb.connect("toggled", self.on_install_toggled, r)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            name = Gtk.Label(xalign=0, ellipsize=3)
            sub = caption(ellipsize=3)
            texts.append(name)
            texts.append(sub)
            meta = Gtk.Label(valign=Gtk.Align.CENTER)
            meta.add_css_class("dim-label")
            meta.add_css_class("numeric")
            b.append(cb)
            b.append(texts)
            b.append(meta)
            row.set_child(b)
            self.listbox.append(row)
            self.rows.append((row, name, sub, meta))
        self.sel = max(0, min(self.sel, len(self.recipes) - 1))
        self.listbox.select_row(self.rows[self.sel][0])
        self.loading = False
        self.refresh_list_labels()
        self.load_form()
        self.looks_dirty = True
        self.schedule_looks()

    def refresh_list_labels(self):
        errs = self.all_errors()
        for (row, name, sub, meta), r, e in zip(self.rows, self.recipes, errs):
            name.set_text(r["name"] or "(no name)")
            bits = [r["base"].title()]
            if r["base"] != "monochrome" and str(r["saturation"]) not in ("0", "auto"):
                bits.append(f"sat {fmt_signed(r['saturation'])}")
            if r.get("wb", "auto") != "auto":
                bits.append(fmt_wb(r))
            if r.get("clarity"):
                bits.append(f"clarity {fmt_signed(r['clarity'])}")
            if float(r.get("gamma", 1.0)) != 1.0:
                bits.append(f"γ {float(r['gamma']):.2f}")
            sub.set_text(" · ".join(bits))
            meta.set_markup("<span foreground='#e01b24'><b>!</b></span>" if e else f"{int(r['slot']):02d}")
            meta.set_tooltip_text(" ".join(e) if e else f"PICCON{int(r['slot']):02d}.NCP")
        n = sum(1 for r in self.recipes if r.get("install"))
        self.foot.set_text(f"{n} of {len(self.recipes)} ticked to install"
                           + (" — the D810 holds 9 at a time; you pick when copying" if n > 9 else ""))

    def on_row_selected(self, lb, row):
        if self.loading or row is None:
            return
        self.sel = row.get_index()
        self.curve.sel = -1
        self.load_form()
        if self.compare:
            self.badge2.set_label(self.cur()["name"])
        self.schedule_render()

    def step_recipe(self, d):
        visible = [i for i, (row, *_rest) in enumerate(self.rows) if row.get_child_visible()]
        if visible:
            pos = visible.index(self.sel) if self.sel in visible else 0
            self.listbox.select_row(self.rows[visible[(pos + d) % len(visible)]][0])

    def on_install_toggled(self, cb, r):
        if self.loading:
            return
        self.checkpoint("install")
        r["install"] = cb.get_active()
        self.refresh_list_labels()
        self.schedule_save()

    def load_form(self):
        r = self.cur()
        self.loading = True
        mono = r["base"] == "monochrome"
        if self.e_name.get_text() != r["name"]:
            self.e_name.set_text(r["name"])
        self.e_slot.set_value(int(r["slot"]) if str(r["slot"]).isdigit() else 1)
        fname = ncp.file_name(r) if str(r["slot"]).isdigit() else "—"
        self.e_slot.set_subtitle(f"File {fname}")
        np2 = ncp.np2_supported(r)
        self.g_pc.set_description("Written to the .NP2 file — curve, gamma, sharpening, Clarity and colour" if np2 else
                                  "Written to the .NCP file (black & white isn't supported in .NP2 yet, so set Clarity on the camera)")
        self.r_clar.set_subtitle("Stored in the file" if np2 else "Set this on the camera")
        self.e_base.set_selected(BASES.index(r["base"]) if r["base"] in BASES else 1)
        g = float(r.get("gamma", 1.0))
        self.s_gamma.set_value(g)
        self.v_gamma.set_text(f"{g:.2f}")
        verbatim = bool(r.get("lut")) and r.get("lut_sig") == ncp.lut_signature(r)
        self.r_gamma.set_subtitle("Imported curve kept exactly as in the file — editing the curve or gamma recalculates it"
                                  if verbatim else "Bends the whole curve: above 1 opens the mid-tones")
        auto_s, auto_sat = str(r["sharpening"]) == "auto", str(r["saturation"]) == "auto"
        self.a_sharp.set_active(auto_s)
        self.s_sharp.set_sensitive(not auto_s)
        self.s_sharp.set_value(3 if auto_s else int(r["sharpening"]))
        self.v_sharp.set_text("Auto" if auto_s else str(r["sharpening"]))
        self.a_sat.set_active(auto_sat)
        self.s_sat.set_sensitive(not auto_sat)
        self.s_sat.set_value(0 if auto_sat else int(r["saturation"]))
        self.v_sat.set_text(fmt_signed(r["saturation"]))
        self.s_hue.set_value(int(r["hue"]))
        self.v_hue.set_text(fmt_signed(r["hue"]))
        self.e_filter.set_selected(FILTERS.index(r["filter"]) if r["filter"] in FILTERS else 0)
        self.e_toning.set_selected(TONINGS.index(r["toning"]) if r["toning"] in TONINGS else 0)
        self.s_str.set_value(int(r["toning_strength"]))
        self.s_str.set_sensitive(r["toning"] != "bw")
        self.v_str.set_text("—" if r["toning"] == "bw" else str(r["toning_strength"]))
        for w in (self.r_sat, self.r_hue):
            w.set_visible(not mono)
        for w in (self.e_filter, self.e_toning, self.r_str):
            w.set_visible(mono)
        wb = r.get("wb", "auto")
        self.e_wb.set_selected(WB_PRESETS.index(wb) if wb in WB_PRESETS else 0)
        self.e_kelvin.set_visible(wb == "kelvin")
        self.e_kelvin.set_value(int(r.get("wb_kelvin", 5000)))
        self.s_ab.set_value(float(r.get("wb_ab", 0)))
        self.v_ab.set_text(fmt_ab(r.get("wb_ab", 0)))
        self.s_gm.set_value(float(r.get("wb_gm", 0)))
        self.v_gm.set_text(fmt_gm(r.get("wb_gm", 0)))
        self.s_clar.set_value(int(r.get("clarity", 0)))
        self.v_clar.set_text(fmt_signed(r.get("clarity", 0)))
        if self.e_notes.get_text() != r.get("notes", ""):
            self.e_notes.set_text(r.get("notes", ""))
        self.curve.table = recipe_table(r) if self.curve_ok(r) else None
        self.curve.set_points(r["curve"])
        self.update_hist()
        errs = self.all_errors()[self.sel]
        self.errors.set_text(" ".join(errs))
        self.errors.set_visible(bool(errs))
        self.loading = False

    def set_field(self, key, value):
        if self.loading or self.cur().get(key) == value:
            return
        self.checkpoint(key)
        self.cur()[key] = value
        if key == "curve":
            self.curve.sel = -1
        self.load_form()
        self.refresh_list_labels()
        self.after_change()

    def after_change(self):
        self.looks_dirty = True
        self.schedule_save()
        self.schedule_render()
        self.schedule_looks()

    def on_curve_begin(self, key=None):
        self.checkpoint(key)

    def on_curve_changed(self, final=False):
        if self.loading:
            return
        self.cur()["curve"] = [list(p) for p in self.curve.points]
        self.curve.table = recipe_table(self.cur()) if self.curve_ok(self.cur()) else None
        errs = self.all_errors()[self.sel]
        self.errors.set_text(" ".join(errs))
        self.errors.set_visible(bool(errs))
        self.schedule_render()
        if final:
            self.refresh_list_labels()
            self.after_change()

    def free_slot(self):
        used = {r["slot"] for r in self.recipes}
        return next((s for s in range(1, 100) if s not in used), 99)

    def on_new(self):
        self.checkpoint()
        self.recipes.append(self._norm({"slot": self.free_slot(), "name": "New look", "install": True}))
        self.sel = len(self.recipes) - 1
        self.rebuild_list()
        self.schedule_save()
        self.e_name.grab_focus()

    def on_dup(self):
        self.checkpoint()
        c = copy.deepcopy(self.cur())
        c["slot"], c["name"] = self.free_slot(), (c["name"] + " 2")[:19]
        self.recipes.insert(self.sel + 1, c)
        self.sel += 1
        self.rebuild_list()
        self.schedule_save()

    def on_delete(self):
        if len(self.recipes) <= 1:
            return self.toast("Keep at least one recipe.")
        self.checkpoint()
        name = self.cur()["name"]
        del self.recipes[self.sel]
        self.rebuild_list()
        self.schedule_save()
        self.toast(f"Deleted “{name}”", button="Undo", cb=self.do_undo)

    def on_all(self):
        self.checkpoint()
        on = not all(r.get("install") for r in self.recipes)
        for r in self.recipes:
            r["install"] = on
        self.rebuild_list()
        self.schedule_save()

    # ------------------------------------------------------------ undo / redo
    def checkpoint(self, key=None):
        now = time.monotonic()
        if key and self._cp[0] == (key, self.sel) and now - self._cp[1] < 1.5:
            self._cp = (self._cp[0], now)
            return
        self.history.append((copy.deepcopy(self.recipes), self.sel))
        del self.history[:-300]
        self.future.clear()
        self._cp = ((key, self.sel) if key else None, now)
        self.update_undo_actions()

    def _restore(self, state):
        self.recipes, self.sel = copy.deepcopy(state[0]), state[1]
        self._cp = (None, 0.0)
        self.rebuild_list()
        self.after_change()
        self.update_undo_actions()

    def do_undo(self):
        if self.history:
            self.future.append((copy.deepcopy(self.recipes), self.sel))
            self._restore(self.history.pop())

    def do_redo(self):
        if self.future:
            self.history.append((copy.deepcopy(self.recipes), self.sel))
            self._restore(self.future.pop())

    def update_undo_actions(self):
        if hasattr(self, "actions"):
            self.actions["undo"].set_enabled(bool(self.history))
            self.actions["redo"].set_enabled(bool(self.future))

    # ------------------------------------------------------------ saving
    def schedule_save(self):
        self.title.set_subtitle("Saving…")
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(600, self.save_now)

    def save_now(self):
        self._save_id = 0
        try:
            ncp.save_all(self.recipes, RECIPES)
            self.title.set_subtitle(f"{len(self.recipes)} recipes · saved")
        except Exception as e:
            self.title.set_subtitle("not saved")
            self.toast(f"Couldn't save recipes.toml: {e}")
        return False

    # ------------------------------------------------------------ toasts & dialogs
    def toast(self, msg, button=None, cb=None, timeout=None):
        t = Adw.Toast(title=GLib.markup_escape_text(msg))
        t.set_timeout(timeout if timeout is not None else max(4, min(12, len(msg) // 12)))
        if button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *a: cb())
        self.toasts.add_toast(t)
        return False

    def on_about(self):
        d = Adw.AboutDialog(application_name="Picture Control Studio", version=VERSION,
                            application_icon="camera-photo",
                            comments="Design Nikon Custom Picture Controls (.NCP), preview them on your NEFs "
                                     "and install them on the camera's SD card.",
                            license_type=Gtk.License.CUSTOM, license="Personal tool. Not affiliated with Nikon.")
        d.present(self)

    def on_shortcuts(self):
        groups = [
            ("Recipes", [("New recipe", "<Control>n"), ("Duplicate", "<Control>d"), ("Search", "<Control>f"),
                         ("Previous / next recipe", "<Alt>Up <Alt>Down"), ("Undo", "<Control>z"),
                         ("Redo", "<Control><Shift>z")]),
            ("Photo", [("Load photo", "<Control>o"), ("Previous / next photo (folder or selection)", "<Alt>Left <Alt>Right"),
                       ("Before / after", "<Control>b"), ("Preview / All looks", "<Control>1 <Control>2"),
                       ("Export photo with look", "<Control>e"), ("Export selected photos", "<Control><Shift>e")]),
            ("Card", [("Install to SD card", "<Control>i"), ("Cheat sheet PDF", "<Control>p")]),
            ("Curve", [("Nudge selected point", "Left Right Up Down"), ("Nudge by 5", "<Shift>Up"),
                       ("Next point", "Tab"), ("Remove point", "Delete")]),
        ]
        if hasattr(Adw, "ShortcutsDialog"):
            d = Adw.ShortcutsDialog()
            for title, items in groups:
                sec = Adw.ShortcutsSection(title=title)
                for label, accel in items:
                    sec.add(Adw.ShortcutsItem.new(label, accel))
                d.add(sec)
            d.present(self)
        else:
            body = "\n".join(f"{label}: {accel}" for _t, items in groups for label, accel in items)
            d = Adw.AlertDialog(heading="Keyboard Shortcuts", body=body)
            d.add_response("ok", "Close")
            d.present(self)

    # ------------------------------------------------------------ import / export
    def on_open(self):
        dlg = Gtk.FileDialog(title="Import Picture Controls (.NCP, .NP2) or recipes.toml")
        f = Gtk.FileFilter(name="Picture Controls (.NCP, .NP2, .toml)")
        for pat in ("*.ncp", "*.NCP", "*.np2", "*.NP2", "*.toml"):
            f.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.import_paths([files.get_item(i).get_path() for i in range(files.get_n_items())])
        dlg.open_multiple(self, None, done)

    def import_paths(self, paths):
        tomls = [p for p in paths if p.lower().endswith(".toml")]
        ncps = [p for p in paths if p.lower().endswith((".ncp", ".np2"))]
        warns = []
        self.checkpoint()
        if tomls:
            try:
                rs = ncp.load_recipes(tomls[0])
            except Exception as e:
                return self.toast(f"Couldn't read {os.path.basename(tomls[0])}: {e}")
            self.recipes, self.sel = [self._norm(r) for r in rs], 0
        for p in ncps:
            try:
                data = pathlib.Path(p).read_bytes()
                parser = ncp.parse_np2 if p.lower().endswith(".np2") else ncp.parse
                r, w = parser(data, os.path.basename(p))
            except Exception as e:
                warns.append(str(e))
                continue
            if any(x["slot"] == r["slot"] for x in self.recipes):
                r["slot"] = self.free_slot()
            r["install"] = True
            self.recipes.append(self._norm(r))
            self.sel = len(self.recipes) - 1
            warns += [f"{os.path.basename(p)}: {x}" for x in w]
        self.rebuild_list()
        self.after_change()
        if tomls:
            self.toast(f"Loaded {len(self.recipes)} recipes from {os.path.basename(tomls[0])}", button="Undo", cb=self.do_undo)
        elif warns:
            self.toast(" · ".join(warns))
        elif ncps:
            self.toast(f"Imported {len(ncps)} Picture Control file{'s' if len(ncps) > 1 else ''}", button="Undo", cb=self.do_undo)

    def chosen(self):
        errs = self.all_errors()
        picked = [(r, e) for r, e in zip(self.recipes, errs) if r.get("install")]
        if not picked:
            self.toast("Tick at least one recipe in the list.")
            return None
        bad = next(((r, e) for r, e in picked if e), None)
        if bad:
            self.toast(f"Fix “{bad[0]['name']}” first: {bad[1][0]}")
            return None
        return [r for r, _ in picked]

    def on_export(self):
        rs = self.chosen()
        if not rs:
            return
        dlg = Gtk.FileDialog(title="Export to folder (NIKON/CUSTOMPC is created inside)")
        dlg.select_folder(self, None, lambda d, res: self._folder_chosen(d, res, rs, None))

    def on_export_one(self):
        errs = self.all_errors()[self.sel]
        if errs:
            return self.toast("Fix this recipe first: " + errs[0])
        dlg = Gtk.FileDialog(title="Export Picture Control file", initial_name=ncp.file_name(self.cur()))

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            pathlib.Path(path).write_bytes(ncp.build(self.cur()) if path.lower().endswith(".ncp") else ncp.build_file(self.cur()))
            self.toast(f"Saved {f.get_basename()}")
        dlg.save(self, None, done)

    def on_cheatsheet(self):
        rs = [r for r in self.recipes if r.get("install")] or self.recipes
        dlg = Gtk.FileDialog(title="Save cheat sheet", initial_name="Picture Controls - camera settings.pdf")
        dlg.set_initial_folder(Gio.File.new_for_path(str(RECIPES.parent)))

        def done(d, res):
            try:
                path = d.save_finish(res).get_path()
            except GLib.Error:
                return
            try:
                write_cheatsheet(rs, path)
            except Exception as e:
                return self.toast(f"Couldn't write the PDF: {e}")
            self.toast(f"Saved {os.path.basename(path)} ({len(rs)} looks)", button="Open",
                       cb=lambda: Gio.AppInfo.launch_default_for_uri(pathlib.Path(path).as_uri(), None))
        dlg.save(self, None, done)

    # ------------------------------------------------------------ SD card
    def on_install(self):
        rs = self.chosen()
        if not rs:
            return
        cards = find_cards()
        if len(cards) == 1:
            name, path, mount = cards[0]
            return self.install_to(rs, path, name, mount)
        vols = [] if cards else unmounted_removable_volumes()
        if vols:
            self.toast("Mounting the card…", timeout=2)
            pending = [len(vols)]

            def mounted(v, res):
                try:
                    v.mount_finish(res)
                except GLib.Error:
                    pass
                pending[0] -= 1
                if pending[0] == 0:
                    found = find_cards()
                    if len(found) == 1:
                        self.install_to(rs, found[0][1], found[0][0], found[0][2])
                    else:
                        self.ask_folder(rs, found)
            for v in vols:
                v.mount(Gio.MountMountFlags.NONE, None, None, mounted)
            return
        self.ask_folder(rs, cards)

    def ask_folder(self, rs, cards):
        body = ("More than one card is mounted. Choose the card's top folder." if cards else
                "No camera card found. Insert the D810's SD card (it has a DCIM folder), "
                "or choose any folder to write NIKON/CUSTOMPC into.")
        d = Adw.AlertDialog(heading="Install to SD card", body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("choose", "Choose Folder…")
        d.set_response_appearance("choose", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("choose")

        def resp(dlg, rid):
            if rid != "choose":
                return
            fd = Gtk.FileDialog(title="Choose the card's top folder")
            if cards:
                fd.set_initial_folder(Gio.File.new_for_path(cards[0][1]))
            fd.select_folder(self, None, lambda f, res: self._folder_chosen(f, res, rs, cards))
        d.connect("response", resp)
        d.present(self)

    def _folder_chosen(self, dlg, res, rs, cards):
        try:
            path = dlg.select_folder_finish(res).get_path()
        except GLib.Error:
            return
        mount = next((m for n, p, m in (cards or []) if os.path.commonpath([p, path]) == p), None)
        looks_like_card = any((pathlib.Path(path) / d).is_dir() for d in ("DCIM", "NIKON")) or \
            pathlib.Path(path).name.upper() in ("NIKON", "CUSTOMPC")
        if cards is not None and not mount and not looks_like_card:
            d = Adw.AlertDialog(heading="This doesn't look like a camera card",
                                body=f"“{os.path.basename(path)}” has no DCIM folder. Install into "
                                     f"{os.path.basename(path)}/NIKON/CUSTOMPC anyway?")
            d.add_response("cancel", "Cancel")
            d.add_response("go", "Install Anyway")
            d.connect("response", lambda _d, rid: rid == "go" and self.install_to(rs, path, os.path.basename(path), None))
            d.present(self)
            return
        self.install_to(rs, path, os.path.basename(path), mount)

    def install_to(self, rs, path, name, mount):
        try:
            dest, added, replaced, same = ncp.install(rs, path)
        except Exception as e:
            return self.toast(f"Couldn't write to {name}: {e}")
        retired = getattr(ncp.install, "retired", [])
        parts = [f"{len(added)} new" if added else "", f"{len(replaced)} updated" if replaced else "",
                 f"{len(same)} already up to date" if same else "",
                 f"{len(retired)} old .NCP replaced by .NP2" if retired else ""]
        msg = f"Installed on {name}: " + ", ".join(p for p in parts if p)
        if mount:
            self.toast(msg, button="Eject", cb=lambda: self.eject(mount, name), timeout=0)
        else:
            self.toast(msg + f" ({dest})")

    def eject(self, mount, name):
        def done(m, res):
            try:
                (m.eject_with_operation_finish if m.can_eject() else m.unmount_with_operation_finish)(res)
                self.toast(f"{name} can be removed. On the camera: MENU › Shooting › Manage Picture Control › "
                           "Load/save › Copy to camera.", timeout=15)
            except GLib.Error as e:
                self.toast(f"Couldn't eject {name}: {e.message}")
        op = Gtk.MountOperation.new(self)
        if mount.can_eject():
            mount.eject_with_operation(Gio.MountUnmountFlags.NONE, op, None, done)
        else:
            mount.unmount_with_operation(Gio.MountUnmountFlags.NONE, op, None, done)

    def on_card_contents(self):
        cards = find_cards()
        if not cards:
            return self.toast("No camera card found. Insert the SD card and try again.")
        d = Adw.Dialog(title="Card Contents", content_width=480, content_height=560)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        for name, root, _m in cards:
            pc = card_pc_dir(root)
            files = sorted([*pc.glob("*.[nN][cC][pP]"), *pc.glob("*.[nN][pP]2")]) if pc else []
            g = Adw.PreferencesGroup(title=GLib.markup_escape_text(name),
                                     description=GLib.markup_escape_text(str(pc or pathlib.Path(root) / "NIKON/CUSTOMPC")))
            if files:
                imp = Gtk.Button(label="Import All", valign=Gtk.Align.CENTER)
                imp.add_css_class("flat")
                imp.connect("clicked", lambda _b, fs=files: (d.close(), self.import_paths([str(f) for f in fs])))
                g.set_header_suffix(imp)
            for f in files:
                try:
                    data = f.read_bytes()
                    r, _w = (ncp.parse_np2 if f.suffix.upper() == ".NP2" else ncp.parse)(data, f.name)
                    ours = any(ncp.build_file(x) == data for x in self.recipes if not ncp.validate(x))
                    sub = f"{f.name} · {r['base'].title()}" + (" · matches one of your recipes" if ours else "")
                    row = Adw.ActionRow(title=GLib.markup_escape_text(r["name"]), subtitle=sub)
                except Exception as e:
                    row = Adw.ActionRow(title=f.name, subtitle=GLib.markup_escape_text(f"unreadable: {e}"))
                g.add(row)
            if not files:
                g.add(Adw.ActionRow(title="No Picture Controls on this card yet"))
            page.add(g)
        tv.set_content(page)
        d.set_child(tv)
        d.present(self)

    # ------------------------------------------------------------ photo loading
    def pick_photo(self):
        dlg = Gtk.FileDialog(title="Load photos (select several to step through them)")
        f = Gtk.FileFilter(name="Photos (NEF, JPEG, PNG, TIFF)")
        for ext in PHOTO_EXT:
            f.add_pattern("*" + ext)
            f.add_pattern("*" + ext.upper())
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)
        start = os.path.dirname(self.photo_path) if self.photo_path else str(pathlib.Path.home() / "Pictures")
        if os.path.isdir(start):
            dlg.set_initial_folder(Gio.File.new_for_path(start))

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.open_photos([files.get_item(i).get_path() for i in range(files.get_n_items())])
        dlg.open_multiple(self, None, done)

    def open_photos(self, paths):
        """One photo: browse its folder. Several: browse just those."""
        photos = [p for p in paths if p and p.lower().endswith(PHOTO_EXT)]
        if not photos:
            return self.toast("None of those are photos (NEF, JPEG, PNG or TIFF).")
        self.load_photo(photos[0], group=photos if len(photos) > 1 else None)
        if len(photos) > 1:
            self.toast(f"{len(photos)} photos selected — step through them with Alt+← / Alt+→", timeout=4)

    def on_drop(self, target, value, x, y):
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths:
            return False
        if any(p.lower().endswith((".ncp", ".np2", ".toml")) for p in paths):
            self.import_paths(paths)
        else:
            self.open_photos(paths)
        return True

    def step_photo(self, d):
        if self.photo_path and self.siblings:
            try:
                i = self.siblings.index(self.photo_path)
            except ValueError:
                return
            self.load_photo(self.siblings[(i + d) % len(self.siblings)], keep=True)

    def update_nav_actions(self):
        if hasattr(self, "actions"):
            many = len(self.siblings) > 1
            self.actions["prev-photo"].set_enabled(many)
            self.actions["next-photo"].set_enabled(many)
            self.actions["export-photo"].set_enabled(self.src is not None)
            self.actions["export-batch"].set_enabled(self.src is not None and self.group_mode and many)

    def load_photo(self, path, group=None, keep=False):
        self.photo_path = path
        if group:
            self.siblings, self.group_mode = sorted(group), True
        elif not (keep and path in self.siblings):
            folder = os.path.dirname(path)
            self.group_mode = False
            try:
                self.siblings = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                                       if f.lower().endswith(PHOTO_EXT) and not f.startswith("."))
            except OSError:
                self.siblings = [path]
        self.update_nav_actions()
        self.spinner.start()
        self.info.set_text(f"Loading {os.path.basename(path)}…")

        def work():
            res = {"path": path}
            t0 = time.time()
            try:
                if path.lower().endswith(".nef"):
                    res["nef"] = nef_info(path)
                    try:
                        res["raw"] = decode_raw(path)
                    except Exception as e:
                        res["raw_error"] = str(e)
                    if res["nef"].get("jpeg"):
                        try:
                            res["jpeg"] = np.asarray(jpeg_from_bytes(res["nef"]["jpeg"], res["nef"]["orientation"],
                                                                     PREVIEW_MAX), np.float32)
                        except Exception:
                            pass
                else:
                    res["image"] = np.asarray(open_image(path, PREVIEW_MAX), np.float32)
            except Exception as e:
                res["error"] = str(e)
            res["secs"] = time.time() - t0
            GLib.idle_add(self.photo_loaded, res)
        threading.Thread(target=work, daemon=True).start()

    def photo_loaded(self, res):
        if res["path"] != self.photo_path:
            return False
        self.spinner.stop()
        name = os.path.basename(res["path"])
        if "error" in res:
            self.info.set_text("")
            self.toast(f"Couldn't open {name}: {res['error']}")
            return False
        self.nef, self.raw_lin, self.jpeg_src = res.get("nef"), res.get("raw"), res.get("jpeg")
        self.image_src = res.get("image")
        pos = ""
        if res["path"] in self.siblings and len(self.siblings) > 1:
            pos = f"{self.siblings.index(res['path']) + 1}/{len(self.siblings)}{' selected' if self.group_mode else ''} · "
        if self.image_src is not None:
            self.info.set_text(f"{pos}{name}")
            self.set_mode("image", force=True)
        else:
            n = self.nef
            shot = ""
            if n.get("pc"):
                shot = f" · shot with {n['pc']}"
                if n.get("pc_base") and n["pc_base"] != n["pc"]:
                    shot += f" ({n['pc_base'].title()})"
            if self.raw_lin is not None:
                self.info.set_text(f"{pos}{name} · {n['width']}×{n['height']}{shot} · {res['secs']:.1f} s")
                keep_jpeg = self.mode == "jpeg" and self.jpeg_src is not None
                self.set_mode("jpeg" if keep_jpeg else "raw", force=True)
            elif self.jpeg_src is not None:
                self.info.set_text(f"{pos}{name}{shot} · camera JPEG only")
                self.toast(f"RAW couldn't be developed ({res.get('raw_error', '?')}); showing the camera JPEG.")
                self.set_mode("jpeg", force=True)
            else:
                self.toast(f"Nothing in {name} could be shown: {res.get('raw_error', '')}")
        return False

    def set_mode(self, mode, force=False):
        if self.mode == mode and not force:
            return
        self.mode = mode
        if mode == "raw":
            self.src = srgb_encode(self.raw_lin * (2 ** self.exposure)).astype(np.float32)
        elif mode == "jpeg":
            self.src = self.jpeg_src
        elif mode == "image":
            self.src = self.image_src
        self.source_changed()
        self.update_source_controls()
        self.stack.set_visible_child_name("photo")
        self.looks_stack.set_visible_child_name("grid")
        self.update_nav_actions()

    def source_changed(self):
        self._blur = (None, None)
        self.thumb_src = thumbnail(self.src)
        self.thumb_blur = luma_blur(self.thumb_src)
        self.lumahist = np.bincount(np.clip(self.src @ LUMA, 0, 255).astype(np.uint8).ravel()[::7],
                                    minlength=256).astype(np.float64)
        self.update_hist()
        self.looks_dirty = True
        self.schedule_render(now=True)
        self.schedule_looks()

    def update_source_controls(self):
        is_nef = self.mode in ("raw", "jpeg")
        self.seg.set_visible(is_nef)
        self.exp_box.set_visible(self.mode == "raw")
        if is_nef:
            self.b_raw.set_sensitive(self.raw_lin is not None)
            self.b_jpg.set_sensitive(self.jpeg_src is not None)
            btn = self.b_raw if self.mode == "raw" else self.b_jpg
            if not btn.get_active():
                btn.set_active(True)

    def on_exposure(self, s):
        self.exposure = round(s.get_value(), 1)
        self.v_exp.set_text("0.0 EV" if self.exposure == 0 else f"{self.exposure:+.1f} EV")
        if self.mode == "raw" and self.raw_lin is not None:
            self.src = srgb_encode(self.raw_lin * (2 ** self.exposure)).astype(np.float32)
            self.source_changed()

    def update_hist(self):
        h = getattr(self, "lumahist", None)
        if h is None or self.src is None:
            self.curve.hist = None
        elif self.mode == "raw":
            bt = np.clip(np.rint(curve_table(BASE_TONE.get(self.cur()["base"], BASE_TONE["standard"]))), 0, 255).astype(int)
            self.curve.hist = np.bincount(bt, weights=h, minlength=256)
        else:
            self.curve.hist = h
        self.curve.queue_draw()

    # ------------------------------------------------------------ rendering
    def blur(self):
        if self._blur[0] is not self.src:
            self._blur = (self.src, luma_blur(self.src))
        return self._blur[1]

    @staticmethod
    def curve_ok(r):
        return not any(e.startswith("Curve") for e in ncp.validate({**r, "name": "x", "slot": 1}))

    def schedule_render(self, now=False):
        if self.src is None:
            return
        if self._render_id:
            GLib.source_remove(self._render_id)
        self._render_id = GLib.timeout_add(1 if now else 16, self.render_now)

    def render_now(self):
        self._render_id = 0
        r = self.cur()
        if self.src is None or not self.curve_ok(r):
            return False
        try:
            raw = self.mode == "raw"
            out = render(self.src, r, raw, self.blur() if r.get("clarity") else None)
            if self.compare:
                orig = base_only(self.src, r["base"], raw)
                x = int(out.shape[1] * self.split)
                out = out.copy()
                out[:, :x] = orig[:, :x]
                out[:, max(0, x - 1):x + 1] = 255
            self.picture.set_paintable(texture(out))
        except Exception as e:
            self.info.set_text(f"Preview error: {e}")
        return False

    def show_original(self, on):
        if self.src is None:
            return
        self.badge.set_visible(on)
        if on:
            self.picture.set_paintable(texture(base_only(self.src, self.cur()["base"], self.mode == "raw")))
        else:
            self.render_now()

    def set_compare(self, on):
        self.compare = on
        self.badge.set_visible(on)
        self.badge2.set_visible(on)
        self.badge2.set_label(self.cur()["name"])
        self.picture.set_tooltip_text("Drag to move the divider" if on else None)
        self.schedule_render(now=True)

    def drag_split(self, x):
        if not self.compare or self.src is None:
            return
        pw, ph = self.picture.get_width(), self.picture.get_height()
        ih, iw = self.src.shape[:2]
        sc = min(pw / iw, ph / ih)
        ox = (pw - iw * sc) / 2
        self.split = min(1.0, max(0.0, (x - ox) / (iw * sc)))
        self.schedule_render()

    # ------------------------------------------------------------ all looks grid
    def schedule_looks(self):
        if self._looks_id or not self.looks_dirty or self.src is None:
            return
        if self.pages.get_visible_child_name() != "looks":
            return
        self._looks_id = GLib.timeout_add(120, self.build_looks)

    def build_looks(self):
        self._looks_id = 0
        self.looks_dirty = False
        while (c := self.flow.get_child_at_index(0)) is not None:
            self.flow.remove(c)
        raw = self.mode == "raw"
        for i, r in enumerate(self.recipes):
            if not self.curve_ok(r):
                continue
            img = render(self.thumb_src, r, raw, self.thumb_blur if r.get("clarity") else None)
            pic = Gtk.Picture(paintable=texture(img), can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
            pic.set_size_request(-1, 170)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.append(pic)
            lab = Gtk.Label(label=r["name"], ellipsize=3)
            if i == self.sel:
                lab.add_css_class("accent")
                lab.add_css_class("heading")
            box.append(lab)
            child = Gtk.FlowBoxChild(child=box, tooltip_text=f"{r['name']} — click to edit")
            child.recipe_index = i
            self.flow.append(child)
        return False

    def on_look_activated(self, flow, child):
        i = getattr(child, "recipe_index", None)
        if i is not None:
            self.listbox.select_row(self.rows[i][0])
            self.pages.set_visible_child_name("preview")

    # ------------------------------------------------------------ export photo
    def on_export_photo(self):
        if self.src is None:
            return self.toast("Load a photo first.")
        r = copy.deepcopy(self.cur())
        if not self.curve_ok(r):
            return self.toast("Fix the curve first.")
        stem = pathlib.Path(self.photo_path).stem
        safe = re.sub(r"[^\w\- ]+", "", r["name"]).strip()
        dlg = Gtk.FileDialog(title="Export photo with look", initial_name=f"{stem} - {safe}.jpg")
        dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.photo_path)))

        def done(d, res):
            try:
                out_path = d.save_finish(res).get_path()
            except GLib.Error:
                return
            self.export_photo_to(out_path, r)
        dlg.save(self, None, done)

    def export_photo_to(self, out_path, r, wait=False):
        mode, path, exposure, nef = self.mode, self.photo_path, self.exposure, self.nef
        self.spinner.start()
        self.toast(f"Exporting {os.path.basename(out_path)} at full resolution…", timeout=3)

        def work():
            try:
                t0 = time.time()
                size = export_file(path, out_path, r, mode, exposure, nef)
                msg = (f"Saved {os.path.basename(out_path)} ({size[0]}×{size[1]}, {time.time() - t0:.1f} s)", None)
            except Exception as e:
                msg = (f"Export failed: {e}", e)
            GLib.idle_add(self._exported, out_path, msg)
        th = threading.Thread(target=work, daemon=True)
        th.start()
        if wait:
            th.join()

    def on_export_batch(self):
        if not (self.group_mode and len(self.siblings) > 1):
            return self.toast("Load several photos first (select more than one in Load Photos).")
        r = copy.deepcopy(self.cur())
        if not self.curve_ok(r):
            return self.toast("Fix the curve first.")
        dlg = Gtk.FileDialog(title=f"Export {len(self.siblings)} photos with “{r['name']}” to folder")
        dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.siblings[0])))

        def done(d, res):
            try:
                folder = d.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            self.export_batch_to(folder, list(self.siblings), r)
        dlg.select_folder(self, None, done)

    def export_batch_to(self, folder, paths, r, wait=False):
        """NEFs are developed from RAW (current exposure); other files keep their own rendering."""
        exposure = self.exposure
        safe = re.sub(r"[^\w\- ]+", "", r["name"]).strip()
        self.spinner.start()
        self.toast(f"Exporting {len(paths)} photos with “{r['name']}”…", timeout=3)

        def work():
            done, failed, t0 = 0, [], time.time()
            for i, path in enumerate(paths, 1):
                GLib.idle_add(self.info.set_text, f"Exporting {i}/{len(paths)} · {os.path.basename(path)}…")
                out = os.path.join(folder, f"{pathlib.Path(path).stem} - {safe}.jpg")
                try:
                    if path.lower().endswith(".nef"):
                        try:
                            export_file(path, out, r, "raw", exposure, None)
                        except Exception:
                            n = nef_info(path)
                            if not n.get("jpeg"):
                                raise
                            export_file(path, out, r, "jpeg", 0, n)
                    else:
                        export_file(path, out, r, "image", 0, None)
                    done += 1
                except Exception as e:
                    failed.append(f"{os.path.basename(path)} ({e})")
            text = f"Exported {done} photo{'s' if done != 1 else ''} to {os.path.basename(folder)} in {time.time() - t0:.0f} s"
            if failed:
                text += f"; failed: {', '.join(failed)}"
            GLib.idle_add(self._batch_done, folder, text)
        th = threading.Thread(target=work, daemon=True)
        th.start()
        if wait:
            th.join()

    def _batch_done(self, folder, text):
        self.spinner.stop()
        if self.photo_path:
            pos = f"{self.siblings.index(self.photo_path) + 1}/{len(self.siblings)} selected · " if self.photo_path in self.siblings else ""
            self.info.set_text(f"{pos}{os.path.basename(self.photo_path)}")
        self.toast(text, button="Open Folder", cb=lambda: Gio.AppInfo.launch_default_for_uri(pathlib.Path(folder).as_uri(), None))
        return False

    def _exported(self, out_path, msg):
        self.spinner.stop()
        text, err = msg
        if err:
            self.toast(text)
        else:
            self.toast(text, button="Open",
                       cb=lambda: Gio.AppInfo.launch_default_for_uri(pathlib.Path(out_path).as_uri(), None))
        return False


# ================================================================ full-resolution export
def export_file(path, out_path, r, mode, exposure=0.0, nef=None):
    """Render one photo at full resolution with recipe r and save as JPEG. Returns (w, h)."""
    if mode == "raw":
        arr = dcraw(path, half=False)
        h, w = arr.shape[:2]
        enc = np.empty((h, w, 3), np.uint8)
        for a in range(0, h, 512):
            enc[a:a + 512] = srgb_encode(arr[a:a + 512].astype(np.float32) / 65535.0 * (2 ** exposure))
        del arr
    elif mode == "jpeg":
        enc = np.asarray(jpeg_from_bytes(nef["jpeg"], nef["orientation"]))
    else:
        enc = np.asarray(open_image(path))
    blur = None
    if r.get("clarity"):
        lum = Image.fromarray(((enc.astype(np.uint16) * np.array([77, 150, 29], np.uint16)).sum(axis=2) >> 8)
                              .astype(np.uint8), "L")
        blur = np.asarray(lum.filter(ImageFilter.GaussianBlur(max(enc.shape[:2]) * 0.012)), np.float32)
    out = np.empty_like(enc)
    for a in range(0, enc.shape[0], 512):
        out[a:a + 512] = render(enc[a:a + 512].astype(np.float32), r, mode == "raw",
                                None if blur is None else blur[a:a + 512])
    Image.fromarray(out).save(out_path, quality=93, optimize=True)
    return out.shape[1], out.shape[0]


# ================================================================ cheat sheet PDF
def write_cheatsheet(recipes, path):
    W, H = 842, 595  # A4 landscape, points
    surf = cairo.PDFSurface(path, W, H)
    cr = cairo.Context(surf)
    cols = [("Slot", 36), ("Name", 66), ("Base", 196), ("Sharp", 262), ("Sat / Hue", 302), ("WB", 372),
            ("Fine-tune", 446), ("Clarity", 510), ("Notes", 556)]

    def fit(text, width):
        while text and cr.text_extents(text).x_advance > width:
            text = text[:-2] + "…" if len(text) > 2 else ""
        return text

    def header(page):
        cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(17)
        cr.move_to(36, 46)
        cr.show_text("Picture Controls — settings to dial in on the camera")
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9)
        cr.set_source_rgb(0.45, 0.45, 0.45)
        cr.move_to(36, 62)
        cr.show_text(f"Nikon D810 · {datetime.date.today():%d %b %Y} · page {page} · White balance isn't stored in a "
                     "Picture Control — set it when you switch looks. Clarity marked ✱ (B&W) must be set on the camera too.")
        cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.set_font_size(8)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        for title, x in cols:
            cr.move_to(x, 88)
            cr.show_text(title.upper())
        cr.set_line_width(0.8)
        cr.move_to(36, 94)
        cr.line_to(W - 36, 94)
        cr.stroke()

    page, y = 1, 112
    header(page)
    for n, r in enumerate(sorted(recipes, key=lambda r: int(r["slot"]))):
        if y > H - 40:
            cr.show_page()
            page += 1
            header(page)
            y = 112
        if n % 2:
            cr.set_source_rgb(0.95, 0.95, 0.94)
            cr.rectangle(32, y - 13, W - 64, 19)
            cr.fill()
        cr.set_source_rgb(0.1, 0.1, 0.1)
        mono = r["base"] == "monochrome"
        ft = " ".join(x for x in (fmt_ab(r.get("wb_ab", 0)), fmt_gm(r.get("wb_gm", 0))) if x != "0") or "—"
        vals = [f"{int(r['slot']):02d}", r["name"], r["base"].title(), str(r["sharpening"]).title(),
                (f"{r['filter'].title()} filter" if mono else f"{fmt_signed(r['saturation'])} / {fmt_signed(r['hue'])}"),
                fmt_wb(r), ft, fmt_signed(r.get("clarity", 0)) + ("" if ncp.np2_supported(r) or not r.get("clarity") else " ✱"),
                r.get("notes", "")]
        for i, ((_t, x), v) in enumerate(zip(cols, vals)):
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if i == 1 else cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(9.5)
            nxt = cols[i + 1][1] if i + 1 < len(cols) else W - 36
            cr.move_to(x, y)
            cr.show_text(fit(str(v), nxt - x - 8))
        y += 19
    surf.finish()


# ================================================================ application
class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.connect("activate", self.on_activate)
        self.connect("open", self.on_open)

    def on_activate(self, *a):
        win = self.get_active_window() or Window(self)
        win.present()
        if os.environ.get("PCS_SNAPSHOT") and not getattr(self, "_snap", False):
            self._snap = True
            print("snapshot: window activated", flush=True)
            GLib.timeout_add(int(os.environ.get("PCS_SNAPSHOT_DELAY", "3000")), self._dev_snapshot, win)
        return win

    def on_open(self, app, files, n, hint):
        win = self.on_activate()
        paths = [f.get_path() for f in files if f.get_path()]
        if any(p.lower().endswith((".ncp", ".np2", ".toml")) for p in paths):
            win.import_paths(paths)
        elif paths:
            win.open_photos(paths)

    def _dev_snapshot(self, win):
        """Developer hook (only with PCS_SNAPSHOT set): run PCS_TEST_PY, save the window as PNG, quit."""
        code = os.environ.get("PCS_TEST_PY")
        if code:
            try:
                exec(code, {"win": win, "app": self, "GLib": GLib, "np": np, "ncp": ncp, "os": os})
            except Exception as e:
                print("PCS_TEST_PY error:", repr(e), flush=True)

        def snap():
            try:
                target = win.get_content()
                paintable = Gtk.WidgetPaintable.new(target).get_current_image()
                w, h = target.get_width(), target.get_height()
                snapshot = Gtk.Snapshot()
                bg = Gdk.RGBA()
                bg.parse("#1e1e1e" if Adw.StyleManager.get_default().get_dark() else "#fafafa")
                snapshot.append_color(bg, Graphene.Rect().init(0, 0, w, h))
                paintable.snapshot(snapshot, w, h)
                node = snapshot.to_node()
                tex = win.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
                tex.save_to_png(os.environ["PCS_SNAPSHOT"])
                print(f"snapshot: saved {w}x{h} to {os.environ['PCS_SNAPSHOT']}", flush=True)
            except Exception as e:
                import traceback
                print("snapshot: failed:", repr(e), flush=True)
                traceback.print_exc()
            finally:
                self.quit()
            return False
        GLib.timeout_add(int(os.environ.get("PCS_SNAPSHOT_AFTER", "1000")), snap)
        return False


if __name__ == "__main__":
    if os.environ.get("PCS_LOG"):  # diagnostics: send stdout/stderr (incl. GTK messages) to a file
        _log = open(os.environ["PCS_LOG"], "a", buffering=1)
        os.dup2(_log.fileno(), 1)
        os.dup2(_log.fileno(), 2)
        sys.stdout = sys.stderr = _log
        print("studio: starting", sys.platform, sys.argv, flush=True)
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Picture Control Studio")
    rc = App().run(sys.argv)
    if os.environ.get("PCS_LOG"):
        print("studio: exit", rc, flush=True)
    sys.exit(rc)
