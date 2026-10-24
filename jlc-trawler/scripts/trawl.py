#!/usr/bin/env python3
"""
trawl.py — JLCPCB/LCSC assembly-parts trawler CLI.

One entry point, several subcommands. Typical session:

  # --- occasional maintenance (downloads ~1GB, then builds) ---
  python3 trawl.py update
  python3 trawl.py build

  # --- introspection: ALWAYS run these before constructing a search ---
  python3 trawl.py categories --grep regulator
  python3 trawl.py describe ldo
  python3 trawl.py raw-attrs "Inductors" --grep current

  # --- the actual search ---
  python3 trawl.py search --recipe ldo \
      --where "output_current_a >= 1.2" --where "vin_max_v >= 6" \
      --min-stock 50 --prefer basic --qty 100 --rank smallest --limit 10

  # --- live stock/price confirmation for finalists only ---
  python3 trawl.py refresh C6186 C725790

DATA DIRECTORY: all databases live in one folder, resolved as
  --dir argument  >  $TRAWL_DATA env var  >  ~/jlc-trawler-data
Files inside it:
  jlcpcb-components.sqlite3   the downloaded source DB (read-only to us)
  trawl.sqlite3               derived typed tables (rebuilt by `build`)

All machine-facing output is JSON (via --json where relevant); default
output is human/Claude-readable text.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import builder  # noqa: E402
import convert_v2  # noqa: E402
import safety  # noqa: E402
import units  # noqa: E402

# Windows consoles default to a legacy codepage (cp1252) that can't encode
# characters common in LCSC attribute values (Ω, ±, ℃, µ). Force UTF-8 on
# stdout/stderr so raw-attrs/describe/search output never dies mid-print;
# errors="replace" is a belt-and-braces fallback for anything stranger.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Constants: where the data comes from.
# ---------------------------------------------------------------------------
CDFER_DB_URL = "https://cdfer.github.io/jlcpcb-parts-database/jlcpcb-components.sqlite3"
# yaqwsx publishes the raw DB as a multi-volume zip on the project's pages
# site. It needs 7z (old zip standard) — `update --source yaqwsx` handles it.
YAQWSX_BASE = "https://yaqwsx.github.io/jlcparts/data"
YAQWSX_VOLUMES = ["cache.zip"] + [f"cache.z{i:02d}" for i in range(1, 19)]
# Live JLCPCB per-part detail endpoint (reverse-engineered, used by the
# community for years; may change without notice — refresh degrades
# gracefully if it does).
JLC_DETAIL_URL = "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={code}"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Accept": "application/json, text/plain, */*",
}

# Bump when the data-safety behaviour changes; `status` prints it so a team
# can confirm everyone runs the repaired version.
SKILL_VERSION = "2026-09-29 (safety guards, v2 import)"

SOURCE_DB_NAME = "jlcpcb-components.sqlite3"
DERIVED_DB_NAME = "trawl.sqlite3"
RECIPES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipes")


# ---------------------------------------------------------------------------
# Path helpers.
# ---------------------------------------------------------------------------
def data_dir(args):
    """Resolve the data directory (see module docstring for priority)."""
    d = args.dir or os.environ.get("TRAWL_DATA") or os.path.expanduser("~/jlc-trawler-data")
    os.makedirs(d, exist_ok=True)
    return d


def source_db_path(args):
    return os.path.join(data_dir(args), SOURCE_DB_NAME)


def derived_db_path(args):
    return os.path.join(data_dir(args), DERIVED_DB_NAME)


def open_source(args, required=True):
    """Open the source DB read-only. With required=False, return None instead
    of exiting when it is missing or unusable, so callers can fall back to
    the derived tables (teams that only have trawl.sqlite3 still work)."""
    p = source_db_path(args)
    _, problem = safety.source_info(p)
    if problem:
        if not required:
            return None
        sys.exit(f"Source DB at {p} is unusable: {problem}\n"
                 "search/describe still work from the derived tables.")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def open_derived(args):
    p = derived_db_path(args)
    if not os.path.exists(p):
        sys.exit(f"Derived DB not found at {p}. Run: python3 trawl.py build")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def data_vintage(dconn):
    """When the derived data was built and how fresh its source was. Shown in
    describe/search output so stale stock and prices are never mistaken for
    live figures."""
    meta = dict(dconn.execute(
        "SELECT key, value FROM meta WHERE key IN "
        "('built_at','source_newest_update','source_downloaded')").fetchall())
    return {"built_at": meta.get("built_at"),
            "source_newest_update": meta.get("source_newest_update") or None,
            "source_downloaded": meta.get("source_downloaded") or None,
            "note": "stock/prices are as of these dates; use `refresh` for live values"}


def find_7zip():
    """Locate a 7-Zip binary on any OS (7z / 7za / 7zz on PATH, or the
    default Windows install folder, which is not on PATH by default)."""
    for cand in ("7z", "7za", "7zz"):
        found = shutil.which(cand)
        if found:
            return found
    for path in (r"C:\Program Files\7-Zip\7z.exe",
                 r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(path):
            return path
    return None


def get_recipe_meta(dconn, recipe_name):
    """Read the stored recipe definition for a table from the derived DB's
    meta — this is how `search` knows each column's parser kind."""
    row = dconn.execute("SELECT value FROM meta WHERE key='recipes'").fetchone()
    if not row:
        sys.exit("Derived DB has no recipe metadata. Re-run: python3 trawl.py build")
    for r in json.loads(row[0]):
        if r["name"] == recipe_name:
            return r
    tables = [r["name"] for r in json.loads(row[0])]
    sys.exit(f"No recipe named {recipe_name!r}. Available: {', '.join(tables)}")


