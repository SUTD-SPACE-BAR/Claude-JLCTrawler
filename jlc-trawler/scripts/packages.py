"""
packages.py — Resolve a package NAME string into physical dimensions (mm).

The jlcparts database stores only the package name ("SOT-23-5", "0402",
"SMD,4x4x3mm"), never dimensions, so "smallest part that does X" needs
this mapping. Resolution is attempted in priority order:

  1. STATIC TABLE   — package_sizes.json next to this file. Exact-name
                      matches for common packages. Highest trust.
                      Can be extended/overridden by importing courtyard
                      sizes from KiCad's standard footprint libraries
                      (see import_kicad_courtyards at the bottom).
  2. NAME HEURISTIC — patterns computable from the name itself:
                      a. Imperial chip codes ("0402", "0805") → known mm.
                      b. Embedded dimensions ("SMD,4x4x3mm",
                         "QFN-24(4x4)") → parse the numbers directly.
  3. FAMILY GUESS   — package family prefix (SOT-23-*, SOIC-*, TQFP-*)
                      with a typical body size. Lowest trust.

Every result carries a `source` tag ("table" / "parsed" / "family" /
None) so search output can show how trustworthy the size ranking is.
Claude should treat "family" sizes as approximate when justifying picks.
"""

import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_TABLE_PATH = os.path.join(_HERE, "package_sizes.json")

# Loaded lazily and cached at module level (build touches this for every
# part, so avoid re-reading the JSON hundreds of thousands of times).
_TABLE = None


def _load_table():
    """Load package_sizes.json once. Keys are UPPERCASED package names,
    values are [length_mm, width_mm]."""
    global _TABLE
    if _TABLE is None:
        try:
            with open(_TABLE_PATH) as f:
                raw = json.load(f)
            # Normalize keys to uppercase so lookups are case-insensitive.
            _TABLE = {k.upper(): v for k, v in raw.items()}
        except FileNotFoundError:
            _TABLE = {}
    return _TABLE


# ---------------------------------------------------------------------------
# Imperial chip sizes (resistors/caps/inductors/LEDs/ferrites).
# Name IS the size: "0402" = 0.04in x 0.02in. Metric equivalents in mm.
# ---------------------------------------------------------------------------
_CHIP_SIZES = {
    "01005": (0.4, 0.2),
    "0201": (0.6, 0.3),
    "0402": (1.0, 0.5),
    "0603": (1.6, 0.8),
    "0805": (2.0, 1.25),
    "1206": (3.2, 1.6),
    "1210": (3.2, 2.5),
    "1812": (4.5, 3.2),
    "2010": (5.0, 2.5),
    "2512": (6.3, 3.2),
}

# ---------------------------------------------------------------------------
# Family fallbacks: typical BODY size for a package family when we know
# nothing else. Deliberately conservative/typical values; tagged "family"
# so downstream knows they're estimates. Pin-count-dependent families
# (QFN, QFP) are handled with embedded-dimension parsing first, since
# LCSC names usually include them: "QFN-24(4x4)".
# ---------------------------------------------------------------------------
_FAMILY_SIZES = [
    # (regex on uppercased name, (L, W))
    (r"^SOT-?23-?6", (2.9, 1.6)),
    (r"^SOT-?23-?5", (2.9, 1.6)),
    (r"^SOT-?23-?3", (2.9, 1.3)),
    (r"^SOT-?23\b", (2.9, 1.3)),
    (r"^SOT-?89", (4.5, 2.5)),
    (r"^SOT-?223", (6.5, 3.5)),
    (r"^SOT-?363|^SC-?70-?6", (2.0, 1.25)),
    (r"^SC-?70|^SOT-?323", (2.0, 1.25)),
    (r"^SOD-?123", (2.7, 1.6)),
    (r"^SOD-?323", (1.7, 1.25)),
    (r"^SOD-?523", (1.2, 0.8)),
    (r"^SOD-?723", (1.0, 0.6)),
    (r"^SMA\b", (4.3, 2.6)),
    (r"^SMB\b", (4.3, 3.6)),
    (r"^SMC\b", (6.6, 5.9)),
    (r"^SOIC-?8|^SOP-?8\b", (4.9, 3.9)),
    (r"^SOIC-?14|^SOP-?14", (8.7, 3.9)),
    (r"^SOIC-?16|^SOP-?16", (9.9, 3.9)),
    (r"^MSOP-?8", (3.0, 3.0)),
    (r"^MSOP-?10", (3.0, 3.0)),
    (r"^TSSOP-?14", (5.0, 4.4)),
    (r"^TSSOP-?16", (5.0, 4.4)),
    (r"^TSSOP-?20", (6.5, 4.4)),
    (r"^SOT-?563", (1.6, 1.2)),
    (r"^DFN-?6", (2.0, 2.0)),
    (r"^DFN-?8", (3.0, 3.0)),
    (r"^TO-?252|^DPAK", (6.6, 6.1)),
    (r"^TO-?263|^D2PAK|^DDPAK", (10.2, 9.2)),
    (r"^ESOP-?8|^SOP-?8-?EP", (4.9, 3.9)),
]

