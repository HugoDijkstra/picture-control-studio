#!/usr/bin/env python3
"""Nikon Custom Picture Control encoder/decoder (.NP2 and .NCP) and command-line builder.

GUI: studio.py (Picture Control Studio). This module is also its file-format library.

Usage:
    python3 ncp.py [recipes.toml] [-o OUTDIR]   # build every recipe into OUTDIR/NIKON/CUSTOMPC/
    python3 ncp.py --ncp                        # force the old .NCP format for every recipe
    python3 ncp.py --dump FILE.NP2|FILE.NCP     # show what's inside a file

Colour recipes are written as .NP2 (Picture Control 2.0, D810 and later: custom curve, gamma,
sharpening, Clarity, saturation, hue, Flat base, quarter steps). Monochrome recipes are written as
.NCP until a camera-written monochrome .NP2 has been seen. Copy NIKON/ to the root of the SD card,
then on the camera: MENU > Shooting > Manage Picture Control > Load/save > Copy to camera.

How the formats were worked out
  .NCP  from nikonpc.com's generator and from files made with Nikon's Picture Control Utility:
        output is byte-identical to both (26 Nikon-made files rebuild exactly).
  .NP2  from files written by a D810: all of them rebuild byte-for-byte, including one with a
        custom curve and Clarity.

.NCP layout (big-endian, 638 bytes)
  0x00  "NCP\\0"  u32 1  u32 0x24  "0100"
  0x10  name, 20 bytes, NUL padded (max 19 chars)
  0x24  u16 base Picture Control code
  0x26  reserved                0x27  0xFF
  0x28  sharpening  0x80+n (0x00 = Auto)
  0x29  contrast    0x01 = custom curve
  0x2A  brightness  0x01 = custom curve
  0x2B  saturation  0x80+n (0x00 = Auto, 0xFF for Monochrome)
  0x2C  hue         0x80+n (0xFF for Monochrome)
  0x2D  filter effect   0x80 off, 81 yellow, 82 orange, 83 red, 84 green (mono only, else 0xFF)
  0x2E  toning type     0x80 B&W, 81 sepia, 82 cyanotype, 83 red, 84 yellow,
                        85 green, 86 blue-green, 87 blue, 88 purple-blue, 89 red-purple
  0x2F  toning strength 0x80+n (0xFF when B&W or not mono)
  0x30  curve record: u32 2, u32 0x242, then
        "I0", levels (in min, in max, out min, out max), gamma (int, hundredths),
        point count + up to 20 (x, y) byte pairs, padding,
        257 x u16 tone LUT 0..32767 (natural cubic spline sampled at x = i*255/256, plus gamma)
  0x27A 4 zero bytes

.NP2 layout
  "NCP\\0"  u32 0x100  u32 4  "0200", then records [00 00 tag 00][u32 length][data]:
  tag 2 name (20 bytes), 3 base (u16; Flat = 0x028E), 4 auto flags, 5 FF 01,
  6 sharpening, 7 Clarity, 8 contrast, 9 brightness, 10 saturation, 11 hue  (each [0x80 + 4*value, 04];
  00 = Auto, 01 = set by curve), 12 filter, 13 toning, 14 toning strength,
  then the same curve record as .NCP (u32 2, u32 0x242, ...), then 4 zero bytes.
"""
import argparse, json, os, pathlib, re, struct, sys, tomllib

HERE = pathlib.Path(__file__).resolve().parent

BASES = {  # base Picture Control codes
    "standard": 0x0001, "neutral": 0x03C2, "vivid": 0x00C3,
    "monochrome": 0x064D, "portrait": 0x0486, "landscape": 0x04C7,
}
FILTERS = {"off": 0x80, "yellow": 0x81, "orange": 0x82, "red": 0x83, "green": 0x84}
TONINGS = {"bw": 0x80, "sepia": 0x81, "cyanotype": 0x82, "red": 0x83, "yellow": 0x84,
           "green": 0x85, "blue-green": 0x86, "blue": 0x87, "purple-blue": 0x88,
           "red-purple": 0x89}
NAME_OK = re.compile(r'^[A-Za-z0-9 \[\]_{}!"#$%&\'()*+,\-./:;<=>?@]{1,19}$')