# ===========================================================================
# COMMAND: update — download the source DB.
# ===========================================================================
def cmd_update(args):
    d = data_dir(args)
    dest = source_db_path(args)

    if args.source == "cdfer":
        # Plain single-file download with curl. -C - resumes a partial
        # download (the file is ~1GB; connections drop). --fail makes HTTP
        # errors exit non-zero instead of saving an error page as the DB.
        tmp = dest + ".part"
        print(f"Downloading CDFER in-stock DB (~1GB) to {dest} …")
        rc = subprocess.call(["curl", "-L", "--fail", "-C", "-", "-o", tmp, CDFER_DB_URL])
        if rc != 0:
            sys.exit(f"curl failed (exit {rc}). Partial file kept at {tmp}; "
                     f"re-running update will resume.")
        _accept_source(tmp, dest, args)
    else:
        # yaqwsx raw DB: multi-volume zip that only 7z reliably extracts.
        # We download all volumes then extract cache.sqlite3.
        seven = find_7zip()
        if not seven:
            sys.exit("--source yaqwsx needs 7-Zip (7z). Install it, or use --source cdfer.")
        vol_dir = os.path.join(d, "yaqwsx_volumes")
        os.makedirs(vol_dir, exist_ok=True)
        for vol in YAQWSX_VOLUMES:
            url = f"{YAQWSX_BASE}/{vol}"
            out = os.path.join(vol_dir, vol)
            print(f"Downloading {vol} …")
            rc = subprocess.call(["curl", "-L", "--fail", "-C", "-", "-o", out, url])
            if rc != 0:
                # Later volumes may not exist if upstream changed the count;
                # cache.zip missing is fatal, missing .zNN just ends the set.
                if vol == "cache.zip":
                    sys.exit("Failed to download cache.zip — aborting.")
                print(f"  (stopped at {vol}; assuming volume set ends earlier)")
                os.remove(out) if os.path.exists(out) else None
                break
        print("Extracting with 7z …")
        rc = subprocess.call([seven, "x", "-y", os.path.join(vol_dir, "cache.zip"),
                              f"-o{d}"])
        if rc != 0:
            sys.exit("7z extraction failed.")
        extracted = os.path.join(d, "cache.sqlite3")
        if not os.path.exists(extracted):
            sys.exit("cache.sqlite3 not found after extraction.")
        _accept_source(extracted, dest, args)

    size_gb = os.path.getsize(dest) / 1024**3
    print(f"Source DB ready: {dest} ({size_gb:.2f} GiB)")
    if not args.no_build:
        print("Building derived tables …")
        safety.build_safely(dest, derived_db_path(args), RECIPES_DIR,
                            allow_shrink=args.allow_shrink)
        print("Done. Derived DB:", derived_db_path(args))


def _is_v2_cache(path):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        row = conn.execute("SELECT value FROM meta WHERE key='format'").fetchone()
        conn.close()
        return bool(row) and row[0] == "source-db-v2"
    except sqlite3.DatabaseError:
        return False


