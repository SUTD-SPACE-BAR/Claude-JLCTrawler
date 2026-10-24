# jlc-trawler — Handoff / Continuation Doc

Purpose of this file: hand the project to a fresh Claude Code session that
can edit files directly on Marcus's machine. It captures WHAT this is, WHERE
everything lives, HOW it works, WHAT currently works vs. is broken, and the
EXACT next actions. Read `SKILL.md` and `references/recipe-format.md` in this
same folder for the user-facing workflow and the full recipe spec — this doc
is the behind-the-scenes engineering context that isn't in those.

---

## 0. TL;DR for the next session

**UPDATED 2026-07-06 (Claude Code tuning session).** The skill folder is now
the AUTHORITATIVE tree. The `C:\Users\marcu\Downloads\jlc-trawler-full`
extract is the OLDER delivered snapshot — do not sync FROM it any more.

- The tool builds and runs on Marcus's real catalogue (last build:
  **188,563 parts, 10 recipes**, 2026-07-06). Pipeline verified end-to-end
  (units self-test, synthetic regression, real searches).
- The §3 sync is DONE (trawl.py raw-attrs schema-drift fix applied), plus a
  new fix on top: trawl.py now forces UTF-8 stdout/stderr at startup so
  Ω/±/℃ in attribute values no longer crash printing on Windows' cp1252
  console (it did, mid-raw-attrs).
- The old "one real bug" (`vgs_max_v` 0%) is RESOLVED — it was never an alias
  bug. A full scan of all 27,342 MOSFET parts found a gate-source
  max/breakdown key on exactly 2 parts: the spec is GENUINELY ABSENT from
  the CDFER snapshot. Documented in `recipes/mosfet.json`; do not chase it.
- 2026-07-06 tuning pass (all verified by rebuild; details in §6):
  * schottky `if_a` 40% → **62%** (real key is plain 'Rectified Current';
    also removed the bare 'Io'/'If' substring-trap aliases).
  * TVS: new `ipp_a` column at **50%** — peak pulse current is the surge
    spec most TVS parts actually carry (peak_power_w only reaches 25%).
  * crystal `load_cap_f` 28% → **38%** (now at parity with frequency_hz,
    i.e. at the data ceiling).
  * MLCC pinned to the 'MLCC - SMD/SMT' bucket → the 431 through-hole
    'MLCC - Leaded' parts are gone (22,890 parts, all SMD).
  * DCDC `sync_rect` CORRECTNESS fix: dominant real key is 'Synchronous
    Rectification' with values 'Yes'/'No'; old truthy ['synchron'] recorded
    every 'Yes' as 0. Now ternary — rebuilt spread: 1218 yes / 898 no /
    1951 NULL(unknown).
  * LED `color`: 'Illumination Color' added before the generic 'Color'
    substring so lens junk ('Colorless Transparent Lens') can't win.
