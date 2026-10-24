# jlc-trawler repair kit (2026-09-29)

**Humans:** unzip this folder, open Claude Code, and say:

> Read `REPAIR-INSTRUCTIONS.md` in `<path to this folder>` and repair my
> jlc-trawler install.

It takes about 5 minutes. Your Claude backs everything up before changing
anything.

**Claude:** the rest of this file is written for you. Read all of it
before you act, follow the ground rules, and report back faithfully.

---

## 1. What happened

jlc-trawler is the team's Claude skill for searching the JLCPCB/LCSC parts
catalogue. It reads a local copy of a community dataset published by
[yaqwsx/jlcparts](https://github.com/yaqwsx/jlcparts). The skill's
`update` command downloads that dataset, either directly or through CDFER's
repackaged in-stock file.

**On 2026-09-14 upstream lost its data.** A jlcparts CI run failed to
download its own cache, started a fresh one from a checkpoint, and
published about 5% of the catalogue over the full 7.2-million-part cache.
It also switched to a new file format called `source-db-v2`. CDFER's file
followed. Refetching everything is rate-limited by JLC and will take
upstream months. See jlcparts issues
[#159](https://github.com/yaqwsx/jlcparts/issues/159) and
[#164](https://github.com/yaqwsx/jlcparts/issues/164), and PR
[#165](https://github.com/yaqwsx/jlcparts/pull/165).

**What that did to the old skill:**
- **`update` destroyed the source database.** Anyone who ran it after
  2026-09-14 had their ~1.6 GB `jlcpcb-components.sqlite3` replaced by a
  ~33 MB partial file. The old skill replaced files without checking them.
- **The search tables survived.** The build step crashed on the new format
  before it touched `trawl.sqlite3`, so searches kept working.
- **Some commands broke.** `categories`, `raw-attrs` and `build` stopped
  working, because they need a usable source database.
- **Stock and prices are stale** in any copy made before the wipe.

## 2. What we did

1. **Recovered upstream's last good copy.** yaqwsx's final good publish
   (commit `75daa67a78f3acf4ce18bab8dd68a86ac28c2818`, 2026-09-14 10:00 UTC)
   was still downloadable from GitHub. We saved it: 13 volumes, 681 MB.
   - Every file matches the hashes recorded in that commit, byte for byte.
   - It holds 7,161,863 parts, all fetched between 2026-05-23 and
     2026-09-14 09:18 UTC.
   - Against our previous tables, 294,790 of 294,792 parts are present,
     and part numbers agree on 99.97%.
   - Its stock figures are much closer to live JLC stock than the older
     data's.
2. **Built new search tables from it.** The new `trawl.sqlite3` has 27
   categories and 322,795 in-stock parts (+9.5%). Spec coverage rose by
   more than 10 points on 159 of 188 columns and fell on none, because the
   new format carries full LCSC attributes. JLC also renamed most
   categories; the skill's recipes still match all of them.
3. **Fixed the skill.** The version in this zip reports
   `2026-09-29 (safety guards, v2 import)`. Changes:
   - **Download checks:** `update` and `import` check a download before
     replacing anything. It must have the right format, at least 200k
     parts, and at least 90% of the current file's parts. Otherwise it is
     parked as `*.rejected` and nothing changes.
   - **Staged builds:** `build` works on a copy. The copy is swapped in only
     if no category lost more than 20% of its parts and the total didn't
     drop more than 10%. Otherwise the live tables stay untouched.
   - **Undo:** every replacement keeps a `*.prev` backup, and
     `trawl.py restore derived|source` swaps it back.
   - **New format supported:** upstream's `source-db-v2` is converted
     automatically (`scripts/convert_v2.py`). Once upstream publishes a
     complete catalogue again, `update` will simply work.
   - **New commands:** `import <cache.zip|cache.sqlite3>` loads a saved
     cache, and `status` gives a one-shot health check.
   - **Data date on every result:** `describe` and `search` show
     `data_vintage`, so stale stock is never mistaken for live.
   - **Tables-only installs work:** with no source database, `categories`
     and `raw-attrs` fall back to the search tables.
   - **Windows:** 7-Zip is now found on Windows too.

## 3. What's in this zip

| Item | What it is |
|---|---|
| `jlc-trawler/` | The repaired skill folder |
| `trawl.sqlite3` | New search tables (472 MB, data to 2026-09-14) |
| `SHA256SUMS` | Checksum for `trawl.sqlite3` |
| `prewipe-cache-manifest.txt` | Sizes and hashes of the 13 upstream cache volumes. Only needed for the optional full-data step (§6) |
| `REPAIR-INSTRUCTIONS.md` | This file |

## 4. Ground rules for Claude

1. **Back up before replacing anything.** Never delete the user's existing
   files: copy or move them into the backup folder in step 5.1.
2. **Never pass `--force` or `--allow-shrink`.** If a guard refuses
   something, stop and show the user its message.
3. **Ask before large downloads.** The 681 MB cache in §6 is optional; ask
   the user first.
4. **Report what actually happened.** Include every check that failed.

**Paths and commands** (resolve these first):

| | macOS / Linux | Windows |
|---|---|---|
| Skills folder | `~/.claude/skills/` | `%USERPROFILE%\.claude\skills\` |
| Data folder | `$TRAWL_DATA` if set, else `~/jlc-trawler-data/` | `%TRAWL_DATA%` if set, else `%USERPROFILE%\jlc-trawler-data\` |
| Python | `python3` | `python` or `py -3` |
| Checksum | `shasum -a 256 <file>` | `Get-FileHash <file>` (PowerShell) |

Below, `TRAWL` means `<python> <skills folder>/jlc-trawler/scripts/trawl.py`.

## 5. Repair steps

### 5.1 Back up the current install
Create `~/jlc-trawler-backup-2026-09-29/`. **Put it outside the skills
folder**, or Claude Code will load the old copy as a second skill.
- If `<skills folder>/jlc-trawler/` exists, **move** it to
  `~/jlc-trawler-backup-2026-09-29/skill/`.
- If the data folder exists, **copy** all of its files into
  `~/jlc-trawler-backup-2026-09-29/data/`.
- Before moving on, list the backup with file sizes and confirm it's
  complete.

If the old skill has recipes the new one lacks, or edited recipe files,
compare `skill/scripts/recipes/*.json` in the backup with
`jlc-trawler/scripts/recipes/` in this zip. Tell the user about any
differences before step 5.2, so their custom recipes aren't lost. The
fresh copy ships 27 recipes.

### 5.2 Install the repaired skill
Copy `jlc-trawler/` from this zip into the skills folder, so that
`<skills folder>/jlc-trawler/SKILL.md` exists. Restore any custom recipe
files the user wants to keep into `scripts/recipes/`.

### 5.3 Diagnose the data
Run `TRAWL status`. It is read-only. Read `source_db`, `derived_db` and
`verdict`, then pick the case that fits:

| Case | What `status` shows | Action in 5.4 |
|---|---|---|
| **A. Fresh install** | no data folder, or `derived_db` missing | install the tables |
| **B. Ran `update` after 2026-09-14** | `source_db.ok = false` with the hint "partial source-db-v2 file"; the file is ~33 MB | move the partial file aside, then install the tables |
| **C. Old but healthy** | `source_db.ok = true`, `newest_update` before 2026-09 | move the old source aside, then install the tables |
| **D. Already repaired** | `newest_update` = `2026-09-14 09:18:45` and derived parts = 322,795 | nothing to do; go to 5.5 |

Why move the old source aside in case C: the new tables are newer than
it, and a later `build` would try to rebuild them from older data. The
guard would probably refuse that, but it's cleaner not to leave a
mismatched pair. The file is already safe in the backup.

### 5.4 Install the tables
1. Check `trawl.sqlite3` in this zip against `SHA256SUMS`. It must be
   `8bd28675190e52cbaeb68a0cb7e4b807c699bd1227f33bb71405b7324ac72c89`. If it
   doesn't match, stop: the zip is corrupt.
2. Case B or C: rename `<data folder>/jlcpcb-components.sqlite3` to
   `jlcpcb-components.sqlite3.old-2026-09-29`. Leave any `-wal` / `-shm`
   files beside it alone.
3. Create the data folder if needed, then copy `trawl.sqlite3` into it,
   replacing any old one (it's backed up in 5.1). If old
   `trawl.sqlite3-wal` / `-shm` files are present, move them into the
   backup folder first. They belong to the old file and must not sit next
   to the new one.

### 5.5 Verify
Run `TRAWL status` and check all of these:
- `skill_version` is `2026-09-29 (safety guards, v2 import)`;
- `derived_db.parts` is `322795`, with `recipes` `27` and `empty_recipes` `[]`;
- `data_vintage.source_newest_update` is `2026-09-14 09:18:45`;
- `verdict` is `["OK"]`, or, without a source database, only the
  "no usable source DB" note. That's expected for a tables-only install.

Then run a real search:
`TRAWL search --recipe diode_schottky --where "mpn ~ RB751G-40" --limit 1`
It should return `"lcsc": "C8527"`.

Tell the user to **start a new Claude Code session**. Skills load when a
session starts, so the updated `SKILL.md` takes effect from the next one.
The scripts work immediately.

### 5.6 Report
Tell the user which case applied, where the backup is, the verification
results, and that stock and prices in the tables date from before
2026-09-14. Claude runs `TRAWL refresh <LCSC codes>` for live figures
before anything goes on a BOM.

## 6. Optional: full data (needed for `build` and new recipes)

The tables are enough for every search. The full source database is only
needed to rebuild tables or add new recipes. **Ask the user before
downloading** (681 MB download, about 7.5 GB free disk during import,
about 1.3 GB after).

1. **Get the 13 files** (`cache.zip`, `cache.z01` … `cache.z12`) into one
   folder. Two sources:
   - **Upstream:** `https://raw.githubusercontent.com/yaqwsx/jlcparts/75daa67a78f3acf4ce18bab8dd68a86ac28c2818/data/<file>`.
     No branch points at this commit any more, so GitHub may delete it
     eventually.
   - **Marcus Quek's copy** (Space Bar), if the upstream links fail.

   macOS / Linux:
   ```
   for f in cache.zip cache.z01 cache.z02 cache.z03 cache.z04 cache.z05 cache.z06 cache.z07 cache.z08 cache.z09 cache.z10 cache.z11 cache.z12; do curl -fL --retry 5 -o "$f" "https://raw.githubusercontent.com/yaqwsx/jlcparts/75daa67a78f3acf4ce18bab8dd68a86ac28c2818/data/$f"; done
   ```
   Windows (PowerShell):
   ```
   foreach ($f in @('cache.zip') + (1..12 | % { 'cache.z{0:D2}' -f $_ })) { Invoke-WebRequest -Uri "https://raw.githubusercontent.com/yaqwsx/jlcparts/75daa67a78f3acf4ce18bab8dd68a86ac28c2818/data/$f" -OutFile $f }
   ```
2. **Verify every file** against `prewipe-cache-manifest.txt`, by sha256
   or by git blob sha (`git hash-object <file>`). Stop on any mismatch.
3. **Install 7-Zip** if it's missing:
   - macOS: `brew install sevenzip`
   - Windows: the installer from 7-zip.org; the default folder is found
     automatically.
   - Linux: `7zip` or `p7zip-full`.
4. **Import:** run `TRAWL import <folder>/cache.zip`. It extracts,
   converts, validates and builds, and swaps the result in only if the
   guards pass. It takes about 1–2 minutes. Afterwards `status` should
   show `source_db.parts` 633292 with newest update `2026-09-14 09:18:45`,
   and `verdict` `["OK"]`.
5. **Keep the 13 files.** They are the full backup.

## 7. Going forward

- **`update`:** safe to run. It is refused until upstream publishes a
  complete catalogue again, then it works without anything else changing.
  Check issues #159 / #164 for news.
- **Live stock:** always `refresh` finalists before a part goes on a BOM.
- **If a guard refuses something:** report it, and never override it on
  your own.
- **To undo the last accepted replacement:** `TRAWL restore derived` (or
  `restore source`).
- **Longer term:** JLCPCB offers an official parts API (api.jlcpcb.com).
  It's free but by application, and approval depends on order history.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `status`: "invalid choice" | The old skill is still being run. Check the path, and make sure no second `jlc-trawler` folder sits in the skills folder |
| Checksum mismatch | Download the zip again. Never use a mismatched file |
| `python3` not found (Windows) | Use `python` or `py -3` |
| `import`: "needs 7-Zip" | Install it (§6 step 3) |
| `import`: "7z extraction failed" | All 13 volumes must be in the same folder as `cache.zip` |
| A guard refused | Stop and show the user the message. Nothing was changed |
| Searches show old dates | The wrong `trawl.sqlite3` is in place, or `TRAWL_DATA` points somewhere else. Check `data_dir` in `status` |