def spline(points):
    """Natural cubic spline through points; returns f(x) clamped to 0..255."""
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    n = len(points) - 1
    h = [xs[i + 1] - xs[i] for i in range(n)]
    sub, diag, sup, rhs = [0.0] * (n + 1), [1.0] * (n + 1), [0.0] * (n + 1), [0.0] * (n + 1)
    for i in range(1, n):
        sub[i], diag[i], sup[i] = h[i - 1], 2 * (h[i - 1] + h[i]), h[i]
        rhs[i] = 3 * ((ys[i + 1] - ys[i]) / h[i] - (ys[i] - ys[i - 1]) / h[i - 1])
    for i in range(2, n):  # Thomas algorithm, rows 1..n-1
        m = sub[i] / diag[i - 1]
        diag[i] -= m * sup[i - 1]
        rhs[i] -= m * rhs[i - 1]
    c = [0.0] * (n + 1)
    for i in range(n - 1, 0, -1):
        c[i] = (rhs[i] - sup[i] * c[i + 1]) / diag[i]
    b = [(ys[i + 1] - ys[i]) / h[i] - (c[i + 1] + 2 * c[i]) * h[i] / 3 for i in range(n)]
    d = [(c[i + 1] - c[i]) / (3 * h[i]) for i in range(n)]

    def f(x):
        if x <= xs[0]:
            y = ys[0]
        elif x >= xs[-1]:
            y = ys[-1]
        else:
            i = max(k for k in range(n) if xs[k] <= x)
            t = x - xs[i]
            y = ys[i] + b[i] * t + c[i] * t * t + d[i] * t ** 3
        return min(255.0, max(0.0, y))
    return f


DEFAULTS = {"base": "neutral", "sharpening": 3, "saturation": 0, "hue": 0, "filter": "off",
            "toning": "bw", "toning_strength": 4, "curve": [[0, 0], [255, 255]], "notes": ""}


def with_defaults(r):
    out = {**DEFAULTS, **r}
    out["curve"] = [list(p) for p in out["curve"]]
    return out


def _is_auto(v):
    return str(v).lower() == "auto"


def _int_in(v, lo, hi):
    try:
        return int(v) == v and lo <= int(v) <= hi
    except (TypeError, ValueError):
        return False


def _quarter_in(v, lo, hi):
    try:
        return lo <= float(v) <= hi and float(v) * 4 == int(float(v) * 4)
    except (TypeError, ValueError):
        return False


def validate(r0, np2=None):
    """Return a list of problems with a recipe (empty = OK). np2=None: check for the format it will be saved as."""
    r, e = with_defaults(r0), []
    if np2 is None:
        np2 = r["base"] == "flat" or (r["base"] != "monochrome")
    if np2:
        return _validate_np2(r)
    if not NAME_OK.match(str(r.get("name", ""))):
        e.append("Name: 1–19 characters, letters, digits and basic punctuation only.")
    if not _int_in(r.get("slot"), 1, 99):
        e.append("Slot must be 1–99.")
    base = str(r["base"]).lower()
    if base not in BASES:
        e.append(f"Base must be one of {', '.join(BASES)} (Flat isn't possible in .NCP).")
    if not _is_auto(r["sharpening"]) and not _int_in(r["sharpening"], 0, 9):
        e.append("Sharpening must be 0–9 or auto.")
    pts = r["curve"]
    if not 2 <= len(pts) <= 20:
        e.append("Curve needs 2–20 points.")
    elif any(not (_int_in(x, 0, 255) and _int_in(y, 0, 255)) for x, y in pts):
        e.append("Curve values must be whole numbers 0–255.")
    elif any(pts[i][0] >= pts[i + 1][0] for i in range(len(pts) - 1)):
        e.append("Curve x values must increase.")
    try:
        g = float(r.get("gamma", 1.0))
        if not 0.1 <= g <= 5.0:
            e.append("Gamma must be 0.10–5.00.")
    except (TypeError, ValueError):
        e.append("Gamma must be a number.")
    if base == "monochrome":
        if r["filter"] not in FILTERS:
            e.append("Unknown filter.")
        if r["toning"] not in TONINGS:
            e.append("Unknown toning.")
        elif r["toning"] != "bw" and not _int_in(r["toning_strength"], 1, 7):
            e.append("Toning strength must be 1–7.")
    else:
        if not _is_auto(r["saturation"]) and not _int_in(r["saturation"], -3, 3):
            e.append("Saturation must be −3..3 or auto.")
        if not _int_in(r["hue"], -3, 3):
            e.append("Hue must be −3..3.")
    return e


