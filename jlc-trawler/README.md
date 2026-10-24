# jlc-trawler — JLCPCB parts search skill for Claude Code

Ask Claude things like *"smallest LDO doing 1A at 3.3V"*, *"cheapest
shielded inductor rated 2A"*, or *"basic-library MOSFET for a 12V load
switch"* and get back LCSC part numbers with concrete, data-grounded
reasons: footprint size, spec headroom, basic/extended status, stock,
unit price at your quantity, and the datasheet link.

Under the hood it's a local SQLite database of JLCPCB's SMT **assembly**
catalogue (~190k in-stock parts) with typed, indexed per-category tables.
Searches run entirely offline on your machine; Claude only translates
your intent into query filters and the results back into prose.

## Requirements

- **Claude Code** (terminal CLI, desktop app "Code" tab, or IDE
  extension) or the desktop app's **Cowork** mode. It will NOT work in
  regular Claude Chat — chat runs in a cloud sandbox with no access to
  your machine, and the skill needs both.
- **Python 3** on your PATH (standard library only — no pip installs).
- **curl** (bundled with Windows 10+ and macOS).
- ~2 GB free disk for the databases.

## Install

1. Extract this folder to your personal Claude skills directory so that
   `SKILL.md` ends up at:
   - Windows: `C:\Users\<you>\.claude\skills\jlc-trawler\SKILL.md`
   - macOS/Linux: `~/.claude/skills/jlc-trawler/SKILL.md`
2. Get the parts data into `~/jlc-trawler-data/` (override with the
   `TRAWL_DATA` env var). Pick one:
   - **Fastest:** copy the shared `trawl.sqlite3` (from the team repair
     zip) into that folder. All searches work straight away.
   - **Full data (needed for `build` and new recipes):** get the saved
     upstream cache (`cache.zip` + `cache.z01`–`z12`, 681 MB, needs 7-Zip)
     and run
     ```
     python3 ~/.claude/skills/jlc-trawler/scripts/trawl.py import /path/to/cache.zip
     ```
   - **From upstream:** `trawl.py update`. Since 2026-09-14 upstream only
     has a partial catalogue, so this gets refused (safely) until it
     recovers.
3. Check it: `python3 ~/.claude/skills/jlc-trawler/scripts/trawl.py status`
   should end with `"verdict": ["OK"]`.

That's it. No configuration, no accounts, no API keys.

## Usage

Just ask parts questions in any Claude Code session — the working
directory doesn't matter (your PCB project folder is the natural
choice). Claude picks the right category table, introspects the real
columns and coverage, filters, ranks (smallest / cheapest / stock /
balanced), and cites every spec from the database.

Covered categories so far: LDOs, DC-DC converters, power inductors,
ferrite beads, MLCCs, polymer capacitors (low-ESR bulk), aluminum
electrolytics, film/CBB capacitors, safety (X/Y) capacitors, tantalum
capacitors, chip resistors, MOSFETs, Schottky diodes, zener diodes,
rectifier/switching/fast-recovery diodes (incl. bridges), TVS/ESD,
LEDs, crystals, voltage supervisors/reset ICs, voltage references,
load switches/eFuses, memory (NOR/NAND flash, EEPROM, FRAM, SRAM,
SDRAM), wire-to-board connectors (JST-style harness headers), FFC/FPC
connectors, and battery management ICs (chargers/protection/gauges). Asking for a category that isn't covered? Say "add
a recipe for <category>" — Claude can extend it (see
`references/recipe-format.md`).

Keeping the data fresh: rerun `update` now and then. It checks each
download and refuses anything in the wrong format or much smaller than
what you have, and `build` refuses to swap in tables that lost parts,
so a bad upstream day can't wipe your data (`restore` undoes any
accepted change). Every search shows the data's date. For a shortlist
you're about to order, ask Claude to `refresh` the candidates for live
stock/price.

No source database? If upstream is down (as since 2026-09-14), someone
who has one can share their `trawl.sqlite3`: drop it into
`~/jlc-trawler-data/` and search works without the download.

## Scope (deliberate)

- SMT assembly parts only — no through-hole / hand-solder sourcing.
- No KiCad symbol/footprint import; you get LCSC numbers + datasheet
  URLs and import files yourself (e.g. easyeda2kicad).
- Search never touches the network; only `update` and `refresh` do.

## For maintainers

`SKILL.md` is the workflow Claude follows at runtime. `HANDOFF.md` is
the engineering deep-dive (architecture, recipe-tuning history, known
data-coverage ceilings) — read it before changing `scripts/` or
`recipes/`.
