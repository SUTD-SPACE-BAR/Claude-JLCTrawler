"""
units.py — Parse raw LCSC attribute strings into normalized numbers.

LCSC attribute values arrive as messy human-readable strings like:
    "1.2A"          "150mA"       "3.3V"        "1.8V~5.5V"
    "10uH"          "±1%"         "100mΩ"       "22µF"
    "-40℃~+85℃"   "0.5A @ 5V"   "600mV @ 1A"  "1MHz"
    "-"             ""            "Shielded"

This module converts them into (value_min, value_max) pairs in SI base
units (volts, amps, ohms, farads, henries, watts, hertz, seconds, °C,
fraction-for-percent). Single values are returned with min == max.

Design rules (keep these in mind when editing):
  * NEVER raise on garbage input — return None instead. The build step
    processes hundreds of thousands of parts; one weird string must not
    kill the run. Unparseable values stay available as raw text.
  * When a value has an "@ condition" suffix (e.g. "600mV @ 1A"), we
    parse the FIRST quantity and ignore the condition. The raw string is
    always preserved in the DB so the condition is never lost.
  * Ranges ("a~b", "a-b", "a to b") return (low, high) sorted.
"""

import re

# ---------------------------------------------------------------------------
# SI prefix table.
# Maps every prefix spelling we've seen in LCSC data to its multiplier.
# Note the unicode traps: µ (U+00B5), μ (U+03BC Greek mu) and plain "u"
# all mean micro. "K" uppercase appears sometimes ("10KΩ").
# ---------------------------------------------------------------------------
_SI_PREFIXES = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3, "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}

# ---------------------------------------------------------------------------
# Unit families.
# Each parser kind (declared in a recipe as "parse": "current" etc.) maps
# to the set of unit spellings that terminate a quantity of that kind.
# Longer spellings must be listed before shorter ones that prefix them
# (e.g. "mΩ" is handled by prefix logic, but "Ohm"/"ohm"/"R" need care).
# ---------------------------------------------------------------------------
_UNIT_FAMILIES = {
    "voltage":     ["V"],
    "current":     ["A"],
    "resistance":  ["Ω", "Ohm", "ohm", "R"],
    "capacitance": ["F"],
    "inductance":  ["H"],
    "power":       ["W"],
    "frequency":   ["Hz"],
    "time":        ["s", "S"],
    "percent":     ["%"],
    "temperature": ["℃", "°C", "C"],
    # "memsize": memory capacity ("64Kbit", "128Mbit", "2Kb") → BITS.
    # Special-cased in _parse_single (binary 1024 prefixes, bit-vs-Byte),
    # so this spelling list is documentation; presence of the key is what
    # makes "parse": "memsize" legal in recipes (builder._VALID_PARSERS).
    "memsize":     ["bit", "B", "b"],
    # "number": bare numeric, no unit expected (pin counts, channel counts…)
    "number":      [],
}

# Pre-compiled: a number like "1.2", "-40", "+85", ".5", "1,000" (comma stripped later)
_NUM = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"

# Range separators seen in LCSC data. "~" is by far the most common.
# We intentionally do NOT treat bare "-" between numbers as a range when the
# second number could be a negative sign target — handled in _split_range.
_RANGE_SEPS = ["~", "～", " to ", ".."]


def _clean(s):
    """Normalize whitespace/commas/unicode oddities before parsing.

    Steps:
      1. Strip leading/trailing whitespace.
      2. Remove thousands-separator commas INSIDE numbers ("1,000" -> "1000").
      3. Normalize the various unicode minus/dash characters to ASCII "-".
      4. Collapse internal whitespace.
    """
    if s is None:
        return ""
    s = str(s).strip()
    # Remove commas only when they sit between digits (thousands separators)
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    # Unicode dashes → ASCII hyphen-minus
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    # Fullwidth tilde → ASCII (used in ranges by Chinese-origin data)
    s = s.replace("～", "~")
    s = re.sub(r"\s+", " ", s)
    return s


def _strip_condition(s):
    """Drop an '@ condition' suffix: '600mV @ 1A' -> '600mV'.

    The condition is measurement context (e.g. dropout voltage measured at a
    load current). We keep only the primary quantity; the raw string remains
    stored in the DB for anyone who needs the condition.
    """
    at = s.find("@")
    if at != -1:
        return s[:at].strip()
    return s


