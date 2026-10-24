---
name: jlc-trawler
description: Parametric search of the JLCPCB/LCSC assembly parts catalogue from a local database. Use this skill whenever the user wants to find, compare, or choose electronic components for a PCB — e.g. "smallest LDO that does 1A at 3.3V", "cheapest shielded inductor rated 2A", "find me a MOSFET for this load switch", "what basic part resistors are available", or any mention of LCSC part numbers, JLCPCB assembly parts, BOM cost optimization, or component selection. Also use it to update/maintain the local parts database or to add new component category recipes.
---

# JLC Trawler — parametric JLCPCB/LCSC part search

Local-first parametric search over JLCPCB's SMT assembly catalogue.
Data foundation: the community jlcparts database (JLC part list enriched
with LCSC attributes), downloaded once and rebuilt into fast typed SQLite
tables. Searching never touches the network; only the optional `refresh`
command does.

All commands run through one CLI:

```bash
python3 scripts/trawl.py [--dir DATA_DIR] <command> ...
```

`DATA_DIR` resolution: `--dir` flag > `$TRAWL_DATA` env var >
`~/jlc-trawler-data`. It holds `jlcpcb-components.sqlite3` (the ~1GB
source download) and `trawl.sqlite3` (derived typed tables).

## The golden workflow for a part search

**Never guess column names or plausible value ranges. Introspect first.**

1. **Pick the recipe table.** Current recipes: `ldo`, `dcdc`,
   `inductor_power`, `ferrite_bead`, `mlcc`, `resistor_chip`, `mosfet`,
   `diode_schottky`, `diode_zener`, `diode_rectifier`, `tvs`, `led`,
   `crystal`, `cap_polymer`, `cap_tantalum`, `cap_electrolytic`,
   `cap_film`, `cap_safety`, `supervisor_reset`, `vref`, `power_switch`,
   `memory`. If unsure which categories exist or a recipe is missing,
   run `categories --grep <term>`.
   Routing notes:
   - Diodes: Schottky → `diode_schottky`; zener → `diode_zener`;
     everything else (general purpose, switching/1N4148, fast recovery,
     SBR, bridges) → `diode_rectifier`, routed within by
     `--where "subcategory ~ Fast"` / `~ Switching` / `~ Bridge`; filter
     `trr_s` for recovery speed. TVS/ESD → `tvs`, where uni/bi is the
     ternary `bidirectional` flag (1 bi, 0 uni, NULL unstated).
   - Capacitors: ceramics → `mlcc`; low-ESR bulk (polymer aluminum +
     solid polymer + "Solid Capacitors") → `cap_polymer`; cheap bulk
     liquid aluminum → `cap_electrolytic` (ripple is specced @120Hz, vs
     @100kHz for polymers — not directly comparable); tantalum →
     `cap_tantalum`; snubber/DC-link film → `cap_film`; mains X/Y →
     `cap_safety` (filter `subclass ~ X2` etc. and `voltage_ac_v`).
     The polymer/electrolytic/film/safety tables MIX mount styles — add
     `--where "smd = 1"` for reflow-able parts (flag ≥93% coverage).
   - Rail EMI filtering → `ferrite_bead` (`z_ohm` = impedance @100MHz;
     check `dcr_ohm` for IR drop on power rails).
   - Battery charge/protect/gauge → `battery_mgmt`. LOW coverage is the
     genuine ceiling here (~57% of parts publish no attrs) — filter
     `chip_type ~ charg` / `~ protect` / `~ gauge` to route within the
     bucket, `battery_type ~ LiFePO4` for chemistry, and expect small
     result sets rather than assuming a bad recipe. NO USB-PD recipe BY
     DESIGN: PD controllers are scattered across 6+ unrelated buckets
     (CH224K in USB ICs, CH224Q in USB PD, AP33772 in 'Others') — find
     them by part name (`mpn ~`), not category.
   - Connectors: harness headers → `conn_wire_board` (filter `series ~ XH`
     / `~ GH` etc. + `pins_per_row` + `smd`; CAUTION: clone vendors
     mislabel 1.25mm families — GH vs PicoBlade don't intermate, confirm
     latch style in the datasheet). Flat-flex → `conn_ffc_fpc` (`pitch_mm`,
     `n_contacts`, `contact_type ~ bottom/top`, `height_mm`, `locking`).
     `--rank smallest` is BLIND for connectors (no package dims) — rank by
     price/stock and constrain via the mm columns instead. No board-to-
     board/mezzanine or USB recipes BY DESIGN: the user standardizes on
     Hirose BM24-24 for board-to-board and has settled USB picks.
   - eFuses / load switches / current-limit switches → `power_switch`
     (that's where integrated eFuse ICs live; the "SPD" category is ~27
     lightning-arrester-style parts, not eFuses).
   - Memory: ONE table for NOR/NAND/EEPROM/FRAM/SRAM/SDRAM — route by
     `--where "subcategory ~ NOR"` (or EEPROM/FRAM/SRAM/NAND/SDRAM).
     Sizes filter in bits: `--where "size_bits >= 128Mbit"`.