def lut_signature(r):
    """What an imported LUT was computed from; if any of it changes, the LUT is recomputed."""
    return json.dumps([[list(map(int, p)) for p in r["curve"]], float(r.get("gamma", 1.0))])


def tone_lut(r0):
    """The 257-entry tone table (0..32767) written to the file."""
    r = with_defaults(r0)
    lut = r.get("lut")
    if lut and len(lut) == 257 and r.get("lut_sig") == lut_signature(r):
        return [int(v) for v in lut]
    pts = [tuple(int(v) for v in p) for p in r["curve"]]
    f, g = spline(pts), float(r.get("gamma", 1.0))
    x0, xn = pts[0][0], pts[-1][0]
    out = []
    for i in range(257):
        k = i * 255 / 256
        y = f(k)
        if g != 1 and x0 < k < xn:  # Picture Control gamma: bends the curve between its end points
            t = (k - x0) / (xn - x0)
            y += (t ** (1 / g) - t) * 255
        out.append(int(min(255.0, max(0.0, y)) * 32767 / 255 + 0.5))
    return out


def _validate_np2(r):
    e = []
    if not NAME_OK.match(str(r.get("name", ""))):
        e.append("Name: 1–19 characters, letters, digits and basic punctuation only.")
    if not _int_in(r.get("slot"), 1, 99):
        e.append("Slot must be 1–99.")
    if r["base"] not in BASES_NP2 or r["base"] == "monochrome":
        e.append("Base must be standard, neutral, vivid, flat, portrait or landscape for .NP2.")
    for k, lo, hi in (("sharpening", 0, 9), ("clarity", -5, 5), ("saturation", -3, 3), ("hue", -3, 3)):
        v = r.get(k, 0)
        if not (_is_auto(v) and k != "hue") and not _quarter_in(v, lo, hi):
            e.append(f"{k.title()} must be {lo}..{hi} in 0.25 steps" + (" or auto." if k != "hue" else "."))
    pts = r["curve"]
    if not 2 <= len(pts) <= 20:
        e.append("Curve needs 2–20 points.")
    elif any(not (_int_in(x, 0, 255) and _int_in(y, 0, 255)) for x, y in pts):
        e.append("Curve values must be whole numbers 0–255.")
    elif any(pts[i][0] >= pts[i + 1][0] for i in range(len(pts) - 1)):
        e.append("Curve x values must increase.")
    try:
        if not 0.1 <= float(r.get("gamma", 1.0)) <= 5.0:
            e.append("Gamma must be 0.10–5.00.")
    except (TypeError, ValueError):
        e.append("Gamma must be a number.")
    return e


def build(r0):
    """Encode a recipe as .NCP bytes. Raises ValueError if the recipe is invalid."""
    errs = validate(r0, np2=False)
    if errs:
        raise ValueError(f"{r0.get('name', '?')}: {' '.join(errs)}")
    r = with_defaults(r0)
    name, base = r["name"], r["base"].lower()
    pts = [tuple(int(v) for v in p) for p in r["curve"]]
    sharp = 0 if _is_auto(r["sharpening"]) else 0x80 + int(r["sharpening"])
    if base == "monochrome":
        sat = hue = 0xFF
        filt, tone = FILTERS[r["filter"]], TONINGS[r["toning"]]
        strength = 0xFF if tone == 0x80 else 0x80 + int(r["toning_strength"])
    else:
        sat = 0 if _is_auto(r["saturation"]) else 0x80 + int(r["saturation"])
        hue = 0x80 + int(r["hue"])
        filt = tone = strength = 0xFF

    out = bytearray(b"NCP\0") + struct.pack(">II", 1, 0x24) + b"0100"
    out += name.encode("ascii").ljust(20, b"\0")
    out += struct.pack(">H", BASES[base])
    out += bytes([int(r.get("reserved", 0)) & 0xFF, 0xFF, sharp, 0x01, 0x01, sat, hue, filt, tone, strength])
    out += curve_record(r) + bytes(4)
    assert len(out) == 638
    return bytes(out)