def _parse_single(s, kind):
    """Parse ONE quantity string of a given unit family -> float in SI base.

    Returns None if the string doesn't contain a parseable quantity.

    Logic flow:
      1. Regex-match: number + optional SI prefix + unit spelling.
         The unit spelling must match one of the family's suffixes
         (or nothing, for kind == "number").
      2. Multiply number by prefix multiplier.
      3. Special cases: percent → fraction (1% -> 0.01),
         resistance "R" notation ("4R7" -> 4.7 handled separately).
    """
    s = s.strip()
    if not s or s in ("-", "—", "N/A", "n/a", "null"):
        return None

    # --- special case: resistor "R/K/M as decimal point" notation (4R7, 1K2) ---
    if kind == "resistance":
        m = re.fullmatch(r"(\d+)([RrKkMm])(\d+)", s)
        if m:
            mult = {"r": 1.0, "k": 1e3, "m": 1e6}[m.group(2).lower()]
            return (float(m.group(1)) + float(m.group(3)) / (10 ** len(m.group(3)))) * mult

    unit_spellings = _UNIT_FAMILIES.get(kind, [])

    if kind == "number":
        # Bare numeric — grab the first number in the string.
        m = re.search(_NUM, s)
        return float(m.group(0)) if m else None

    if kind == "percent":
        # This "percent" kind really means "dimensionless ratio", stored as
        # a fraction (1% -> 0.01). Two spellings appear in the data:
        #
        #   * PERCENT ("±1%", "1%", "+80%/-20%") — common for capacitor,
        #     resistor, inductor tolerances. Divide by 100.
        #   * PPM  ("±10ppm", "30ppm", "±50 ppm") — parts-per-million, used
        #     for crystal frequency tolerance/stability and precision-
        #     reference specs. Divide by 1,000,000.
        #
        # We must check ppm FIRST: a value like "10ppm" contains no "%", but
        # a value like "10%" contains no "ppm", so the two are unambiguous —
        # but doing ppm first keeps the intent obvious and avoids the "%"
        # regex ever seeing a stray digit from something odd.
        #
        # For asymmetric ranges ("+80%/-20%") we take the first magnitude;
        # for symmetric "±" we drop the sign and keep the magnitude. Callers
        # that need both bounds should use range handling upstream — this
        # single-value parser returns one representative fraction.
        m_ppm = re.search(rf"±?\s*({_NUM})\s*ppm", s, re.IGNORECASE)
        if m_ppm:
            return abs(float(m_ppm.group(1))) / 1_000_000.0
        m_pct = re.search(rf"±?\s*({_NUM})\s*%", s)
        if m_pct:
            return abs(float(m_pct.group(1))) / 100.0
        return None

    if kind == "temperature":
        # "-40℃" / "+85°C" / "125C" — prefix logic doesn't apply to temps.
        m = re.search(rf"({_NUM})\s*(?:℃|°C|C)(?![A-Za-z])", s)
        if m:
            return float(m.group(1))
        return None

    if kind == "memsize":
        # Memory capacity → BITS. LCSC writes "64Kbit", "128Mbit", "2Kb",
        # occasionally byte-denominated "16MB". Conventions here differ
        # from SI, which is why the generic prefix logic can't be reused:
        #   * prefixes are BINARY (K=1024, M=1024², G=1024³) — a "2Kbit"
        #     24C02 EEPROM is exactly 2048 bits;
        #   * prefix case is meaningless ("K" and "k" are both kilo — there
        #     is no milli-bit for lowercase to denote);
        #   * unit case matters: 'bit'/'b' = bits, 'Byte'/'B' = ×8.
        m = re.search(
            rf"({_NUM})\s*([KkMmGg])?\s*([Bb]its?|[Bb]ytes?|B|b)(?![A-Za-z])", s)
        if m:
            mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[
                (m.group(2) or "").upper()]
            unit = m.group(3)
            byte_factor = 8 if ("yte" in unit or unit == "B") else 1
            return float(m.group(1)) * mult * byte_factor
        return None

    # --- general case: number + prefix + unit ---
    # Build alternation of unit spellings, longest first so "Ohm" wins over "O".
    spellings = sorted(unit_spellings, key=len, reverse=True)
    unit_re = "|".join(re.escape(u) for u in spellings)
    prefix_re = "|".join(re.escape(p) for p in _SI_PREFIXES if p)

    # Trailing (?![A-Za-z]) guard: the unit must not be immediately followed
    # by another letter, so "V" won't match inside "Vdss" or "VDC"… actually
    # "VDC" should parse as volts, so we allow it explicitly first.
    s_v = re.sub(r"(?<=\d)\s*VDC\b", "V", s) if kind == "voltage" else s
    m = re.search(rf"({_NUM})\s*({prefix_re})?({unit_re})(?![A-Za-z])", s_v)
    if m:
        num = float(m.group(1))
        prefix = m.group(2) or ""
        # Disambiguation: "m" before "Hz" could be milli or Mega typo; LCSC
        # writes MHz correctly, so trust the case as-is.
        return num * _SI_PREFIXES[prefix]

    # Fallback: bare number with no unit at all ("5" for "5V" column).
    m = re.fullmatch(_NUM, s)
    if m:
        return float(m.group(0))

    return None