- Remaining low columns are believed GENUINE data gaps (each verified
  against raw-attrs samples of the real DB): ldo iq (dominant key 'standby
  current' is mostly literal '-')/dropout, dcdc vout/iq, mosfet rds_on/
  vgs_th/power/qg (coverage ≈ key availability), inductor srf (ZERO
  frequency keys exist — noted in recipe)/sat, LED photometrics, crystal
  esr (many literal '-')/tolerance/stability, TVS peak_power/direction/
  n_channels.
- LATER SAME DAY: two new recipes added after a real session hit the gap —
  `cap_polymer` (polymer aluminum + solid polymer, 2,101 parts) and
  `cap_tantalum` (2,139 parts). Enablers that came with them:
  * packages.py now parses EIA case codes ('CASE-D-7343-31(mm)' → 7.3×4.3)
    and cylindrical cans ('SMD,D8xL12mm' → D×D bounding box, height ignored).
  * builder.py from_description flags now scavenge description+PACKAGE text
    (mount style lives in the package string), and recipe JSON is read as
    explicit UTF-8 (cap_polymer contains the Chinese THT marker '插件';
    Windows' cp1252 default crashed the build).
  * cap_polymer has an `smd` ternary flag (99% coverage) because that
    bucket is MAJORITY through-hole (1,540/2,101) — filter smd=1.
- Everything deterministic is plain Python run via the CLI. Claude's only
  runtime job is intent→query and results→prose. No model in the data path.

---

## 1. What this project is

A local-first, offline parametric search over the JLCPCB/LCSC **assembly**
(SMT) parts catalogue. Marcus is an avionics/PCB designer; he wants queries
like "smallest LDO doing 1A at 3.3V", "cheapest shielded inductor rated 2A",
"MOSFET for this load switch" and to get back **an LCSC part number plus the
concrete reasons** (footprint size, headroom, basic/extended, stock, unit
price, datasheet link). He reviews the top 5–10 datasheets and imports the
KiCad files himself, so the tool does NOT do footprint/symbol import.

Hard constraints (all confirmed by Marcus, do not relitigate without asking):
- **Assembly parts only** (JLC-anchored data). No pure-LCSC / through-hole
  hand-solder sourcing.
- **Deterministic code does everything** that doesn't need judgment: DB
  download, JSON→typed-table build, unit parsing, footprint sizing, all
  filtering/sorting/ranking, price math. Mirrors his KiCad schematic-parser
  skill philosophy. Claude only (a) turns intent into concrete `--where`
  predicates after introspecting, and (b) turns result JSON into prose.
- **Search never touches the network.** Only `update` (download) and
  `refresh` (optional live price/stock for a shortlist) do.
- **NOT the official JLCPCB API** — Marcus is an individual, not a company.
- Output = LCSC number + grounded reasons. Datasheet URL is a DB column.

---

## 2. Where everything lives (Marcus's machine, Windows)

**Skill folder** (the code; this is also where this doc lives):
```
C:\Users\marcu\AppData\Roaming\Claude\local-agent-mode-sessions\skills-plugin\30a5911a-16f9-4f0c-99b0-1a7e6d021242\80003dc2-926e-48b9-82b4-1d039a62cb0a\skills\jlc-trawler
```
NOTE: that path is session-scoped and ugly. If Claude Code can relocate the
project to a stable path (e.g. `C:\Users\marcu\dev\jlc-trawler`), do it — it
'll make life easier. The code has no hard dependency on its own location
except that `scripts/` imports are relative and `recipes/` + `package_sizes.json`
are resolved relative to the script files (via `__file__`), so moving the whole
folder is safe.

**Data directory** (the databases; NOT in the skill folder):
```
C:\Users\marcu\jlc-trawler-data\
    jlcpcb-components.sqlite3   ~1.5 GB  downloaded source DB (read-only to us)
    trawl.sqlite3              derived typed tables (rebuilt by `build`)
```
Resolution order in code: `--dir` flag > `%TRAWL_DATA%` env var >
`~/jlc-trawler-data`. The 1.5 GB source is already downloaded — do NOT
re-download unless refreshing the catalogue.

**Python:** invoked as `python3` in Marcus's shell. Standard library only —
no pip installs required for the core tool (sqlite3, json, re, urllib, etc.
are all stdlib). `curl` (bundled with Windows 10+) is used by `update`.
`7z` is only needed for the `--source yaqwsx` fallback, not the default.

---

## 3. ~~FIRST ACTION — sync the code baseline~~ DONE (2026-07-06)

Completed. `scripts/trawl.py` was confirmed the only real divergence (every
other file differed by line endings only), was overwritten from the baseline,
and then received one further fix here: stdout/stderr forced to UTF-8 at
startup (Windows cp1252 console crashed on 'Ω' mid-print in `raw-attrs`).
**The skill folder now leads the Downloads baseline — never sync from the
zip extract again.** Sanity checks still worth running after any edit:
```
python3 scripts\units.py          # expect: ALL PASS (35 cases)
python3 scripts\trawl.py raw-attrs "MOSFET"   # expect: a list, no traceback
```

---

## 4. Architecture (how the pipeline works)

```
 CDFER SQLite (source, read-only)
   components.extra  = JSON blob of LCSC parametric attributes (per part)
   components.jlc_extra = JSON blob of JLC overrides  [MAY BE ABSENT — see §5]
   categories, manufacturers, v_components (view)
        │
        │  builder.build()  — streams v_components, per recipe:
        │     1. match categories by regex  → category_ids
        │     2. merge_attributes(extra, jlc_extra)  [jlcparts-identical rule]
        │     3. for each recipe column: find attr by alias list, parse via
        │        units.py into SI base units (or flag 0/1, or text)
        │     4. resolve package name → physical size via packages.py
        │     5. precompute unit price at qty 1/10/100/1000 from price tiers
        │     6. bulk-insert into t_<recipe>, index numeric columns
        ▼
 trawl.sqlite3 (derived)
   meta(key,value)   — stores the recipe set as JSON (self-describing) +
                       source_db path + built_at
   t_ldo, t_mosfet, …  — one typed, indexed table per recipe
        │
        │  trawl.py search  — validates --where against known columns,
        │     builds safe SQL, ranks, recomputes exact price at --qty,
        │     emits JSON candidates with a `matched` block of grounded specs
        ▼
   JSON → Claude writes the human "reasons"
```

**The files (`scripts/`):**

- `units.py` (375 lines) — the deterministic parsing core. `parse_quantity(raw,
  kind)` turns messy strings ("1.2A", "10uH", "1.8V~5.5V", "600mV @ 1A",
  "±10ppm", "4R7", "-40℃~+85℃") into `(min, max)` in SI base units. `parse_flag(
  raw, truthy, false_patterns=None)` does categorical → 1/0/None (ternary when
  false_patterns given). Has an inline self-test: `python3 units.py` → "ALL
  PASS (35 cases)". EDIT CAREFULLY and re-run the self-test after any change.

- `packages.py` (247 lines) — `resolve_package_size(name)` → {l_mm, w_mm,
  area_mm2, source}. Priority: static table (`package_sizes.json`) → parsed
  (imperial chip codes like 0402, or dims embedded in the name like
  "SMD,4x4x3mm") → family typical (SOT-23-*, SOIC-*) → None. `source` tag
  records which tier fired; "family" = approximate. Also has an
  `import-kicad <footprints_dir>` subcommand that harvests real courtyard
  bounding boxes from KiCad `.kicad_mod` libraries into the static table.

- `package_sizes.json` — ~75 common packages → [L, W] mm. Hand-curated;
  courtyard import only ADDS new names, never overwrites these.

- `builder.py` (555 lines) — the build engine. Key functions:
  `load_recipes()` (validates + strips inline-comment strings from columns),
  `merge_attributes()` (LCSC attrs overlaid by JLC attrs, skipping ""/"-" —
  identical to upstream jlcparts `_mergeAttributes`), `_probe_source_columns()`
  + `_build_source_select()` (the schema-drift adapter, see §5), `build()`
  (the main loop). Streams the source DB so memory is O(1) in part count.

- `trawl.py` (599 lines) — the CLI. Subcommands: `update`, `build`,
  `categories`, `raw-attrs`, `describe`, `search`, `refresh`. See SKILL.md
  for the user-facing description of each. Data-dir resolution and source/
  derived DB opening helpers live at the top.

- `make_test_db.py` (186 lines) — builds a small synthetic source DB with the
  EXACT jlcparts schema and deliberately messy data (corrupt JSON blob,
  jlc_extra overlays, R-notation, "@ condition" strings, ppm crystals,
  description-only shielding, hyphenated MOSFET keys). NOT shipped as part of
  the skill's function — it's the test harness. Used like:
  `python3 make_test_db.py /tmp/x/jlcpcb-components.sqlite3` then
  `python3 trawl.py --dir /tmp/x build`.

**`recipes/*.json`** — one declarative file per component category. Full spec
in `references/recipe-format.md`. A recipe maps LCSC category regexes +
attribute-name alias lists → typed columns. Adding a category = new recipe +
`build`. The recipe format supports: `parse` kinds (voltage/current/…/percent/
text/number), `range` (min/max/both → both makes `_min`+`_max` columns),
`flag` + optional `flag_false` (ternary) + optional `from_description`
(scavenge from description text), and inline string "comments" in the columns
array.

---

## 5. The `jlc_extra` schema-drift saga (important context)

The code was originally written against the exact upstream **yaqwsx/jlcparts**
schema, which has a `jlc_extra` column (JLC's attribute overrides). But the
**CDFER** snapshot Marcus actually downloads is a processed subset and its
`components` table / `v_components` view **do not have `jlc_extra`**. This
caused three separate crashes, fixed in three places:

1. `builder.build()` — now probes the source's real columns
   (`_probe_source_columns`) and substitutes safe defaults for any missing
   OPTIONAL column (`jlc_extra`→`'{}'`, `manufacturer`→NULL, `basic`/
   `preferred`/`joints`→0). Missing ESSENTIAL columns (lcsc, price, extra…)
   fail loudly with a clear message. Prints a one-line NOTE when it defaults.
2. `cmd_raw_attrs()` in `trawl.py` — had its OWN separate query selecting
   `extra, jlc_extra`; now probes `components` columns and only selects
   `jlc_extra` if present. **THIS is the fix Marcus hasn't synced yet.**
3. (No other call site touches jlc_extra — `search`/`describe` read the
   derived DB; `categories` doesn't select it; `refresh` is network.)

Consequence of the missing column: attributes that lived ONLY in JLC's
overlay are unavailable, so a few columns show slightly lower coverage than
they would on the raw yaqwsx DB. This is acceptable. If we ever want max
coverage, `update --source yaqwsx` pulls the raw multi-volume zip (needs 7z)
which HAS jlc_extra — the code already supports repointing there.

---

## 6. Current state (post-tuning, 2026-07-06)

Last real build: **188,563 parts across 10 recipes.** Coverage per column
(actual build output — ground truth). Every low column below has been
checked against a `raw-attrs` sample; "genuine" means the data simply isn't
there, not that an alias is missing.

| recipe | healthy columns | remaining low columns — all believed genuine |
|---|---|---|
| **mlcc** (22,890, SMD-only now) | cap 82%, V 80%, tol 69%, dielectric 81% | — |
| **resistor_chip** (79,546) | R 88%, tol 87%, P 86%, tempco 81%, V 67% | — |
| **ldo** (7,402) | Iout 62%, Vout 61%, Vin 61%, type 63%, n_out 60% | dropout 42% (aliases match; stock-biased data), iq 32% (dominant key 'standby current' is mostly literal '-') |
| **dcdc** (4,067) | topology 60%, Iout 56%, Vin 56%, fsw 54%, sync 52% **with fixed semantics** | Vout 42% (adjustable parts leave it blank), iq 23% |
| **diode_schottky** (8,851) | Vr 63%, **If 62% (was 40)**, Vf 61%, Ir 54% | — |
| **inductor_power** (21,557) | L 64%, tol 63%, DCR 55%, Irated 52% | shielded 35% (description-sourced ceiling), sat 34% (cheap parts omit Isat), srf 0% (ZERO freq keys in data — see recipe comment) |
| **crystal** (5,986) | — | freq 38%, **load_cap 38% (was 28)**, tol 37%, stab 30%, esr 18% (many literal '-'). ~25% of sampled parts carry NO parametric attrs at all (jlcparts-style record blobs instead) — that's the ceiling. |
| **led** (4,362) | color 62% (now prefers real emitted color over lens color) | vf 31%, if 47%, wavelength 36%, lum 24% (photometrics genuinely sparse; multi-color 'R:2V;G:2.6V' values unparseable by design) |
| **mosfet** (18,365) | Id 59%, vds 49%, channel 48% | rds_on 43%, vgs_th 44%, power 43%, qg 35% — all ≈ key availability in data; **vgs_max 0% is GENUINE (2 parts in 27,342 have the key)** |
| **tvs** (15,537) | standoff 54%, breakdown 52%, clamp 52%, **ipp_a 50% (NEW column)**, **bidirectional 51% (ternary flag, 07-07 — replaced the junk text 'direction' column whose values were channel counts/'TVS')** | peak_power 25% (≈ availability), n_channels 18% ('Number of Channels' values are words) |
| **cap_polymer** (2,101, NEW) | smd 99% (ternary; bucket is majority THT — filter smd=1) | cap/V 51%, tol 43%, lifetime 38%, esr/ripple 26% — the jlcparts-style record blobs (no attributes dict) cap this; ESR-known parts are the useful ones anyway |
| **cap_tantalum** (2,139, NEW) | cap 90%, V 90%, tol 90%, **esr 83%** | ripple 3% (values are literal '-' — genuine). ESR value discriminates polymer-Ta (tens of mΩ) from MnO2 (ohms); LCSC doesn't split them. |
| **supervisor_reset** (2,574, NEW 07-07) | output_type/active_level 51%, n_monitored 49%, threshold 46%, timeout 37% | vin 21%, watchdog 13% (attr sparse; watchdog kept as TEXT so 'None' vs 'Windowed' is visible) |
| **vref** (2,111, NEW 07-07) | output_type 72%, vout 71%, iout 71%, tol 69%, ref_type 69%, tempco 56% | vin_max 34%, cathode_current_min 11% (shunt-only spec — genuine) |
| **power_switch** (1,524, NEW 07-07) | vin 54%, current_limit 52%, n_outputs 51%, ron 49% ('Resistor On-State' is the real key name) | control_logic/switch_type 8% (only jlcparts-style records carry them). eFuses live HERE — the SPD bucket is 27 arrester-style parts. |
| **memory** (2,351, NEW 07-07) | size_bits 42% (new `memsize` parser kind: '128Mbit' → bits, 1024-based), vcc 42%, interface 39% | clock 12%, write_cycle 6%, endurance 3% (attrs mostly '-' — genuine). Route types via `subcategory ~ NOR/EEPROM/FRAM/SRAM/NAND/SDRAM` (common column, filterable). |
| **diode_zener** (11,035, NEW 07-07) | vz 63%, power 61%, zzt 59%, ir 59%, config 51%, vz_window 49% | — (largest formerly-uncovered category; LL-34/LL-41 MELF added to package_sizes.json) |
| **diode_rectifier** (10,111, NEW 07-07) | vr 62%, vf 61%, if 60%, ir 59% | trr 32% overall (bridges/GP don't spec it — among fast/switching it's ~60%; filter `trr_s <= 75ns` works), power 1% (genuine). Covers GP + switching + fast recovery + SBR + bridges; route via `subcategory ~`. |
| **cap_electrolytic** (12,834, NEW 07-07) | cap 77%, V 75%, tol 75%, lifetime 55%, smd 99% | ripple 33% (@120Hz convention!), esr 13% (liquid electrolytics don't publish it — use cap_polymer when ESR matters) |
| **cap_film** (3,952, NEW 07-07) | cap/tol 63%, V 62%, smd 93% (mostly THT 'Plugin,P=x' — smd=1 is the exception) | dielectric 20% (sparse attr; the CBB bucket is polypropylene by definition) |
| **cap_safety** (2,407, NEW 07-07) | cap/tol 67%, subclass 65% (X2/Y1/Y2 — THE filter), voltage_ac 63%, smd 96% | certifications 25%, voltage_dc 0% (~genuinely absent) |
| **ferrite_bead** (3,301, NEW 07-07) | z@100MHz 78%, n_circuits 76%, tol 69%, dcr 68%, Irated 66% | — (healthiest new recipe; z_ohm condition is @100MHz by near-universal convention) |
| cap_polymer update 07-07: now also matches 'Solid Capacitors' (+1,102 parts → 3,203 total). | | |
| **conn_wire_board** (22,745, NEW 07-07) | pitch 50%, n_rows 50%, pins_per_row/structure 48%, series 39% ('XH'/'GH' etc — see mislabeling CAUTION in recipe), smd 74% | current 31%, gender 20%. Housings bucket deliberately excluded (^Wire To Board anchor). No footprint dims → don't rank smallest. |
| **conn_ffc_fpc** (6,244, NEW 07-07) | smd 97%, pitch/n_contacts 53%, locking 52%, contact_type 51%, height 50%, thickness 46% | — (best-parameterized connector bucket; cable buckets excluded) |
| NO board-to-board/mezzanine or USB recipes BY DESIGN — Marcus standardizes on Hirose BM24-24 and settled USB picks (see memory: marcus-part-prefs). | | |
| **battery_mgmt** (1,624, NEW 07-07) | battery_type 29%, n_cells 28% | chip_type 12%, charge_current 16%, vin 19%, charge_voltage 3%, balance/interface ~0% — ALL GENUINE: 57% of parts publish zero attrs (TP4056-clone world). Sparse balance_current non-NULL ≈ AFE signal. |
| NO USB-PD recipe BY DESIGN — PD controllers are scattered across 6+ buckets (CH224K→USB ICs, CH224Q→USB PD, CH224D→empty string, AP33772→'Others'); no category regex can collect them. Right fix if ever needed: wire up the source DB's components_fts free-text search (Phase-2 item, §9). | | |

### 6a. RESOLVED: MOSFET `vgs_max_v` was never an alias bug

A full scan of ALL 27,342 MOSFET parts' merged attributes (not just the
500-part raw-attrs sample) found exactly ONE gate-source max/breakdown key:
`'Gate-Source Breakdown Voltage (V(BR)GSS)'` — on **2 parts** (and the
recipe's alias list already matches it case-insensitively). The CDFER
snapshot genuinely does not carry this spec. The column is kept (it would
populate if the source were repointed to raw yaqwsx) and the ground truth
is documented in `recipes/mosfet.json`. The other MOSFET columns were
checked in the same pass: coverage ≈ key availability, i.e. at ceiling.

### 6b. Distinguishing "fixable" from "genuine" low coverage

For any low column, run `raw-attrs "<category>"` and look:
- If a well-populated attribute key exists that the recipe's `attrs` list
  misses → **fixable**, add the alias.
- If no such key appears in the sample (the spec just isn't there for most
  parts) → **genuine**, leave it. Likely-genuine: inductor `srf_hz`, inductor
  `sat_current_a`, LED photometric columns, TVS `n_channels`.

### 6c. Category-match cleanups — DONE

- `mlcc` is now pinned to the exact 'MLCC - SMD/SMT' bucket; the 431
  through-hole 'MLCC - Leaded' parts are excluded (2026-07-06).
- `crystal` buckets were inspected: all six matched buckets are legitimate
  crystal categories. The low coverage is attribute sparsity (see table),
  not category contamination.
Use `python3 scripts\trawl.py categories --grep <term>` to see exact names.

### 6d. Substring-trap lore (learned the hard way, keep honoring it)

`_find_attr` falls back to substring matching, so SHORT aliases are
dangerous. Documented instances, all now fixed/annotated in the recipes:
- bare `'Vgs'` would match the threshold key `'...(Vgs(th)...'` (mosfet).
- bare `'Io'` matched `'D-io-de Configuration'` and returned text as a
  current (schottky — this was why if_a sat at 40%).
- bare `'If'` matched `'Rect-if-ied Current'` and `'(Vf@If)'` (schottky).
- generic `'Color'` matched `'Lens Color'` before the real emitted color
  (led) — fixed by listing `'Illumination Color'` explicitly first.
Rule of thumb: never add an alias shorter than ~4 chars or one that is a
substring of a DIFFERENT spec's key; put exact real keys first in the list.

---

## 7. How to actually do the tuning loop (for the next session)

The whole loop is deterministic and fast (search is offline; only the source
DB is large). Standard cycle:

1. `categories --grep <keyword>` → confirm real category/subcategory names,
   fix any `category_match` regex that matched zero or wrong buckets.
2. `raw-attrs "<category regex>" [--grep <term>]` → see real attribute key
   names + example values. This drives alias-list edits.
3. Edit the relevant `recipes/<name>.json` (aliases, flags, category match).
4. `build --only <name>` → rebuild just that table (fast).
5. `describe <name>` → verify coverage/ranges look right.
6. Spot-check with a real `search` and eyeball the `matched` block.

After bigger changes, run the synthetic regression to catch parser breaks:
```
python3 scripts\units.py                       # 35-case parser self-test
python3 scripts\make_test_db.py C:\Temp\ht\jlcpcb-components.sqlite3
python3 scripts\trawl.py --dir C:\Temp\ht build
python3 scripts\trawl.py --dir C:\Temp\ht search --recipe mosfet --where "vgs_max_v >= 12" --limit 3
```

---

## 8. Design decisions log (so context isn't re-litigated)

- **Data source = CDFER in-stock SQLite** (`https://cdfer.github.io/jlcpcb-parts-database/jlcpcb-components.sqlite3`), ~1 GB, stock≥5, rebuilt daily upstream. Code can repoint to raw yaqwsx via `update --source yaqwsx`.
- **Hybrid schema:** source DB is the universal store (nothing dropped; full raw attrs kept per-part in `attrs_json`). Typed per-category tables are DERIVED by declarative recipes. Adding capability = add a recipe, never touch the scraper/parser.
- **Package sizes:** static curated table + name heuristics + optional KiCad courtyard import. easyeda2kicad geometry fallback for exotic packages = **Phase 2, deferred** (Marcus imports files himself, so query-time geometry isn't needed yet).
- **Output:** LCSC number + grounded `matched` specs + datasheet URL. Claude writes reasons. Top 5–10, user picks.
- **`--prefer basic|preferred`** groups (basic first) but never excludes; `--basic-only` is the hard filter. `--rank` = smallest|cheapest|stock|balanced (balanced = normalized size+price+log-stock composite with a basic/preferred bonus, computed in Python).
- **shielded flag** includes semi-shielded (=1). Sourced from DESCRIPTION text (LCSC has no shielding attribute). Ternary: 1 shielded/semi, 0 explicitly unshielded, NULL unknown. So `shielded=1` filter is reliable; `shielded=0` returns only explicit unshielded, not unknowns.
- **ppm:** the `percent` parser kind handles both `%` and `ppm` (÷1e6), both stored as dimensionless fraction — that's why crystal tol/stability use `"parse":"percent"`.
- **Initial recipe set = 10 most common categories** (ldo, dcdc, inductor_power, mlcc, resistor_chip, mosfet, diode_schottky, tvs, led, crystal). More later as needed.
- Marcus's coding prefs: heavy explanatory comments; edit local files directly; NO git worktrees/branches/commits.

---

## 9. Phase 2 / future (agreed deferred)

- easyeda2kicad build-time footprint-geometry fallback for exotic packages
  (cached into the DB so search stays offline).
- More category recipes beyond the initial 10.
- Optional: relocate project out of the session-scoped AppData path.
- Optional: free-text search via the source DB's `components_fts` FTS5 table
  (CDFER adds it; not yet wired into the CLI).

## 10. 2026-09-29: upstream wipe and the safety guards

On 2026-09-14 a yaqwsx/jlcparts CI run failed to fetch its own cache,
bootstrapped from a checkpoint and deployed ~5 % of the catalogue over the
full one, in a new schema (`meta.format = source-db-v2`, tables
`jlc_components` / `lcsc_components`, no `v_components`). CDFER's file
followed. Our `update` then replaced the good 1.6 GB July source with the
33 MB partial file without checking; only the schema change stopped the
build from rebuilding every table from it. That source is gone. The
derived `trawl.sqlite3` (built 2026-08-23 from source data whose newest
update is 2026-04-01) survived and is the team's copy.

New `scripts/safety.py`: `validate_source` (schema + ≥200k parts + ≥90 %
of current), `build_safely` (staging copy, per-recipe/total shrink check,
atomic swap), `swap_in` (one rolling `.prev`). `trawl.py` gained
`restore`, `--force` / `--allow-shrink`, a `data_vintage` block in
describe/search output (meta keys `built_at`, `source_newest_update`,
`source_downloaded`), and derived-table fallbacks for `categories` /
`raw-attrs`. Tested against `make_test_db.py` output: refused shrink
leaves the live file byte-identical, `restore` round-trips, the
source-db-v2 file is rejected even with `--force`.

Supporting source-db-v2 would need `_probe_source_columns` and the
SELECTs to read `jlc_components` (category/subcategory are inline text,
attributes in `attributes` JSON, price as "1-9:0.1,10-:0.08" text). Not
worth doing until upstream publishes a complete set in that format.

Later the same day: the last good upstream publish was found still reachable
at the unreferenced gh-pages commit `75daa67` (raw.githubusercontent.com),
downloaded, and all 13 volumes matched the commit's git blob SHAs. It is
source-db-v2 (7,161,863 JLC rows, 633,292 in stock, fetched 2026-05-23 to
2026-09-14). New `convert_v2.py` maps it to the classic schema; `update`
and the new `import` command call it automatically. Built from it, the
tables pass the shrink guard: 322,795 parts (+9.5 %), all 27 recipes
populated, and of 188 spec columns 159 gained >10 pts coverage and none
lost any (v2 carries full LCSC attributes). JLC renamed most categories
(e.g. "Transistors/Thyristors / MOSFETs"); the recipe regexes still match.