def curve_record(r0):
    """The custom-curve block shared by .NCP and .NP2: id 2, 578 bytes (levels, gamma, points, 257-entry LUT)."""
    r = with_defaults(r0)
    pts = [tuple(int(v) for v in p) for p in r["curve"]]
    g = round(float(r.get("gamma", 1.0)), 2)
    lvl = r.get("levels", [0, 255, 0, 255]) if r.get("lut") and r.get("lut_sig") == lut_signature(r) else [0, 255, 0, 255]
    body = b"I0" + bytes([int(v) for v in lvl] + [int(g), int(round((g - int(g)) * 100))])
    body += bytes([len(pts)]) + b"".join(bytes(p) for p in pts)
    body += bytes(0x40 - len(body))
    body += b"".join(struct.pack(">H", v) for v in tone_lut(r))
    assert len(body) == 0x242
    return struct.pack(">II", 2, 0x242) + body


# ---------------------------------------------------------------- .NP2 (Picture Control 2.0, D810 and later)
# Layout worked out from files written by a D810: "NCP\0", u32 0x100, u32 4, "0200", then records
# [00 00 tag 00][u32 length][data] and a 4-byte zero terminator. Adjustments are 0x80 + 4*value
# (quarter steps), 0x00 = Auto, 0x01 = "set by custom curve", 0xFF = n/a.
BASES_NP2 = {**BASES, "flat": 0x028E}
NP2_AUTO_BITS = {"sharpening": 0x01, "clarity": 0x02, "contrast": 0x04, "brightness": 0x08,
                 "saturation": 0x10, "hue": 0x20}  # only the clarity bit is confirmed from camera files


def np2_supported(r):
    """Monochrome .NP2 hasn't been seen from the camera yet, so B&W recipes stay .NCP."""
    return with_defaults(r)["base"] != "monochrome"


def file_format(r):
    return "np2" if np2_supported(r) else "ncp"


def file_name(r):
    return f"PICCON{int(r['slot']):02d}.{file_format(r).upper()}"


def build_file(r):
    return build_np2(r) if np2_supported(r) else build(r)


def _rec(tag, data):
    return bytes([0, 0, tag, 0]) + struct.pack(">I", len(data)) + data


def _q(v):
    return 0x00 if _is_auto(v) else 0x80 + int(round(float(v) * 4))


def build_np2(r0, with_curve=True):
    errs = validate(r0, np2=True)
    if errs:
        raise ValueError(f"{r0.get('name', '?')}: {' '.join(errs)}")
    if not np2_supported(r0):
        raise ValueError("Monochrome .NP2 isn't supported yet; use .NCP")
    r = with_defaults(r0)
    mask = 0
    for k, bit in NP2_AUTO_BITS.items():
        if _is_auto(r.get(k, 0)):
            mask |= bit
    contrast = bright = 0x01 if with_curve else None
    if not with_curve:
        contrast, bright = _q(r.get("contrast", 0)), _q(r.get("brightness", 0))
    out = b"NCP\0" + struct.pack(">II", 0x100, 4) + b"0200"
    out += _rec(2, r["name"].encode("ascii").ljust(20, b"\0"))
    out += _rec(3, struct.pack(">H", BASES_NP2[r["base"]]))
    out += _rec(4, bytes([mask, 0]))
    out += _rec(5, b"\xff\x01")
    for tag, v in ((6, _q(r["sharpening"])), (7, _q(r.get("clarity", 0))), (8, contrast), (9, bright),
                   (10, _q(r["saturation"])), (11, _q(r["hue"]))):
        out += _rec(tag, bytes([v, 4]))
    out += _rec(12, b"\xff\x00") + _rec(13, b"\xff\x00") + _rec(14, b"\xff\x04")
    if with_curve:
        out += curve_record(r)
    return out + bytes(4)


