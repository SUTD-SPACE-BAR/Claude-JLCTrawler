# Claude-JLCTrawler
Claude skill for automated searching of electronics parts from JLCPCB Parts Library

Ask Claude Code for a part in plain English. Claude searches a local copy of
JLCPCB's assembly catalogue and comes back with LCSC part numbers and the
reasons for each pick.

Current skill version: **`2026-09-29 (safety guards, v2 import)`**.

## What to expect

Ask in any Claude Code session, from any folder (your PCB project is the
natural place). For example:

- "find the highest capacitance polymer or electrolytic capacitor, 35V at least, which is 8x8mm or smaller. Then sort by price"
- "smallest LDO that does 1A at 3.3V"
- "cheapest shielded inductor rated 2A"
- "basic-library MOSFET for a 12V load switch"
- "polyfuse for 5V USB input, about 500mA load"

For each request Claude:

1. picks the right table out of 27 categories: regulators, passives, diodes,
   MOSFETs, crystals, memory, connectors, polyfuses, battery ICs and more;
2. checks how many parts list the specs you care about, and tells you when
   filtering on a spec would hide parts that don't list it;
3. searches and ranks by size, price, stock, or a balance of the three;
4. checks live JLCPCB stock and price for the finalists.

For each pick you get the LCSC number, MPN, footprint size, headroom over your
requirement, basic or extended (extended parts carry a feeder-loading fee),
stock, price at your quantity and the datasheet link. Claude also states the
assumptions it made.

Need a category that isn't covered? Ask Claude to "add a recipe for
<category>" and it will extend the skill.

### Example: high-capacitance 35 V capacitor

From a real session on 2026-10-15. The prompt:

> find the highest capacitance polymer or electrolytic capacitor, 35V at least, which is 8x8mm or smaller
>
> Then sort by price

What Claude did:

1. Checked the parts data was healthy and noted how old it was.
2. Looked at the polymer and electrolytic tables. Voltage is listed for 98% of
   parts, but ESR for only 52% of polymers and 18% of electrolytics, so it
   didn't filter on ESR.
3. Searched both tables for 35 V and up, kept SMD parts no bigger than 8 mm on
   either side, and sorted by capacitance.
4. Checked live stock and price for the finalists.

Claude's answer (excerpt):

> The highest capacitance at 35 V or more in an 8×8 mm footprint is **470 µF / 35 V**, and it's an aluminium electrolytic, not a polymer.
>
> | # | LCSC | MPN | Maker | Price @100 | Live stock |
> |---|---|---|---|---|---|
> | 1 | C48935389 | RV35V470M8X10 | jieerrui | $0.128 | 151,991 |
> | 2 | C22383515 | GVM1V477M0810CN | ACMECON | $0.132 | 498 |
> | 3 | C7471889 | 189RV0027 | KNSCHA | $0.149 | 4,642 |
> | 4 | C48970923 | RYGVM35V470UF8*10 | Honor Elec | $0.184 | 230 |
> | 5 | C18208552 | V3MCD1001V471MVTM | Ymin | $0.347 | 95 |
> | 6 | C2164331 | EMZS350ARA471MHA0G | Nippon Chemi Con | $0.351 | 2,849 |
>
> All six are extended parts, so each carries JLCPCB's feeder-loading fee.
>
> **Best polymer: 330 µF / 35 V.** C22395182, SPT1VM331F12TRVZC (Yunxing), $0.204 at qty 100 with 852 in stock. It is Ø8 × 12 mm tall, so the footprint is 8×8 mm but it is taller than the electrolytics.

It also listed its assumptions: SMD parts only, no height limit, and "8×8 mm"
read as a can diameter of 8 mm or less.

### Good to know

- **The data is a snapshot.** It runs to 2026-09-14, and every answer says how
  old it is. Stock and prices are as old as the snapshot, so Claude checks live
  figures before a part goes on a BOM.
- **Some specs are missing.** Not every part lists every spec. Connector
  current ratings are the worst: LCSC mixes per-contact and whole-connector
  figures. Claude flags these, but check the datasheet before you commit.
- **SMT assembly parts only.** No LCSC-only or hand-solder sourcing.
- **No KiCad files.** You get LCSC numbers and datasheet links. Import
  symbols and footprints yourself, for example with easyeda2kicad.
- **Claude Code only.** Regular Claude chat can't reach the database on your
  machine.
- **Searches are offline.** Only setting up or updating the data, and live
  stock and price checks, use the network.

## Setup

You need Claude Code, Python 3 (standard library only, nothing to pip
install), curl and about 2 GB of free disk. There are no accounts or API keys.

1. **Install the skill.** From this repo's folder, link the skill into Claude
   Code's skills folder, then start a new Claude Code session:
   ```bash
   ln -s "$PWD/jlc-trawler" ~/.claude/skills/jlc-trawler
   ```
   On Windows, copy the `jlc-trawler` folder to
   `%USERPROFILE%\.claude\skills\jlc-trawler` instead.
2. **Get the parts data.** Ask Claude to "set up the JLC parts database". It
   explains the options below and asks before downloading anything. The data
   goes in `~/jlc-trawler-data/`.

### Where the parts data comes from

The data is the community [jlcparts](https://github.com/yaqwsx/jlcparts)
dataset: JLCPCB's assembly parts list with LCSC's specs, which the skill turns
into search tables on your machine. Normally the skill downloads it from
upstream (about 1 GB).

On 2026-09-14 upstream lost about 95% of its catalogue. As of 2026-10-24 it
hasn't recovered: [yaqwsx/jlcparts#159](https://github.com/yaqwsx/jlcparts/issues/159)
and [yaqwsx/jlcparts#164](https://github.com/yaqwsx/jlcparts/issues/164) are
open and the fix [yaqwsx/jlcparts#165](https://github.com/yaqwsx/jlcparts/pull/165)
is unmerged. The skill refuses a partial download rather than use it. On
2026-10-24 upstream had 76% of the parts back, short of the 90% the skill
requires.

Until upstream recovers, the data is attached to this repo's
[`data-2026-09-14` release](https://github.com/SUTD-SPACE-BAR/Claude-JLCTrawler/releases/tag/data-2026-09-14),
and Claude downloads it from there. You choose one of two options:

| Option | Size | What works |
|---|---|---|
| Search tables only (`trawl.sqlite3`) | 472 MB | Every search |
| Full data: upstream's last good cache, then imported | 681 MB download, needs 7-Zip, about 7.5 GB free while importing | Every search, plus rebuilding tables and adding new categories |

Both hold data up to 2026-09-14.

## For maintainers

| Path | What it is |
|---|---|
| `jlc-trawler/` | The skill itself, and the folder that gets installed. `SKILL.md` is what Claude follows. `HANDOFF.md` has the design notes and recipe history. `references/prewipe-cache-manifest.txt` has the hashes of upstream's last good cache. |
| `repair-2026-09-29/` | The repair instructions sent to the team after the upstream data loss, and the checksum of the shared `trawl.sqlite3`. |

The parts data is never committed: it lives outside the repo and is hundreds of MB.

**Keep the safety guards.** The pre-2026-09-29 skill overwrote good data with
upstream's partial file. Every future version must keep these:

- downloads and table rebuilds are checked before they replace anything;
- every replacement keeps a backup that can be restored;
- upstream's newer `source-db-v2` format is converted on import;
- every result shows the data's date.

Before committing a change, ask Claude to run the self-tests, the health check
and a test build against the synthetic database. HANDOFF.md covers these.
Bump `SKILL_VERSION` in `jlc-trawler/scripts/trawl.py` when behaviour changes.