def _accept_source(new_path, dest, args):
    """Replace the source DB with new_path only if it validates. A rejected
    download is parked as *.rejected; the current source and the derived
    tables are left exactly as they were. Upstream's 2026 'source-db-v2'
    format is converted to the classic schema first (see convert_v2.py)."""
    if _is_v2_cache(new_path):
        converted = dest + ".converted"
        print("Source is in upstream's source-db-v2 format; converting …")
        convert_v2.convert(new_path, converted, progress=lambda m: None)
        if new_path.endswith(".part") or getattr(args, "remove_raw", False):
            os.remove(new_path)
        new_path = converted
    ok, info, reasons = safety.validate_source(new_path, dest, force=args.force)
    if not ok:
        rejected = dest + ".rejected"
        os.replace(new_path, rejected)
        sys.exit("Download REJECTED, nothing replaced:\n  - " + "\n  - ".join(reasons)
                 + f"\nThe download is kept at {rejected} for inspection. "
                   "Existing source and derived DBs are untouched.")
    if reasons:
        print("WARNING (--force): " + "; ".join(reasons))
    backed_up = safety.swap_in(new_path, dest)
    print(f"Accepted new source: {info.get('parts', 0):,} parts, newest update "
          f"{info.get('newest_update')}."
          + (f" Previous source kept at {dest}.prev." if backed_up else ""))


# ===========================================================================
# COMMAND: import — load a local upstream cache (e.g. a saved backup).
# Accepts the multi-volume cache.zip (extracted with 7z) or a cache.sqlite3,
# in either the classic or the source-db-v2 format. Goes through exactly the
# same validation and safe build as `update`.
# ===========================================================================
def cmd_import(args):
    d = data_dir(args)
    dest = source_db_path(args)
    path = os.path.abspath(args.path)
    if not os.path.exists(path):
        sys.exit(f"{path} not found.")
    args.remove_raw = False
    if path.lower().endswith(".zip"):
        seven = find_7zip()
        if not seven:
            sys.exit("Importing a cache.zip needs 7-Zip (7z/7zz). Install it first.")
        work = os.path.join(d, "import_tmp")
        os.makedirs(work, exist_ok=True)
        print(f"Extracting {path} with {seven} (all volumes must sit beside it) …")
        if subprocess.call([seven, "x", "-y", path, f"-o{work}"],
                           stdout=subprocess.DEVNULL) != 0:
            sys.exit("7z extraction failed — are all cache.zNN volumes present?")
        path = os.path.join(work, "cache.sqlite3")
        if not os.path.exists(path):
            sys.exit("cache.sqlite3 not found in the archive.")
        args.remove_raw = True  # our own extracted copy; the zip is the original
    _accept_source(path, dest, args)
    if args.remove_raw and os.path.exists(path):
        os.remove(path)
    work = os.path.join(d, "import_tmp")
    if os.path.isdir(work) and not os.listdir(work):
        os.rmdir(work)
    if not args.no_build:
        print("Building derived tables …")
        safety.build_safely(dest, derived_db_path(args), RECIPES_DIR,
                            allow_shrink=args.allow_shrink)
        print("Done. Derived DB:", derived_db_path(args))


# ===========================================================================
# COMMAND: build — (re)generate typed tables from recipes.
# ===========================================================================
def cmd_build(args):
    only = set(args.only) if args.only else None
    report = safety.build_safely(source_db_path(args), derived_db_path(args),
                                 RECIPES_DIR, only=only,
                                 allow_shrink=args.allow_shrink)
    total = sum(r.get("parts", 0) for r in report.values())
    print(f"\nBuild complete: {len(report)} recipe(s), {total} parts total.")