# Embedded-dimension patterns, tried in order:
#   "QFN-24(4x4)"        → 4 x 4
#   "SMD,4x4x3mm"        → 4 x 4 (third number is height; ignored)
#   "6.6x6.6x3mm"        → 6.6 x 6.6
#   "LQFP-48(7x7)"       → 7 x 7
_DIM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)(?:\s*[xX×*]\s*\d+(?:\.\d+)?)?\s*(?:MM)?"
)

# Tantalum/polymer-chip EIA METRIC case codes: "CASE-D-7343-31(mm)",
# "CASE-A-3216-18(mm)". The 4 digits after the case letter are length and
# width in 0.1mm units (7343 = 7.3 x 4.3mm); the trailing group is height,
# ignored. Anchored on "CASE" so it can never collide with the IMPERIAL
# chip codes above (0402 etc.), which use the same 4-digit shape but a
# completely different unit system.
_EIA_CASE_RE = re.compile(r"CASE[-A-Z]*-(\d{2})(\d{2})")

# Cylindrical cans (polymer/electrolytic): "SMD,D8xL12mm", "Plugin,D5.5xL9mm".
# D is the can DIAMETER, L is its HEIGHT — the board footprint is the D x D
# bounding box, so the L number must NOT be used as a planar dimension
# (the generic _DIM_RE would have gotten this wrong had it matched).
_CAN_RE = re.compile(r"D\s*(\d+(?:\.\d+)?)\s*[xX×]\s*L?\s*(?:\d+(?:\.\d+)?)\s*MM")


def resolve_package_size(package_name):
    """Package name string → dict {l_mm, w_mm, area_mm2, source} or all-None.

    See module docstring for the resolution priority. Never raises.
    """
    empty = {"l_mm": None, "w_mm": None, "area_mm2": None, "source": None}
    if not package_name:
        return empty
    name = str(package_name).strip().upper()

    # --- 1. static table (exact name) ---
    table = _load_table()
    if name in table:
        l, w = table[name]
        return {"l_mm": l, "w_mm": w, "area_mm2": round(l * w, 3), "source": "table"}

    # --- 2a. imperial chip code (exact or as leading token: "0402" in "LED,0402") ---
    for code, (l, w) in _CHIP_SIZES.items():
        # Match code as its own token to avoid "10402" false positives.
        if re.search(rf"(?<![0-9]){code}(?![0-9])", name):
            return {"l_mm": l, "w_mm": w, "area_mm2": round(l * w, 3), "source": "parsed"}

    # --- 2b-pre. EIA metric case codes and cylindrical cans ---
    # Both MUST run before the generic dimension parse: the can pattern
    # because _DIM_RE would misread the can height as a planar dimension,
    # the case code because it has no 'x' separator at all.
    m = _EIA_CASE_RE.search(name)
    if m:
        l, w = int(m.group(1)) / 10.0, int(m.group(2)) / 10.0
        if 0.2 <= l <= 60 and 0.2 <= w <= 60:
            return {"l_mm": max(l, w), "w_mm": min(l, w),
                    "area_mm2": round(l * w, 3), "source": "parsed"}
    m = _CAN_RE.search(name)
    if m:
        d = float(m.group(1))
        if 0.2 <= d <= 60:
            return {"l_mm": d, "w_mm": d,
                    "area_mm2": round(d * d, 3), "source": "parsed"}

    # --- 2b. dimensions embedded in the name ---
    m = _DIM_RE.search(name)
    if m:
        l, w = float(m.group(1)), float(m.group(2))
        # Sanity clamp: PCB parts bigger than 60mm on a side are almost
        # certainly a mis-parse (e.g. a frequency in the name).
        if 0.2 <= l <= 60 and 0.2 <= w <= 60:
            return {"l_mm": max(l, w), "w_mm": min(l, w),
                    "area_mm2": round(l * w, 3), "source": "parsed"}

    # --- 3. family fallback ---
    for pat, (l, w) in _FAMILY_SIZES:
        if re.search(pat, name):
            return {"l_mm": l, "w_mm": w, "area_mm2": round(l * w, 3), "source": "family"}

    return empty