def parse_np2(d, filename=""):
    """Decode .NP2 bytes into (recipe, warnings)."""
    if len(d) < 20 or d[:4] != b"NCP\0" or d[12:16] != b"0200":
        raise ValueError(f"{filename or 'file'} is not an .NP2 file")
    recs, p = {}, 16
    while p + 8 <= len(d):
        rid, n = d[p:p + 4], struct.unpack(">I", d[p + 4:p + 8])[0]
        if rid == b"\0\0\0\x02":
            recs["curve"] = d[p + 8:p + 8 + n]
        elif rid[0] == rid[1] == rid[3] == 0 and rid[2]:
            recs[rid[2]] = d[p + 8:p + 8 + n]
        else:
            break
        p += 8 + n
    warn = []
    dq = lambda b: "auto" if b[0] == 0 else None if b[0] in (0x01, 0xFF) else (b[0] - 0x80) / 4
    num = lambda v: int(v) if isinstance(v, float) and v == int(v) else v
    base = {v: k for k, v in BASES_NP2.items()}.get(struct.unpack(">H", recs.get(3, b"\0\x03"))[0], "neutral")
    m = re.search(r"(\d{1,2})\.np2$", filename, re.I)
    r = {"slot": int(m.group(1)) if m else 1, "name": recs.get(2, b"Imported").split(b"\0")[0].decode("ascii", "replace")[:19],
         "notes": "", "base": base}
    for tag, key in ((6, "sharpening"), (7, "clarity"), (10, "saturation"), (11, "hue")):
        if tag in recs and dq(recs[tag]) is not None:
            r[key] = num(dq(recs[tag]))
    if recs.get(4, b"\0")[0]:
        r["np2_flags"] = recs[4][0]
    if "curve" in recs and len(recs["curve"]) == 0x242:
        fake = bytearray(0x30) + struct.pack(">II", 2, 0x242) + recs["curve"] + bytes(4)
        fake[0:4] = b"NCP\0"
        cr, _w = parse(bytes(fake), "x.ncp")
        for k in ("curve", "gamma", "levels", "lut"):
            if k in cr:
                r[k] = cr[k]
        if "lut" in r:
            r["lut_sig"] = lut_signature(with_defaults(r))
    else:
        r["curve"] = [[0, 0], [255, 255]]
        c, b = recs.get(8, b"\x80"), recs.get(9, b"\x80")
        if dq(c) or dq(b):
            warn.append(f"uses contrast {dq(c)} / brightness {dq(b)} instead of a curve; not carried over")
    return r, warn


def parse(d, filename=""):
    """Decode .NCP bytes into (recipe, warnings)."""
    if len(d) < 0x78 or d[:4] != b"NCP\0":
        raise ValueError(f"{filename or 'file'} is not an .NCP file")
    warn = []
    name = d[0x10:0x24].split(b"\0")[0].decode("ascii", "replace") or "Imported"
    base = {v: k for k, v in BASES.items()}.get(struct.unpack(">H", d[0x24:0x26])[0])
    if not base:
        base = "neutral"
        warn.append("unknown base, using Neutral")
    sv = lambda b, dflt: "auto" if b == 0 else dflt if b in (0xFF, 1) else b - 0x80
    curve = [[d[0x41 + 2 * i], d[0x42 + 2 * i]] for i in range(min(d[0x40], 20))]
    if len(curve) < 2:
        curve = [[0, 0], [255, 255]]
        warn.append("no custom curve in file; using linear")
    if d[0x29] != 1:
        warn.append("file used contrast/brightness instead of a curve; those aren't carried over")
    m = re.search(r"(\d{1,2})\.ncp$", filename, re.I)
    r = {"slot": int(m.group(1)) if m else 1, "name": name, "notes": "", "base": base,
         "sharpening": sv(d[0x28], 3), "curve": curve}
    g = d[0x3E] + d[0x3F] / 100
    if g and g != 1:
        r["gamma"] = round(g, 2)
    if d[0x26]:
        r["reserved"] = d[0x26]
    levels = list(d[0x3A:0x3E])
    lut = list(struct.unpack(">257H", d[0x78:0x78 + 514])) if len(d) >= 0x78 + 514 else None
    if lut:  # keep the file's exact tone table so an untouched import rebuilds byte-for-byte
        r["lut"], r["levels"] = lut, levels
        r["lut_sig"] = lut_signature(with_defaults(r))
        if levels != [0, 255, 0, 255]:
            warn.append(f"uses input/output levels {levels}; kept as long as the curve isn't edited")
    if base == "monochrome":
        r["filter"] = {v: k for k, v in FILTERS.items()}.get(d[0x2D], "off")
        r["toning"] = {v: k for k, v in TONINGS.items()}.get(d[0x2E], "bw")
        r["toning_strength"] = 4 if d[0x2F] == 0xFF else d[0x2F] - 0x80
    else:
        r["saturation"] = sv(d[0x2B], 0)
        r["hue"] = 0 if sv(d[0x2C], 0) == "auto" else sv(d[0x2C], 0)
    return r, warn


CAMERA_KEYS = ["wb", "wb_kelvin", "wb_ab", "wb_gm", "clarity"]
EXTRA_KEYS = ["gamma", "reserved", "levels", "lut_sig", "lut"]  # imported-file details