2. **Describe the table** to learn real columns, units, coverage, and
   actual value ranges:
   ```bash
   python3 scripts/trawl.py describe ldo
   ```
   Pay attention to `coverage` — filtering on a 40%-covered column
   silently drops 60% of parts (NULL never satisfies a comparison).
   Mention this tradeoff to the user if it matters.

3. **Search.** Translate the user's requirement into `--where` clauses.
   Values may carry units ("300mA", "10uH", "6V") — they're parsed with
   the column's own unit parser. Engineering judgment belongs HERE:
   add sensible margin (e.g. user needs 1.2A → filter `>= 1.2` but
   prefer headroom), pick vin/vout bounds correctly (`vin_v_max >= rail`
   AND `vin_v_min <= rail` for "works from a 5V rail").
   ```bash
   python3 scripts/trawl.py search --recipe ldo \
     --where "output_current_a >= 1.2" \
     --where "vin_v_max >= 6" --where "vin_v_min <= 5" \
     --min-stock 100 --prefer basic --qty 100 \
     --rank smallest --limit 10
   ```
   - `--rank`: `smallest` (footprint area), `cheapest` (unit price at
     `--qty`), `stock`, `balanced` (size+price+stock composite with a
     basic/preferred bonus). Default `balanced`.
   - `--prefer basic|preferred` GROUPS results: basic parts first (sorted
     by the rank), then preferred, then extended. Nothing is excluded, but
     note the ordering is per-group — with `--prefer basic --rank smallest`
     the first row is the smallest BASIC part, which may be larger than an
     extended part further down. Present both when they differ ("smallest
     basic is X at 22.8mm²; smallest overall is Y at 4.6mm² but extended").
     `--basic-only` is the hard filter.
   - `--where "col ~ text"` = case-insensitive contains (text cols only).
   - Numeric equality (`=`) is safe when the value string is identical to
     what the data uses (same parser both sides), e.g. `inductance_h = 10uH`.
     For values you're unsure of, prefer a tight range instead.

4. **Present results.** The user wants **LCSC numbers + the concrete
   reasons** — footprint size, headroom over their requirement,
   basic/extended status (basic/preferred = no feeder-loading fee),
   stock depth, unit price at their quantity, datasheet link. Ground
   every claim in the JSON `matched` values; never invent specs. Flag
   caveats honestly: `footprint_source: "family"` means the size is a
   family-typical estimate; low-coverage columns mean unfiltered parts
   may exist. Default to a top-5-to-10 list unless asked otherwise.

5. **(Optional) live-confirm finalists** before the user commits:
   ```bash
   python3 scripts/trawl.py refresh C6186 C725790
   ```
   This hits an unofficial JLCPCB endpoint. Every `describe`/`search`
   output carries `data_vintage` (when the tables were built and how fresh
   their source was). Local stock and prices can be months old, so
   **always refresh anything the user is about to put on a BOM**, and
   quote the live figure, not the local one.

## Interpreting search output

Each candidate includes `matched` (every typed attribute that parsed for
that part) and, with `--raw-attrs`, the raw LCSC attribute strings. When
a number looks surprising (e.g. dropout voltage), remember: values with
"@ condition" suffixes were parsed as the primary quantity only — check
`--raw-attrs` or the datasheet for the measurement condition.

`shielded = 1` includes semi-shielded parts (design decision: both
suppress EMI vs. unshielded). Note that LCSC has no parametric shielding
field — this flag is derived from the part **description**, so it's 1
(shielded/semi/molded), 0 (explicitly unshielded), or NULL (description
never said). A `shielded = 1` filter is reliable; a `shielded = 0` filter
returns only *explicitly* unshielded parts, not the unknowns. If the user
needs strictly fully-shielded, filter then check each candidate's
description/datasheet.

## Database maintenance (run when the user asks, or data feels stale)

```bash
python3 scripts/trawl.py update            # download CDFER in-stock DB (~1GB, resumable) + build
python3 scripts/trawl.py update --source yaqwsx   # fallback: raw upstream (needs 7z, ~10GB download)
python3 scripts/trawl.py build             # rebuild typed tables after editing recipes
python3 scripts/trawl.py build --only ldo  # rebuild one table
python3 scripts/trawl.py status            # health check: data dates, part counts, backups, verdict
python3 scripts/trawl.py import <cache.zip|cache.sqlite3>   # load a saved upstream cache (+ build)
python3 scripts/trawl.py restore derived   # swap trawl.sqlite3 back with its .prev backup
python3 scripts/trawl.py restore source    # same for the source DB
```

**Safety guards (added 2026-09-29, after upstream lost its data).**
`update` and `build` can no longer destroy good data:
- A download replaces the source DB only if it has the expected schema
  and at least 200k parts and ≥90 % of the current source's count.
  Otherwise it is parked as `*.rejected` and nothing else changes.
  `--force` overrides the size check only; a wrong schema is never accepted.
- `build` builds into a staging copy and swaps it in only if no recipe
  lost more than 20 % of its parts and the total didn't drop more than
  10 %. Otherwise the result is parked as `trawl.sqlite3.rejected` and the
  live tables are untouched. `--allow-shrink` overrides.
- Every accepted replacement keeps one rolling `*.prev` backup.

If a guard refuses, **tell the user what it reported and stop.** Never
add `--force` / `--allow-shrink` on your own; that is the user's call.

Upstream's 2026 cache format (`meta.format = source-db-v2`) is converted
to the classic schema automatically by `update` and `import`
(`scripts/convert_v2.py`), so the builder and recipes are unchanged.

**Upstream status (2026-09):** yaqwsx/jlcparts lost its full catalogue on
2026-09-14 and republished a partial set, so `update` currently gets
rejected for having too few parts. The last good upstream cache (data to
2026-09-14 09:18, 13 volumes, 681 MB) was saved from commit `75daa67` of
yaqwsx/jlcparts and verified byte-identical to it; load it with `import`. Don't rerun it expecting a different result; the
existing tables keep working. Without a usable source DB, `categories`
and `raw-attrs` fall back to the derived tables (recipe-covered
categories only), so `build` and new recipes are the only things blocked.

Teams can share the derived tables: copy one person's `trawl.sqlite3`
into `~/jlc-trawler-data/` (or `$TRAWL_DATA`) and everything except
`build` works without the 1 GB source download.

## Tuning recipes against the real database (IMPORTANT after first build)

Recipe category regexes and attribute alias lists were written against
expected LCSC naming; the real DB may differ. After any build:

1. Any `WARNING: recipe X matched ZERO categories` → run
   `categories --grep <keyword>` to find the true category names and fix
   the recipe's `category_match` regexes.
2. Any column with suspiciously low coverage in `describe` → run
   `raw-attrs "<category regex>" [--grep <term>]` to see the actual
   attribute key names in the data, then extend the column's `attrs`
   alias list.
3. Rebuild with `build --only <recipe>` and re-check `describe`.

## Adding a new category (recipe)

Recipes live in `scripts/recipes/*.json`. To add one:

1. `categories --grep <term>` → find category/subcategory names.
2. `raw-attrs "<regex>"` → see available attribute keys + example values.
3. Write the recipe (see `references/recipe-format.md` for the full
   format: parser kinds, range handling, flag columns, reserved names).
4. `build --only <newname>`, then `describe <newname>` to verify coverage.

## Things this skill deliberately does NOT do

- No LCSC-only (non-assembly) parts: the data foundation is JLC's
  assembly library. Hand-solder/THT sourcing is out of scope.
- No KiCad symbol/footprint import: the user imports files themselves
  (easyeda2kicad with the LCSC ID). Just hand over LCSC numbers +
  datasheet URLs.
- Search never uses the network. Only `update` and `refresh` do.