# ===========================================================================
# COMMAND: status — one-shot health check of the install and its data.
# ===========================================================================
def cmd_status(args):
    d = data_dir(args)
    src, der = source_db_path(args), derived_db_path(args)
    rep = {"skill_version": SKILL_VERSION, "data_dir": d}

    s_info, s_problem = safety.source_info(src)
    rep["source_db"] = ({"ok": True, "parts": s_info.get("parts"),
                         "newest_update": s_info.get("newest_update")}
                        if not s_problem else {"ok": False, "problem": s_problem})
    if s_problem and os.path.exists(src) and _is_v2_cache(src):
        rep["source_db"]["hint"] = ("this is upstream's partial source-db-v2 file from a "
                                    "post-2026-09-14 update; it is useless — move it aside")

    if os.path.exists(der):
        dconn = open_derived(args)
        counts = safety.recipe_counts(der)
        rep["derived_db"] = {"ok": bool(counts), "parts": sum(counts.values()),
                             "recipes": len(counts),
                             "empty_recipes": sorted(k for k, v in counts.items() if not v),
                             "data_vintage": data_vintage(dconn)}
    else:
        rep["derived_db"] = {"ok": False, "problem": "missing"}

    rep["backups"] = sorted(f for f in os.listdir(d)
                            if f.endswith((".prev", ".rejected")))
    verdict = []
    if not rep["derived_db"].get("ok"):
        verdict.append("NO SEARCH TABLES: install trawl.sqlite3 or run `import`")
    if not rep["source_db"]["ok"]:
        verdict.append("no usable source DB (fine for searching; only `build` and "
                       "new recipes need one, via `import`)")
    newest = (rep.get("derived_db", {}).get("data_vintage") or {}).get("source_newest_update") or ""
    if newest and newest < "2026-09-01":
        verdict.append(f"tables are old (newest source record {newest[:10]}); "
                       "consider the 2026-09-14 tables")
    rep["verdict"] = verdict or ["OK"]
    print(json.dumps(rep, indent=1))


# ===========================================================================
# COMMAND: restore — swap a DB back with its rolling *.prev backup.
# Running it twice undoes the restore.
# ===========================================================================
def cmd_restore(args):
    live = derived_db_path(args) if args.which == "derived" else source_db_path(args)
    prev = live + ".prev"
    if not os.path.exists(prev):
        sys.exit(f"No backup at {prev} — nothing to restore.")
    tmp = live + ".swap"
    if os.path.exists(live):
        os.replace(live, tmp)
    os.replace(prev, live)
    if os.path.exists(tmp):
        os.replace(tmp, prev)
    print(f"Restored {live} from its backup. The replaced file is now {prev} "
          "(run restore again to undo).")