TOML_HEAD = """# Nikon Custom Picture Control recipes. Build with:  ncp   (or edit in Picture Control Studio)
#
# Fields:
#   slot        1-99  -> file PICCONxx.NCP
#   name        max 19 chars, shown on the camera
#   notes       free text for yourself (not written to the file)
#   install     true/false: include when installing to the SD card
#   base        standard | neutral | vivid | monochrome | portrait | landscape
#   sharpening  0-9 or "auto"
#   saturation  -3..3 or "auto"   (colour bases only)
#   hue         -3..3             (colour bases only; + pushes reds toward yellow)
#   filter      off | yellow | orange | red | green          (monochrome only)
#   toning      bw | sepia | cyanotype | red | yellow | green | blue-green |
#               blue | purple-blue | red-purple              (monochrome only)
#   toning_strength 1-7 (when toning isn't bw)
#   curve       2-20 [input, output] points, 0-255, x increasing
#   gamma       0.10-5.00 (default 1.00): bends the whole curve; >1 brightens the mid-tones
#   levels/lut  only on imported files: their exact tone table, used until the curve or gamma is edited
#
# White balance and Clarity can't be stored in .NCP; set them on the camera. The app keeps them as:
#   wb          auto | daylight | cloudy | shade | incandescent | fluorescent | flash | kelvin
#   wb_kelvin   colour temperature when wb = "kelvin"
#   wb_ab       fine-tune, + = A (amber), - = B (blue), -6..6
#   wb_gm       fine-tune, + = G (green), - = M (magenta), -6..6
#   clarity     -5..5
"""


def to_toml(recipes):
    def val(v):
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, float):
            return str(int(v)) if v == int(v) else repr(v)
        return str(v)
    s = TOML_HEAD
    for r0 in recipes:
        r = with_defaults(r0)
        s += f"\n[[recipe]]\nslot = {r['slot']}\nname = {val(r['name'])}\n"
        if r.get("notes"):
            s += f"notes = {val(r['notes'])}\n"
        if "install" in r:
            s += f"install = {val(bool(r['install']))}\n"
        s += f"base = {val(r['base'])}\nsharpening = {val(r['sharpening'])}\n"
        if r["base"] == "monochrome":
            s += f"filter = {val(r['filter'])}\ntoning = {val(r['toning'])}\n"
            if r["toning"] != "bw":
                s += f"toning_strength = {r['toning_strength']}\n"
        else:
            s += f"saturation = {val(r['saturation'])}\nhue = {r['hue']}\n"
        for k in CAMERA_KEYS:  # on-camera settings (not part of the .NCP), kept for the app
            if k in r:
                s += f"{k} = {val(r[k])}\n"
        if float(r.get("gamma", 1.0)) != 1.0:
            s += f"gamma = {val(round(float(r['gamma']), 2))}\n"
        if r.get("reserved"):
            s += f"reserved = {int(r['reserved'])}\n"
        if r.get("lut") and r.get("lut_sig"):
            s += f"levels = [{', '.join(str(int(v)) for v in r['levels'])}]\n"
            s += f"lut_sig = {val(r['lut_sig'])}\n"
            s += "lut = [" + ", ".join(str(int(v)) for v in r["lut"]) + "]\n"
        s += "curve = [" + ", ".join(f"[{x}, {y}]" for x, y in r["curve"]) + "]\n"
    return s


def load_recipes(path):
    return tomllib.loads(pathlib.Path(path).read_text()).get("recipe", [])


def local_path(path):
    """recipes.toml -> recipes.local.toml: personal recipes kept out of git."""
    p = pathlib.Path(path)
    return p.with_name(p.stem + ".local.toml")


def load_all(path):
    """Recipes from recipes.toml plus recipes.local.toml (if present); local ones are flagged."""
    path = pathlib.Path(path)
    rs = load_recipes(path) if path.exists() else []
    lp = local_path(path)
    if lp.exists():
        for r in load_recipes(lp):
            r["local"] = True
            rs.append(r)
    if not rs and not path.exists():
        raise FileNotFoundError(path)
    return sorted(rs, key=lambda r: int(r.get("slot", 99)) if str(r.get("slot", "")).isdigit() else 99)


LOCAL_HEAD = "# Personal recipes — kept out of git (see .gitignore). Same format as recipes.toml.\n"


