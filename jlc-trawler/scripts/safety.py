"""
safety.py — guards that stop `update` and `build` from destroying good data.

WHY THIS EXISTS: on 2026-09-14 the upstream yaqwsx/jlcparts cache was wiped
by a CI glitch and republished as a small partial set in a new schema. The
CDFER file we download followed it. `update` then did exactly what it was
told: it replaced a good 1.6 GB source DB with a 33 MB partial one, with no
check, and only the schema change stopped the build from overwriting the
derived tables too. If upstream had kept the old schema, every t_<recipe>
table would have been silently rebuilt from ~5% of the catalogue.

The rules this module enforces:
  1. A downloaded source DB replaces the current one only after it passes
     validate_source(): right schema, and not dramatically smaller than
     what it replaces. A rejected download is kept as *.rejected for
     inspection and nothing else is touched.
  2. The derived DB is never built in place. build_safely() builds into a
     staging copy, compares per-recipe part counts against the live DB,
     and refuses to swap in a result that lost a lot of parts unless the
     caller explicitly allows it.
  3. Every replacement keeps one rolling backup (*.prev), and `trawl.py
     restore` swaps it back.
"""

import os
import sqlite3

import builder

# A genuine in-stock snapshot has ~600k+ parts; the partial upstream set
# that triggered this module had 35k. The floor sits well between the two.
MIN_SOURCE_PARTS = 200_000
# A new source may be at most this much smaller than the one it replaces.
MIN_SOURCE_RATIO = 0.90
# Derived DB: refuse the swap if the total, or any recipe with a meaningful
# part count, shrinks below these fractions of its previous size.
MIN_TOTAL_RATIO = 0.90
MIN_RECIPE_RATIO = 0.80
MIN_RECIPE_PARTS_TO_CHECK = 50


def _connect_ro(path, immutable=False):
    # immutable=1 for downloaded source files: nothing else writes them, and
    # it stops SQLite leaving -wal/-shm sidecars next to a file we may rename.
    flag = "&immutable=1" if immutable else ""
    return sqlite3.connect(f"file:{path}?mode=ro{flag}", uri=True)


def source_info(path):
    """Describe a source DB, or explain why it isn't a usable one.
    Returns (info_dict, problem_str_or_None)."""
    if not os.path.exists(path):
        return {}, "file does not exist"
    try:
        conn = _connect_ro(path, immutable=True)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    except sqlite3.DatabaseError as e:
        return {}, f"not a readable SQLite database ({e})"
    info = {"tables": sorted(tables)}
    if "v_components" not in tables or "components" not in tables:
        meta = ""
        if "meta" in tables:
            try:
                meta = "; meta: " + str(dict(conn.execute(
                    "SELECT * FROM meta LIMIT 5").fetchall()))
            except sqlite3.DatabaseError:
                pass
        conn.close()
        return info, ("wrong schema: no components/v_components "
                      f"(tables: {sorted(tables)}{meta}). Upstream format has "
                      "changed; the builder cannot read it.")
    try:
        builder._build_source_select(builder._probe_source_columns(conn))
    except ValueError as e:
        conn.close()
        return info, str(e)
    info["parts"] = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    cols = {d[0].lower() for d in
            conn.execute("SELECT * FROM components LIMIT 0").description}
    if "last_update" in cols:
        info["newest_update"] = conn.execute(
            "SELECT datetime(MAX(last_update),'unixepoch') FROM components"
        ).fetchone()[0]
    conn.close()
    return info, None


def validate_source(new_path, current_path, force=False):
    """Decide whether new_path may replace current_path.
    Returns (ok, info, reasons). Schema problems can never be forced;
    size problems can (force=True) when a real upstream shrink is expected."""
    info, problem = source_info(new_path)
    if problem:
        return False, info, [problem]
    reasons = []
    if info["parts"] < MIN_SOURCE_PARTS:
        reasons.append(f"only {info['parts']:,} parts (a full in-stock snapshot "
                       f"has ~600k; floor is {MIN_SOURCE_PARTS:,})")
    cur, cur_problem = source_info(current_path)
    if not cur_problem and cur.get("parts"):
        if info["parts"] < MIN_SOURCE_RATIO * cur["parts"]:
            reasons.append(f"{info['parts']:,} parts vs {cur['parts']:,} in the "
                           f"current source (below {MIN_SOURCE_RATIO:.0%})")
    if reasons and not force:
        return False, info, reasons
    return True, info, reasons


