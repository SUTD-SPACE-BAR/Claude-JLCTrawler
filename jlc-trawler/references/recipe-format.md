# Recipe format & database schema reference

## Recipe JSON format

One file per component category in `scripts/recipes/<name>.json`:

```json
{
  "name": "ldo",                       // snake_case; table becomes t_ldo
  "display": "LDO linear regulators",  // human label shown by describe
  "category_match": {
    // Case-insensitive re.search against the source DB's category and
    // subcategory strings INDEPENDENTLY. Either key may be omitted.
    // A recipe matching zero categories triggers a loud build WARNING.
    "category": "Power Management",
    "subcategory": "Linear Voltage Regulators|LDO"
  },
  "columns": [
    {
      "name": "output_current_a",      // snake_case, unit-suffixed by convention
      "parse": "current",              // parser kind, see table below
      "range": "max",                  // which bound of a parsed range to keep
      "attrs": ["Output Current", "Current - Output"]   // alias list
    },
    {
      "name": "shielded",              // flag column: no "parse" key
      "flag": ["^shielded", "semi[- ]?shield"],   // regexes meaning "true"
      "attrs": ["Shielding"]
    }
  ]
}
```

### Parser kinds (`parse`)

| kind | SI base unit stored | example raw inputs |
|---|---|---|
| voltage | V | "3.3V", "1.8V~5.5V", "600mV @ 1A" |
| current | A | "1.2A", "150mA" |
| resistance | Ω | "100mΩ", "4R7", "10KΩ" |
| capacitance | F | "22uF", "1,000pF" |
| inductance | H | "10uH" |
| power | W | "0.5W", "62.5mW" |
| frequency | Hz | "500kHz", "16MHz" |
| time | s | "10ms" |
| percent | fraction | "±1%" → 0.01 |
| temperature | °C | "-40℃~+85℃" |
| number | bare float | "64", pin/channel counts |
| text | verbatim string | "Fixed", "X7R", "N-Channel" |

### Range handling (`range`)

Every numeric raw value parses to a `(min, max)` pair (equal for single
values). `range` selects what's stored:

- `"max"` (default): the upper bound — right for ratings ("up to 1.2A").
- `"min"`: the lower bound — right for "at least" specs (e.g. minimum
  self-resonant frequency, minimum breakdown voltage).
- `"both"`: TWO columns are generated, `<name>_min` and `<name>_max` —
  right for operating ranges (input voltage span). Filter with e.g.
  `vin_v_max >= rail AND vin_v_min <= rail`.

### Flag columns (`flag`, optional `flag_false`, optional `from_description`)

Categorical yes/no. `flag` is a list of case-insensitive regexes meaning
"true". Two refinements handle the messy real cases:

- **`flag_false`** (optional list of regexes): makes the flag *ternary* /
  honest. With it, the result is 1 if a truthy pattern matches, 0 only if
  a `flag_false` pattern matches, and **NULL if neither matches** (the
  source text simply didn't speak to the property, so we don't assert
  false). Without `flag_false`, the flag is binary: any non-truthy
  non-empty value is 0. Use `flag_false` whenever silence should mean
  "unknown" rather than "false" — especially for `from_description` flags.
- **`from_description`** (optional bool): when true, if the attribute
  lookup yields nothing usable, the flag/value is computed from the
  part's **description string** instead. Essential for properties LCSC
  doesn't expose parametrically — the canonical case is inductor
  **shielding**, which exists only as a word in the description.

Substring trap to remember: `"unshielded"` contains `"shielded"`. Truthy
patterns use a negative lookbehind — `"(?<!un)shielded"` — so they fire on
shielded / semi-shielded but skip unshielded, and `"unshielded"` goes in
`flag_false`. Per design decision, semi-shielded counts as `shielded = 1`.

Example (the real inductor shielding column):
```json
{"name": "shielded",
 "flag": ["(?<!un)shielded", "semi[- ]?shield", "\\bmolded\\b"],
 "flag_false": ["unshielded", "non[- ]?shielded", "no shield"],
 "from_description": true,
 "attrs": ["Shielding", "Shielded", "Construction", "Type"]}
```

### Inline comments in the columns array

JSON has no comment syntax, but recipe `columns` arrays may contain plain
**string** entries interleaved among the column objects. The builder
treats any non-object entry as a comment and skips it, so you can
document non-obvious columns in place:
```json
"columns": [
  "__note__: Vgs max must not alias bare 'Vgs' — it would grab the threshold key.",
  {"name": "vgs_max_v", "parse": "voltage", "range": "max", "attrs": [...]}
]
```

### Note on ppm (percent parser)

The `percent` parser kind understands both `%` and `ppm`, normalizing
each to a dimensionless fraction (`±10ppm` → 1e-5, `±1%` → 0.01). This is
why crystal frequency tolerance/stability columns — specified in ppm —
use `"parse": "percent"`.

### Attribute alias matching (`attrs`)

For each part, the first alias that matches wins. Matching passes:
1. exact key match (case-insensitive)
2. alias as substring of a key (handles "Output Current (Max)" etc.)
Order aliases most-specific-first; a too-generic alias placed first can
shadow the right key via substring matching.

### Reserved column names

These are built-in on every table and cannot be recipe column names:
lcsc, mpn, manufacturer, category, subcategory, package, joints, basic,
preferred, description, datasheet, stock, price_json, price_q1,
price_q10, price_q100, price_q1000, l_mm, w_mm, area_mm2, size_source,
attrs_json.

## Derived DB (trawl.sqlite3) schema

- `meta(key, value)`: `recipes` (the recipe set as JSON — search reads
  column kinds from here), `source_db`, `built_at`.
- `t_<recipe>`: the built-in columns above + one column per recipe
  column (two for `range: both`). `attrs_json` holds the merged raw
  attribute dict verbatim, so no information is ever more than one JSON
  parse away. Indexes exist on every numeric recipe column plus stock,
  area_mm2, price_q100, basic, preferred, package.

## Source DB facts worth remembering

- `components.lcsc` is INTEGER without the "C" prefix; the CLI adds/strips it.
- Attributes live in `extra` JSON (LCSC) overlaid by `jlc_extra` (JLC),
  where overlay values of "" or "-" are ignored — identical merge rule
  to the upstream jlcparts project.
- `price` is a JSON list of `{qFrom, qTo, price}` tiers; `qTo` null/0
  means "and up".
- CDFER's variant drops parts with stock < 5 and adds an FTS5 table
  `components_fts(lcsc, mfr, package, description, datasheet)` usable for
  free-text lookups directly against the source DB if ever needed.

## Package size resolution (packages.py)

Priority: exact match in `package_sizes.json` ("table") → imperial chip
code or dimensions embedded in the name ("parsed") → package family
typical size ("family") → unknown (NULLs; part ranks last on size).
`size_source` on every row records which tier fired.

To harvest authoritative courtyard sizes from a local KiCad install:
```bash
python3 scripts/packages.py import-kicad /usr/share/kicad/footprints
# macOS: /Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints
# Windows: "C:/Program Files/KiCad/9.0/share/kicad/footprints"
```
Existing table entries win; only new names are added. Rebuild afterwards.