# ---------------------------------------------------------------------------
# OPTIONAL, run manually: harvest authoritative courtyard sizes from a
# KiCad installation's standard footprint libraries into package_sizes.json.
#
#   python3 packages.py import-kicad /usr/share/kicad/footprints
#   python3 packages.py import-kicad "C:/Program Files/KiCad/9.0/share/kicad/footprints"
#
# For every .kicad_mod file, we compute the bounding box of the
# F.CrtYd (courtyard) layer lines and register it under simplified names
# derived from the footprint filename. Existing table entries are kept
# (hand-curated values win); only new names are added.
# ---------------------------------------------------------------------------
def import_kicad_courtyards(footprints_root):
    """Walk KiCad .pretty libraries, extract courtyard bounding boxes,
    merge new entries into package_sizes.json. Returns count added."""
    added = 0
    table = dict(_load_table())

    # Regex over the s-expression text: courtyard graphic lines look like
    #   (fp_line (start -1.7 -1.2) (end 1.7 -1.2) (layer "F.CrtYd") ...
    # We scan coordinates of any fp_line/fp_rect that mentions F.CrtYd.
    coord_re = re.compile(r"\((?:start|end)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\)")

    for dirpath, _dirs, files in os.walk(footprints_root):
        if not dirpath.endswith(".pretty"):
            continue
        for fn in files:
            if not fn.endswith(".kicad_mod"):
                continue
            try:
                with open(os.path.join(dirpath, fn), errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            xs, ys = [], []
            # Only consider lines/rects on the courtyard layer: split the
            # file into s-expression chunks per graphic item, keep those
            # that mention F.CrtYd, then collect their coordinates.
            for chunk in re.split(r"\(fp_", text):
                if "F.CrtYd" not in chunk:
                    continue
                for m in coord_re.finditer(chunk):
                    xs.append(float(m.group(1)))
                    ys.append(float(m.group(2)))
            if not xs:
                continue
            l = round(max(xs) - min(xs), 2)
            w = round(max(ys) - min(ys), 2)
            if l <= 0 or w <= 0:
                continue
            # Derive candidate package names from the filename, e.g.
            # "SOT-23-5.kicad_mod" -> "SOT-23-5";
            # "R_0402_1005Metric.kicad_mod" -> handled by chip heuristic
            # already, so table entry is harmless duplication.
            name = fn[:-len(".kicad_mod")].upper()
            if name not in table:
                table[name] = [max(l, w), min(l, w)]
                added += 1

    with open(_TABLE_PATH, "w") as f:
        json.dump(dict(sorted(table.items())), f, indent=1)
    return added


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "import-kicad":
        n = import_kicad_courtyards(sys.argv[2])
        print(f"Added {n} package sizes from KiCad courtyards -> {_TABLE_PATH}")
        sys.exit(0)

    # Self-test of the resolver priority chain.
    tests = [
        ("SOT-23-5", "family_or_table"),
        ("0402", "parsed"),
        ("QFN-24(4x4)", "parsed"),
        ("SMD,4x4x3mm", "parsed"),
        ("SOIC-8", "family_or_table"),
        ("TO-252-2(DPAK)", "family_or_table"),
        ("CASE-D-7343-31(mm)", "parsed"),   # EIA case: expect 7.3 x 4.3
        ("CASE-A-3216-18(mm)", "parsed"),   # EIA case: expect 3.2 x 1.6
        ("SMD,D8xL12mm", "parsed"),         # can: expect 8 x 8 (D x D)
        ("Plugin,D5.5xL9mm", "parsed"),     # can: expect 5.5 x 5.5
        ("WEIRD-UNKNOWN-99", None),
    ]
    for name, expect in tests:
        r = resolve_package_size(name)
        status = "ok"
        if expect is None and r["source"] is not None:
            status = "FAIL (expected no match)"
        if expect is not None and r["source"] is None:
            status = "FAIL (expected match)"
        print(f"{name:24s} -> {r}  [{status}]")
