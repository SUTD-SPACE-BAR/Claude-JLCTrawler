# Claude-JLCTrawler
Claude skill for automated searching of electronics parts from JLCPCB Parts Library

Current skill version: **`2026-09-29 (safety guards, v2 import)`**, set by
`SKILL_VERSION` in `jlc-trawler/scripts/trawl.py`.

## Layout

| Path | What it is |
|---|---|
| `jlc-trawler/` | The skill itself. This folder is what gets installed. Start with `SKILL.md`; `README.md` is the user guide, `HANDOFF.md` the design notes. |
| `repair-2026-09-29/` | Record of the repair kit sent out after the upstream data loss: the instructions, the SHA-256 of the shared `trawl.sqlite3`, and hashes of the last good upstream cache volumes. |

The parts data is **not** in git. The skill reads it from `~/jlc-trawler-data/`
(or `$TRAWL_DATA`). See `jlc-trawler/README.md` for setup.

## Installing from this repo

Claude Code loads skills from `~/.claude/skills/`. To have edits tracked here,
link the skill folder instead of copying it:

```bash
ln -s ~/Documents/Claude-JLCTrawler/jlc-trawler ~/.claude/skills/jlc-trawler
```

For the Claude desktop app, zip the `jlc-trawler/` folder and upload it as a
skill. That copy doesn't update itself, so re-upload it after each change.

## Upstream data loss (2026-09-14): keep the guards

On 2026-09-14 the upstream dataset ([yaqwsx/jlcparts](https://github.com/yaqwsx/jlcparts))
replaced its full catalogue with about 5% of it, in a new `source-db-v2` format.
The old skill's `update` overwrote good local data with that partial file.
`repair-2026-09-29/REPAIR-INSTRUCTIONS.md` has the full story.

The 2026-09-29 version fixes this. Keep these protections in every future change:

- `scripts/safety.py`: downloads and builds are checked before they replace
  anything, and every replacement keeps a `.prev` backup (`trawl.py restore`).
- `scripts/convert_v2.py`: imports upstream's `source-db-v2` format.
- `trawl.py import`: loads the saved pre-wipe cache (`cache.zip` + `z01`–`z12`;
  hashes in `repair-2026-09-29/prewipe-cache-manifest.txt`).
- `data_vintage` on every search result, so stale stock is never mistaken for live.

As of 2026-10-24 upstream has not recovered. Issues
[yaqwsx/jlcparts#159](https://github.com/yaqwsx/jlcparts/issues/159) and
[yaqwsx/jlcparts#164](https://github.com/yaqwsx/jlcparts/issues/164) are open,
and the fix [yaqwsx/jlcparts#165](https://github.com/yaqwsx/jlcparts/pull/165)
is unmerged. So `update` is still correctly refused.

## Before committing a change

```bash
python3 jlc-trawler/scripts/units.py
python3 jlc-trawler/scripts/packages.py
python3 jlc-trawler/scripts/trawl.py status
```

`status` should end with `"verdict": ["OK"]`. If you changed the builder or a
recipe, also run a build against the synthetic test DB in a scratch folder,
never in `~/jlc-trawler-data/`:

```bash
mkdir -p /tmp/jt-test
python3 jlc-trawler/scripts/make_test_db.py /tmp/jt-test/jlcpcb-components.sqlite3
python3 jlc-trawler/scripts/trawl.py --dir /tmp/jt-test build
```

The synthetic DB has only 5 categories, so most recipes warn that they matched
zero categories. That's expected; the build should end with `Build complete: 27 recipe(s)`.

Bump `SKILL_VERSION` in `trawl.py` when behaviour changes.