def _split_range(s):
    """Split a string on a range separator into (low_str, high_str) or None.

    Handles:
      "1.8V~5.5V"      → ("1.8V", "5.5V")
      "-40℃~+85℃"     → ("-40℃", "+85℃")
      "10 to 20"       → ("10", "20")
      "2.5V-5.5V"      → ("2.5V", "5.5V")   (dash range, both sides positive)

    We only treat "-" as a range separator when it sits between two
    complete quantities and the left side isn't just a sign — this avoids
    mangling "-40" into a range.
    """
    for sep in _RANGE_SEPS:
        if sep in s:
            parts = s.split(sep, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return parts[0], parts[1]

    # Cautious dash-range detection: "2.5V-5.5V" but NOT "-40C".
    m = re.fullmatch(rf"\s*({_NUM}\s*[^\s-]*)\s*-\s*({_NUM}.*)", s)
    if m and not s.lstrip().startswith("-"):
        return m.group(1), m.group(2)

    return None


def parse_quantity(raw, kind):
    """Public entry point. raw string + parser kind -> (min, max) or None.

    Examples:
        parse_quantity("1.2A", "current")          -> (1.2, 1.2)
        parse_quantity("150mA", "current")         -> (0.15, 0.15)
        parse_quantity("1.8V~5.5V", "voltage")     -> (1.8, 5.5)
        parse_quantity("600mV @ 1A", "voltage")    -> (0.6, 0.6)
        parse_quantity("±1%", "percent")           -> (0.01, 0.01)
        parse_quantity("Shielded", "current")      -> None
    """
    s = _clean(raw)
    if not s:
        return None
    s = _strip_condition(s)

    rng = _split_range(s)
    if rng:
        lo = _parse_single(rng[0], kind)
        hi = _parse_single(rng[1], kind)
        if lo is not None and hi is not None:
            return (min(lo, hi), max(lo, hi))
        # If only one side parsed, fall through and try as single value.

    v = _parse_single(s, kind)
    if v is not None:
        return (v, v)
    return None


def parse_flag(raw, truthy_patterns, false_patterns=None):
    """Parse a categorical value into a flag: 1 (true), 0 (false), or None
    (unknown). `raw` may be a short attribute value ("Shielded") OR a whole
    description string that mentions the property in passing ("SMD Power
    Inductor 10uH Shielded 20%").

    Matching logic (all patterns are case-insensitive regexes):

      1. If any `truthy_patterns` regex matches  -> 1
      2. Else, behaviour depends on whether `false_patterns` was supplied:
         * false_patterns GIVEN (ternary/honest mode):
             - if any false pattern matches       -> 0
             - if none match                       -> None  (UNKNOWN: the
               text simply never spoke to this property, so we must NOT
               assert it's false)
         * false_patterns OMITTED (legacy binary mode):
             - non-empty input, no truthy match    -> 0  (the value was a
               real attribute string that explicitly wasn't "true", so
               calling it false is reasonable)
      3. Empty/"-" input                           -> None (nothing to judge)

    WHY THE TWO MODES: when a flag is sourced from a dedicated attribute
    ("Shielding" = "Shielded"/"Unshielded"), a non-truthy value genuinely
    means false, so binary mode is right. But when the flag is scavenged
    from a free-text description (because LCSC has no shielding attribute),
    most descriptions never mention shielding at all — treating that
    silence as "unshielded" would flood the column with false 0s and make
    `shielded = 0` searches meaningless. Ternary mode keeps unknowns NULL.

    CRUCIAL SUBSTRING TRAP: "unshielded" contains "shielded". A truthy
    pattern of bare "shielded" would wrongly fire on "unshielded". Recipes
    therefore use a negative lookbehind like "(?<!un)shielded" so the
    truthy pass skips the negated form, and list "unshielded" among the
    false_patterns so it's classified as 0, not None.
    """
    s = _clean(raw)
    if not s or s == "-":
        return None
    for pat in truthy_patterns:
        if re.search(pat, s, re.IGNORECASE):
            return 1
    if false_patterns is not None:
        for pat in false_patterns:
            if re.search(pat, s, re.IGNORECASE):
                return 0
        return None  # ternary mode: no signal either way -> unknown
    return 0  # binary mode: real value that simply wasn't truthy -> false


# ---------------------------------------------------------------------------
# Self-test: run `python3 units.py` to sanity-check the parser against the
# messiest real-world strings we know about. Kept inline so anyone editing
# the regexes can instantly verify nothing broke.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cases = [
        # (raw, kind, expected (min,max) or None)
        ("1.2A", "current", (1.2, 1.2)),
        ("150mA", "current", (0.15, 0.15)),
        ("3.3V", "voltage", (3.3, 3.3)),
        ("1.8V~5.5V", "voltage", (1.8, 5.5)),
        ("600mV @ 1A", "voltage", (0.6, 0.6)),
        ("±1%", "percent", (0.01, 0.01)),
        ("±10ppm", "percent", (1e-5, 1e-5)),
        ("30ppm", "percent", (3e-5, 3e-5)),
        ("±50 ppm", "percent", (5e-5, 5e-5)),
        ("-40ppm~+40ppm", "percent", (4e-5, 4e-5)),
        ("10uH", "inductance", (1e-5, 1e-5)),
        ("22µF", "capacitance", (2.2e-5, 2.2e-5)),
        ("100mΩ", "resistance", (0.1, 0.1)),
        ("4R7", "resistance", (4.7, 4.7)),
        ("1K2", "resistance", (1200.0, 1200.0)),
        ("10KΩ", "resistance", (10000.0, 10000.0)),
        ("-40℃~+85℃", "temperature", (-40.0, 85.0)),
        ("2.5V-5.5V", "voltage", (2.5, 5.5)),
        ("1MHz", "frequency", (1e6, 1e6)),
        ("100kHz", "frequency", (1e5, 1e5)),
        ("1,000pF", "capacitance", (1e-9, 1e-9)),
        ("-", "current", None),
        ("", "voltage", None),
        ("Shielded", "current", None),
        ("5", "voltage", (5.0, 5.0)),
        ("0.5W", "power", (0.5, 0.5)),
        ("64", "number", (64.0, 64.0)),
        # memsize: binary prefixes, bit vs Byte (see _parse_single)
        ("64Kbit", "memsize", (65536.0, 65536.0)),
        ("128Mbit", "memsize", (134217728.0, 134217728.0)),
        ("2Kb", "memsize", (2048.0, 2048.0)),
        ("1Gbit", "memsize", (1073741824.0, 1073741824.0)),
        ("16MB", "memsize", (134217728.0, 134217728.0)),
        ("512bit", "memsize", (512.0, 512.0)),
    ]
    failed = 0
    for raw, kind, expect in cases:
        got = parse_quantity(raw, kind)
        ok = (got == expect) if expect is None else (
            got is not None
            and abs(got[0] - expect[0]) < 1e-12 * max(1, abs(expect[0]))
            and abs(got[1] - expect[1]) < 1e-12 * max(1, abs(expect[1]))
        )
        if not ok:
            failed += 1
            print(f"FAIL {raw!r} ({kind}): expected {expect}, got {got}")
    flag_cases = [
        # binary mode (no false_patterns): non-match -> 0
        ("Shielded", ["shield"], None, 1),
        ("Unshielded", ["^shielded|^semi"], None, 0),
        ("Semi-Shielded", ["^shielded|^semi"], None, 1),
        ("-", ["shield"], None, None),
        # ternary mode (with false_patterns): the shielding-from-description
        # scenario. "(?<!un)shielded" must fire on shielded/semi-shielded but
        # NOT on unshielded; a description with no shielding word -> None.
        ("SMD Power Inductor 10uH Shielded", ["(?<!un)shielded"], ["unshielded"], 1),
        ("Wirewound Semi-shielded 4.7uH", ["(?<!un)shielded"], ["unshielded"], 1),
        ("Power Inductor Unshielded 22uH", ["(?<!un)shielded"], ["unshielded"], 0),
        ("Power Inductor 10uH 1A 20%", ["(?<!un)shielded"], ["unshielded"], None),
    ]
    for raw, pats, false_pats, expect in flag_cases:
        got = parse_flag(raw, pats, false_pats)
        if got != expect:
            failed += 1
            print(f"FAIL flag {raw!r}: expected {expect}, got {got}")
    print(f"{'ALL PASS' if failed == 0 else str(failed) + ' FAILURES'} "
          f"({len(cases) + len(flag_cases)} cases)")