def _atomic_write(path, text):
    tmp = pathlib.Path(str(path) + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def save_all(recipes, path):
    path = pathlib.Path(path)
    _atomic_write(path, to_toml([r for r in recipes if not r.get("local")]))
    local = [r for r in recipes if r.get("local")]
    lp = local_path(path)
    if local or lp.exists():
        _atomic_write(lp, LOCAL_HEAD + to_toml(local)[len(TOML_HEAD):])


def install(recipes, root):
    """Write recipes into <root>/NIKON/CUSTOMPC (or straight into root if it is NIKON or CUSTOMPC).
    Returns (dest_dir, added, replaced, unchanged) lists of file names."""
    root = pathlib.Path(root)
    if root.name.upper() == "CUSTOMPC":
        dest = root
    elif root.name.upper() == "NIKON":
        dest = root / "CUSTOMPC"
    else:
        dest = root / "NIKON" / "CUSTOMPC"
    datas = [(file_name(r), build_file(r), r) for r in recipes]  # validate all first
    dest.mkdir(parents=True, exist_ok=True)
    existing = {p.name.upper(): p for p in dest.iterdir()}
    added, replaced, same, retired = [], [], [], []
    for fname, data, r in datas:
        twin = existing.get(fname[:-3] + "NCP") if fname.endswith(".NP2") else None
        if twin:  # this recipe's older .NCP (same slot, same name) is superseded by the .NP2
            try:
                if parse(twin.read_bytes(), twin.name)[0]["name"] == r["name"]:
                    twin.unlink()
                    retired.append(twin.name)
            except (ValueError, OSError):
                pass
        old = existing.get(fname)
        if old and old.read_bytes() == data:
            same.append(fname)
            continue
        path = old or dest / fname
        with open(path, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        (replaced if old else added).append(fname)
    install.retired = retired
    return dest, added, replaced, same


def dump(path):
    d = pathlib.Path(path).read_bytes()
    if d[12:16] == b"0200":
        r, w = parse_np2(d, pathlib.Path(path).name)
        for k in ("name", "base", "sharpening", "clarity", "saturation", "hue", "gamma", "curve"):
            if k in r:
                print(f"{k:11s} {r[k]}")
        for x in w:
            print("note       ", x)
        return
    if d[:4] != b"NCP\0":
        sys.exit("not an NCP file")
    base = {v: k for k, v in BASES.items()}.get(struct.unpack(">H", d[0x24:0x26])[0], "unknown")
    sv = lambda b: "auto" if b == 0 else "n/a" if b == 0xFF else "curve" if b == 1 else b - 0x80
    n = d[0x40]
    name = d[0x10:0x24].split(b"\0")[0].decode()
    print(f"name        {name}")
    print(f"base        {base}")
    for label, off in [("sharpening", 0x28), ("contrast", 0x29), ("brightness", 0x2A),
                       ("saturation", 0x2B), ("hue", 0x2C)]:
        print(f"{label:11s} {sv(d[off])}")
    if base == "monochrome":
        print(f"filter      {({v: k for k, v in FILTERS.items()}).get(d[0x2D])}")
        print(f"toning      {({v: k for k, v in TONINGS.items()}).get(d[0x2E])} {sv(d[0x2F])}")
    print(f"curve       {[(d[0x41 + 2*i], d[0x42 + 2*i]) for i in range(n)]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("recipes", nargs="?", default=HERE / "recipes.toml")
    ap.add_argument("-o", "--outdir", default=HERE, type=pathlib.Path)
    ap.add_argument("--dump", metavar="FILE")
    ap.add_argument("--ncp", action="store_true", help="write the old .NCP format for every recipe (no Clarity/Flat)")
    a = ap.parse_args()
    if a.dump:
        return dump(a.dump)
    recipes = load_all(a.recipes)
    dest = a.outdir / "NIKON" / "CUSTOMPC"
    dest.mkdir(parents=True, exist_ok=True)
    slots = set()
    for r in recipes:
        slot = int(r["slot"])
        if not 1 <= slot <= 99 or slot in slots:
            sys.exit(f"{r['name']}: slot must be unique 1-99")
        slots.add(slot)
        try:
            data = build(r) if a.ncp else build_file(r)
        except ValueError as e:
            sys.exit(str(e))
        path = dest / (f"PICCON{slot:02d}.NCP" if a.ncp else file_name(r))
        for other in (".NCP", ".NP2"):  # drop the stale twin in the other format
            twin = path.with_suffix(other)
            if twin != path and twin.exists():
                twin.unlink()
        path.write_bytes(data)
        print(f"{path.name}  {r['name']}")


if __name__ == "__main__":
    main()