# ===========================================================================
# COMMAND: categories — list/browse source categories.
# ===========================================================================
def _derived_tables(dconn):
    return [r[0] for r in dconn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 't\\_%' ESCAPE '\\'")]


def cmd_categories(args):
    src = open_source(args, required=False)
    if src is not None:
        # Join with a part count so it's obvious which categories matter.
        rows = src.execute("""
            SELECT cat.category, cat.subcategory, COUNT(c.lcsc) AS n
            FROM categories cat LEFT JOIN components c ON c.category_id = cat.id
            GROUP BY cat.id ORDER BY cat.category, cat.subcategory
        """).fetchall()
    else:
        # No usable source DB (e.g. a teammate who installed only the shared
        # trawl.sqlite3): list what the derived tables contain instead. This
        # only covers categories some recipe already ingests.
        print("NOTE: no usable source DB; listing categories from the derived "
              "tables (recipe-covered categories only).", file=sys.stderr)
        dconn = open_derived(args)
        union = " UNION ALL ".join(
            f"SELECT category, subcategory FROM {t}" for t in _derived_tables(dconn))
        rows = dconn.execute(f"""
            SELECT category, subcategory, COUNT(*) AS n FROM ({union})
            GROUP BY category, subcategory ORDER BY category, subcategory
        """).fetchall()
    pat = re.compile(args.grep, re.I) if args.grep else None
    out = []
    for r in rows:
        line = f"{r['category']} / {r['subcategory']}"
        if pat and not pat.search(line):
            continue
        out.append({"category": r["category"], "subcategory": r["subcategory"], "parts": r["n"]})
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        for o in out:
            print(f"{o['parts']:>7}  {o['category']} / {o['subcategory']}")
        print(f"\n{len(out)} categories" + (f" matching {args.grep!r}" if args.grep else ""))


# ===========================================================================
# COMMAND: raw-attrs — inspect raw attribute keys for a category.
# This is THE recipe-tuning tool: shows what attribute names actually
# exist in the data (sampled), with counts and example values, so recipe
# alias lists can be written against reality instead of guesses.
# ===========================================================================
def cmd_raw_attrs(args):
    src = open_source(args, required=False)
    pat = re.compile(args.category, re.I)
    if src is None:
        print("NOTE: no usable source DB; sampling the derived tables "
              "(recipe-covered categories only).", file=sys.stderr)
        dconn = open_derived(args)
        union = " UNION ALL ".join(
            f"SELECT category, subcategory, stock, attrs_json FROM {t}"
            for t in _derived_tables(dconn))
        rows_all = dconn.execute(
            f"SELECT * FROM ({union}) ORDER BY stock DESC").fetchall()
        picked = [r for r in rows_all
                  if pat.search(f"{r['category']} / {r['subcategory']}")][:args.samples]
        if not picked:
            sys.exit(f"No derived-table parts match {args.category!r}.")
        attr_rows = [(json.loads(r["attrs_json"] or "{}"),) for r in picked]
        _print_attr_sample(attr_rows, args)
        return
    cat_ids = [r["id"] for r in src.execute("SELECT id, category, subcategory FROM categories")
               if pat.search(f"{r['category']} / {r['subcategory']}")]
    if not cat_ids:
        sys.exit(f"No categories match {args.category!r}. Try: trawl.py categories --grep …")

    placeholders = ",".join("?" * len(cat_ids))

    # SCHEMA-DRIFT TOLERANCE (same issue the builder handles):
    # Some source snapshots (notably CDFER's) don't carry the jlc_extra
    # column. Probe the components table's real columns first, then only
    # SELECT jlc_extra if it exists — otherwise substitute an empty JSON
    # object literal so merge_attributes() still works (it just sees no
    # JLC-side overlay). "LIMIT 0" reads the column list without fetching
    # any rows.
    components_cols = {
        d[0].lower()
        for d in src.execute("SELECT * FROM components LIMIT 0").description
    }
    jlc_expr = "jlc_extra" if "jlc_extra" in components_cols else "'{}' AS jlc_extra"

    # Sample the highest-stock parts: they're the most representative and
    # the most likely to have well-populated attribute sets.
    cur = src.execute(
        f"""SELECT extra, {jlc_expr} FROM components
            WHERE category_id IN ({placeholders})
            ORDER BY stock DESC LIMIT ?""",
        cat_ids + [args.samples])

    _print_attr_sample(
        ((builder.merge_attributes(row["extra"], row["jlc_extra"]),) for row in cur),
        args)


def _print_attr_sample(attr_rows, args):
    counts, examples = {}, {}
    n = 0
    for (attrs,) in attr_rows:
        n += 1
        for k, v in attrs.items():
            counts[k] = counts.get(k, 0) + 1
            if k not in examples:
                examples[k] = builder._attr_value_to_str(v)[:60]

    grep = re.compile(args.grep, re.I) if args.grep else None
    rows = sorted(counts.items(), key=lambda kv: -kv[1])
    print(f"Sampled {n} parts. Attribute keys (count / example value):\n")
    for k, c in rows:
        if grep and not grep.search(k):
            continue
        print(f"{c:>5}/{n}  {k!r:45s} e.g. {examples[k]!r}")


# ===========================================================================
# COMMAND: describe — typed columns of a built table with coverage/ranges.
# Claude runs this BEFORE building --where clauses so every referenced
# column is real and every threshold is within the data's actual range.
# ===========================================================================
def cmd_describe(args):
    dconn = open_derived(args)
    recipe = get_recipe_meta(dconn, args.recipe)
    table = f"t_{args.recipe}"
    total = dconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    out = {"recipe": args.recipe, "display": recipe.get("display", ""),
           "parts": total, "data_vintage": data_vintage(dconn), "columns": []}

    # Reconstruct SQL column list the same way the builder did.
    for sql_name, sql_type, colspec, bound in builder._sql_columns_for(recipe):
        info = {"column": sql_name,
                "kind": "flag" if "flag" in colspec else colspec.get("parse"),
                "from_attrs": colspec["attrs"]}
        nn = dconn.execute(
            f"SELECT COUNT({sql_name}) FROM {table}").fetchone()[0]
        info["coverage"] = f"{100 * nn // max(total, 1)}%"
        if sql_type == "REAL":
            mn, mx = dconn.execute(
                f"SELECT MIN({sql_name}), MAX({sql_name}) FROM {table}").fetchone()
            info["min"], info["max"] = mn, mx
        else:
            top = dconn.execute(
                f"""SELECT {sql_name}, COUNT(*) c FROM {table}
                    WHERE {sql_name} IS NOT NULL
                    GROUP BY {sql_name} ORDER BY c DESC LIMIT 8""").fetchall()
            info["top_values"] = {str(r[0]): r[1] for r in top}
        out["columns"].append(info)

    # Common columns worth knowing about too.
    pk = dconn.execute(
        f"""SELECT package, COUNT(*) c FROM {table}
            GROUP BY package ORDER BY c DESC LIMIT 12""").fetchall()
    out["top_packages"] = {r[0]: r[1] for r in pk}
    basics = dconn.execute(
        f"SELECT SUM(basic), SUM(preferred) FROM {table}").fetchone()
    out["basic_parts"], out["preferred_parts"] = basics[0], basics[1]

    print(json.dumps(out, indent=1))


# ===========================================================================
# COMMAND: search — the main event.
# ===========================================================================
# --where clause grammar: "<column> <op> <value>"
#   ops: >= <= != = > < ~        (~ means case-insensitive contains, TEXT only)
#   value: number, or unit string ("1.2A", "10uH") parsed with the column's
#   declared kind so thresholds can be written naturally.
_WHERE_RE = re.compile(r"^\s*([a-z][a-z0-9_]*)\s*(>=|<=|!=|=|>|<|~)\s*(.+?)\s*$")

# Columns always allowed in --where beyond recipe columns.
_COMMON_NUMERIC = {"stock": "number", "joints": "number", "area_mm2": "number",
                   "l_mm": "number", "w_mm": "number",
                   "price_q1": "number", "price_q10": "number",
                   "price_q100": "number", "price_q1000": "number"}
_COMMON_TEXT = {"package", "mpn", "manufacturer", "description", "subcategory"}


def _column_kinds(recipe):
    """Map every legal --where column of this recipe's table to its parser
    kind ('text' for TEXT columns)."""
    kinds = dict(_COMMON_NUMERIC)
    for c in _COMMON_TEXT:
        kinds[c] = "text"
    for sql_name, sql_type, colspec, _b in builder._sql_columns_for(recipe):
        if "flag" in colspec:
            kinds[sql_name] = "number"  # flags filter as 0/1
        elif colspec.get("parse") == "text":
            kinds[sql_name] = "text"
        else:
            kinds[sql_name] = colspec["parse"]
    return kinds


def _parse_where(clause, kinds):
    """One --where string -> (sql_fragment, params). Exits with a helpful
    message on any error — bad clauses must never silently match nothing."""
    m = _WHERE_RE.match(clause)
    if not m:
        sys.exit(f"Bad --where {clause!r}. Format: '<column> <op> <value>', "
                 f"e.g. --where 'output_current_a >= 1.2'")
    col, op, val = m.groups()
    if col not in kinds:
        sys.exit(f"Unknown column {col!r} in --where. Run "
                 f"`trawl.py describe <recipe>` to list valid columns. "
                 f"Valid: {', '.join(sorted(kinds))}")
    kind = kinds[col]
    if op == "~":
        if kind != "text":
            sys.exit(f"Operator '~' (contains) is only for text columns; {col} is {kind}.")
        return f"{col} LIKE ? COLLATE NOCASE", [f"%{val}%"]
    if kind == "text":
        if op in ("=", "!="):
            return f"{col} {op if op != '=' else '='} ? COLLATE NOCASE", [val]
        sys.exit(f"{col} is text — use =, != or ~, not {op}.")
    # Numeric: accept bare numbers or unit strings ("1.2A", "10uH", "3.3V").
    parsed = units.parse_quantity(val, kind)
    if parsed is None:
        sys.exit(f"Could not parse value {val!r} for column {col} (kind={kind}).")
    return f"{col} {op} ?", [parsed[1]]


# --rank presets: (SQL ORDER BY, human explanation)
# NULLs are pushed last so parts missing a size/price never win by default.
_RANKS = {
    "smallest": ("area_mm2 IS NULL, area_mm2 ASC, price_q100 ASC", "footprint area"),
    "cheapest": ("PRICE_COL IS NULL, PRICE_COL ASC, area_mm2 ASC", "unit price"),
    "stock":    ("stock DESC", "stock depth"),
    # balanced: normalized score computed in Python post-fetch (see below).
    "balanced": (None, "size+price+stock composite"),
}


def cmd_search(args):
    dconn = open_derived(args)
    recipe = get_recipe_meta(dconn, args.recipe)
    table = f"t_{args.recipe}"
    kinds = _column_kinds(recipe)

    where_sql, params = [], []
    for clause in args.where or []:
        frag, p = _parse_where(clause, kinds)
        where_sql.append(frag)
        params.extend(p)

    if args.min_stock:
        where_sql.append("stock >= ?")
        params.append(args.min_stock)
    if args.package:
        where_sql.append("package LIKE ? COLLATE NOCASE")
        params.append(f"%{args.package}%")
    if args.basic_only:
        where_sql.append("basic = 1")

    # Pick the pre-computed price column nearest the requested quantity —
    # exact per-qty price is recomputed from tiers for the output rows.
    qty = args.qty
    price_col = ("price_q1" if qty < 10 else "price_q10" if qty < 100
                 else "price_q100" if qty < 1000 else "price_q1000")

    order, rank_reason = _RANKS[args.rank]
    fetch_n = max(args.limit * 5, 100) if args.rank == "balanced" else args.limit
    if order:
        order = order.replace("PRICE_COL", price_col)
        # --prefer floats basic/preferred parts to the top WITHOUT excluding
        # extended parts — an extended part can still win on merit; this just
        # breaks ties and biases the ordering.
        if args.prefer == "basic":
            order = "basic DESC, preferred DESC, " + order
        elif args.prefer == "preferred":
            order = "(basic + preferred) DESC, " + order
        sql = (f"SELECT * FROM {table}"
               + (" WHERE " + " AND ".join(where_sql) if where_sql else "")
               + f" ORDER BY {order} LIMIT {fetch_n}")
    else:
        # balanced: fetch a wider pool ordered by nothing fancy, score in Python.
        sql = (f"SELECT * FROM {table}"
               + (" WHERE " + " AND ".join(where_sql) if where_sql else "")
               + f" ORDER BY {price_col} IS NULL, {price_col} ASC LIMIT {fetch_n}")

    rows = [dict(r) for r in dconn.execute(sql, params)]

    if args.rank == "balanced" and rows:
        # Composite score: min-max normalize area, price, and log-ish stock
        # over the candidate pool; lower = better. Parts missing a value get
        # the worst normalized score for that term rather than being dropped.
        import math
        areas = [r["area_mm2"] for r in rows if r["area_mm2"]]
        prices = [r[price_col] for r in rows if r[price_col]]
        stocks = [math.log10(max(r["stock"], 1)) for r in rows]

        def norm(v, pool, invert=False):
            if v is None or not pool or max(pool) == min(pool):
                return 1.0
            x = (v - min(pool)) / (max(pool) - min(pool))
            return 1.0 - x if invert else x

        for r in rows:
            r["_score"] = (
                norm(r["area_mm2"], areas)              # smaller better
                + norm(r[price_col], prices)            # cheaper better
                + norm(math.log10(max(r["stock"], 1)), stocks, invert=True)  # deeper better
                # small bonus for basic/preferred (no feeder-loading fee)
                - 0.3 * (r["basic"] or 0) - 0.15 * (r["preferred"] or 0)
            )
        rows.sort(key=lambda r: r["_score"])
        rows = rows[:args.limit]

    # ---- shape the output records ----
    typed_cols = [c for c, _t, _s, _b in builder._sql_columns_for(recipe)]
    results = []
    for r in rows:
        tiers = json.loads(r["price_json"] or "[]")
        exact_price = builder.price_at_qty(tiers, qty)
        rec = {
            "lcsc": f"C{r['lcsc']}",
            "mpn": r["mpn"],
            "manufacturer": r["manufacturer"],
            "description": r["description"],
            "package": r["package"],
            "footprint_mm": [r["l_mm"], r["w_mm"]] if r["l_mm"] else None,
            "footprint_area_mm2": r["area_mm2"],
            "footprint_source": r["size_source"],
            "library_type": ("basic" if r["basic"] else
                             "preferred" if r["preferred"] else "extended"),
            "stock": r["stock"],
            "price_at_qty": exact_price,
            "qty": qty,
            "datasheet_url": r["datasheet"],
            # Every typed attribute value — the grounded facts for the
            # recommendation write-up.
            "matched": {c: r[c] for c in typed_cols if r.get(c) is not None},
        }
        if args.raw_attrs:
            rec["raw_attributes"] = json.loads(r["attrs_json"] or "{}")
        results.append(rec)

    print(json.dumps({
        "recipe": args.recipe,
        "data_vintage": data_vintage(dconn),
        "rank": args.rank,
        "rank_by": rank_reason,
        "filters": args.where or [],
        "candidates": results,
    }, indent=1, ensure_ascii=False))


# ===========================================================================
# COMMAND: refresh — live stock/price for a shortlist (network!).
# ===========================================================================
def cmd_refresh(args):
    out = []
    for code in args.codes:
        code = code.upper()
        if not code.startswith("C"):
            code = "C" + code
        url = JLC_DETAIL_URL.format(code=code)
        rec = {"lcsc": code}
        try:
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
            data = payload.get("data") or {}
            # Field names on this endpoint have shifted over the years;
            # probe the known spellings and take the first present.
            for stock_key in ("stockCount", "stock", "quantity"):
                if stock_key in data:
                    rec["live_stock"] = data[stock_key]
                    break
            for price_key in ("prices", "jlcPrices", "priceList"):
                if data.get(price_key):
                    rec["live_prices"] = data[price_key]
                    break
            if "live_stock" not in rec and "live_prices" not in rec:
                rec["note"] = "Endpoint responded but fields unrecognized — endpoint may have changed."
        except Exception as e:  # noqa: BLE001 — degrade gracefully, per-part
            rec["error"] = f"{type(e).__name__}: {e}"
        out.append(rec)
    print(json.dumps(out, indent=1))
    print("\nNote: live endpoint is unofficial; on persistent errors just "
          "check jlcpcb.com/parts. The local DB's stock/prices date from its "
          "build (see `describe`), so refresh anything you're about to order.",
          file=sys.stderr)


# ===========================================================================
# Argument parsing.
# ===========================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="data directory (default $TRAWL_DATA or ~/jlc-trawler-data)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("update", help="download source DB (and build)")
    p.add_argument("--source", choices=["cdfer", "yaqwsx"], default="cdfer")
    p.add_argument("--no-build", action="store_true", help="skip the build step")
    p.add_argument("--force", action="store_true",
                   help="accept a download that is much smaller than expected "
                        "(schema problems are never accepted)")
    p.add_argument("--allow-shrink", action="store_true",
                   help="swap in a derived build even if it lost many parts")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("build", help="(re)build typed tables from recipes")
    p.add_argument("--only", nargs="+", help="rebuild only these recipe names")
    p.add_argument("--allow-shrink", action="store_true",
                   help="swap in the new build even if it lost many parts")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("import", help="load a local cache.zip / cache.sqlite3 (and build)")
    p.add_argument("path", help="cache.zip (with its .zNN volumes beside it) or cache.sqlite3")
    p.add_argument("--no-build", action="store_true", help="skip the build step")
    p.add_argument("--force", action="store_true",
                   help="accept a cache much smaller than expected")
    p.add_argument("--allow-shrink", action="store_true",
                   help="swap in a derived build even if it lost many parts")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("status", help="health check of the install and its data")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("restore", help="swap a DB back with its *.prev backup")
    p.add_argument("which", choices=["derived", "source"])
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("categories", help="list source categories")
    p.add_argument("--grep", help="case-insensitive regex filter")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_categories)

    p = sub.add_parser("raw-attrs", help="sample raw attribute keys for a category")
    p.add_argument("category", help="regex matched against 'Category / Subcategory'")
    p.add_argument("--samples", type=int, default=500)
    p.add_argument("--grep", help="filter attribute names")
    p.set_defaults(func=cmd_raw_attrs)

    p = sub.add_parser("describe", help="show a recipe table's columns/coverage/ranges")
    p.add_argument("recipe")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("search", help="parametric part search")
    p.add_argument("--recipe", required=True)
    p.add_argument("--where", action="append",
                   help="repeatable: '<column> <op> <value>', value may carry units")
    p.add_argument("--min-stock", type=int, default=0)
    p.add_argument("--package", help="substring filter on package name")
    p.add_argument("--prefer", choices=["basic", "preferred", "any"], default="any",
                   help="float basic/preferred parts to the top (never excludes)")
    p.add_argument("--basic-only", action="store_true",
                   help="HARD filter to basic parts only")
    p.add_argument("--qty", type=int, default=100, help="quantity for unit pricing")
    p.add_argument("--rank", choices=list(_RANKS), default="balanced")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--raw-attrs", action="store_true",
                   help="include full raw attribute dict per candidate")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("refresh", help="live stock/price for LCSC codes (network)")
    p.add_argument("codes", nargs="+")
    p.set_defaults(func=cmd_refresh)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