def recipe_counts(derived_path):
    """{recipe_name: part_count} for every t_<recipe> table, or {} if the
    derived DB doesn't exist."""
    if not os.path.exists(derived_path):
        return {}
    conn = _connect_ro(derived_path)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 't\\_%' ESCAPE '\\'")]
    counts = {n[2:]: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
              for n in names}
    conn.close()
    return counts


def shrink_report(old, new):
    """List the ways `new` lost parts relative to `old` beyond the allowed
    ratios. Empty list means the new build is safe to swap in."""
    problems = []
    for name, n_old in sorted(old.items()):
        n_new = new.get(name, 0)
        if n_old >= MIN_RECIPE_PARTS_TO_CHECK and n_new < MIN_RECIPE_RATIO * n_old:
            problems.append(f"{name}: {n_old:,} -> {n_new:,} parts")
    t_old, t_new = sum(old.values()), sum(new.values())
    if t_old and t_new < MIN_TOTAL_RATIO * t_old:
        problems.append(f"TOTAL: {t_old:,} -> {t_new:,} parts")
    return problems


def _to_single_file(path):
    """Checkpoint and leave WAL mode so the DB is one self-contained file
    (safe to rename, copy or zip)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()


def _snapshot(src_path, dst_path):
    """Consistent copy via SQLite's backup API (safe even mid-WAL)."""
    for p in (dst_path, dst_path + "-wal", dst_path + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    src = _connect_ro(src_path)
    dst = sqlite3.connect(dst_path)
    src.backup(dst)
    dst.close()
    src.close()


def swap_in(new_path, live_path):
    """Move live -> live.prev (one rolling backup), then new -> live.
    Returns True if a backup was made."""
    backed_up = os.path.exists(live_path)
    if backed_up:
        if live_path.endswith(".sqlite3") or live_path.endswith(".db"):
            try:
                _to_single_file(live_path)
            except sqlite3.DatabaseError:
                pass  # not a valid DB (e.g. a bad download); move it as-is
        os.replace(live_path, live_path + ".prev")
    os.replace(new_path, live_path)
    return backed_up


def build_safely(source_path, derived_path, recipes_dir, only=None,
                 allow_shrink=False, progress=print):
    """Build into a staging copy, check it didn't lose parts, then swap it in.
    Returns the builder report. Exits (SystemExit) without touching the live
    derived DB if the source is unusable or the result shrank."""
    info, problem = source_info(source_path)
    if problem:
        raise SystemExit(f"Refusing to build: source DB {source_path} is unusable: "
                         f"{problem}\nThe existing derived tables were not touched.")
    staging = derived_path + ".staging"
    if os.path.exists(derived_path):
        progress("Copying current derived DB to a staging file …")
        _snapshot(derived_path, staging)
    else:
        for p in (staging, staging + "-wal", staging + "-shm"):
            if os.path.exists(p):
                os.remove(p)

    old = recipe_counts(derived_path)
    report = builder.build(source_path, staging, recipes_dir, only=only,
                           progress=progress)
    conn = sqlite3.connect(staging)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('source_parts', ?)",
                 (str(info.get("parts", "")),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('source_newest_update', ?)",
                 (info.get("newest_update") or "",))
    # When this source file was obtained (downloaded, restored or imported).
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('source_downloaded', "
                 "date(?, 'unixepoch', 'localtime'))",
                 (int(os.path.getmtime(source_path)),))
    conn.commit()
    conn.close()
    _to_single_file(staging)

    problems = shrink_report(old, recipe_counts(staging))
    if problems and not allow_shrink:
        rejected = derived_path + ".rejected"
        os.replace(staging, rejected)
        raise SystemExit(
            "Refusing to replace the derived DB: the new build lost parts.\n  "
            + "\n  ".join(problems)
            + f"\nLive DB untouched. The new build is kept at {rejected} for "
              "inspection. If the shrink is real and intended, rerun with "
              "--allow-shrink.")
    if problems:
        progress("WARNING (--allow-shrink): " + "; ".join(problems))
    if swap_in(staging, derived_path):
        progress(f"Swapped in new derived DB. Previous one kept at {derived_path}.prev "
                 "(undo with: trawl.py restore derived).")
    else:
        progress(f"Created derived DB at {derived_path}.")
    return report
