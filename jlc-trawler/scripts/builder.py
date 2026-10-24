"""
builder.py — Turn the raw jlcparts/CDFER SQLite into fast typed tables.

WHY THIS EXISTS
The source database stores each part's parametric attributes as a JSON
blob (columns `extra` and `jlc_extra`) — impossible to filter with SQL
("output current >= 1.2A" can't index into JSON strings). This module
reads declarative RECIPE files (scripts/recipes/*.json) and materializes
one clean, indexed table per recipe into a separate derived database
(trawl.sqlite3), leaving the source DB untouched and read-only.

DATA FLOW (per recipe):
  1. Match categories: recipe's category/subcategory regexes against the
     source `categories` table  -> a set of category_ids.
  2. Stream all components in those categories from `v_components`.
  3. For each part:
       a. Merge attributes: extra["attributes"] overlaid by
          jlc_extra["attributes"] (skipping ""/"-" values) — this is the
          exact merge rule jlcparts itself uses.
       b. For each recipe column, find the first matching attribute name
          (alias list, case-insensitive) and parse it with units.py.
       c. Resolve the physical package size via packages.py.
       d. Pre-compute unit prices at qty 1/10/100/1000 from the price
          tier JSON so SQL can sort by price directly.
  4. Bulk-insert into t_<recipe> and index the numeric columns.

The recipes themselves are also stored (verbatim) in the derived DB's
`meta` table, so the search layer knows each column's parser kind
without re-reading recipe files (keeps DB self-describing).

NOTHING IS EVER LOST: the source DB keeps every raw attribute string; a
typed table only *adds* a queryable view. If a recipe misses an
attribute, `trawl raw-attrs` inspects the raw blobs to fix the recipe.
"""

import json
import os
import re
import sqlite3
import sys

# Allow running both as a module and as a loose script from the skill dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import units  # noqa: E402
import packages  # noqa: E402


# ---------------------------------------------------------------------------
# Attribute merging — mirrors jlcparts' _mergeAttributes() exactly, since
# that logic is battle-tested against this data's quirks:
#   * `extra` may store the attribute dict directly OR under "attributes"
#   * either may be a list (corrupt rows) -> treat as empty
#   * jlc_extra values of "" or "-" must NOT overwrite LCSC values
# ---------------------------------------------------------------------------
def _attr_dict(blob_json):
    """Decode a JSON blob column and return its attribute dict (possibly {})."""
    if not blob_json:
        return {}
    try:
        obj = json.loads(blob_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    attrs = obj.get("attributes", obj if "attributes" not in obj else {})
    if not isinstance(attrs, dict):
        return {}
    return attrs


def merge_attributes(extra_json, jlc_extra_json):
    """LCSC attrs first, then JLC attrs overlay (skipping empty/'-')."""
    merged = dict(_attr_dict(extra_json))
    for key, value in _attr_dict(jlc_extra_json).items():
        if value in ("", "-"):
            continue
        merged[key] = value
    return merged


def _attr_value_to_str(value):
    """Attribute values are usually strings but occasionally dicts like
    {"format": "...", "primary": "...", "values": {...}} or lists.
    Reduce anything non-string to a best-effort string for parsing."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        # jlcparts sometimes pre-normalizes into {"values": {...}, "format": ...};
        # prefer a "primary"/"format" style human string if present.
        for k in ("primary", "format", "value", "raw"):
            if isinstance(value.get(k), str):
                return value[k]
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return ", ".join(_attr_value_to_str(v) for v in value)
    return ""


# ---------------------------------------------------------------------------
# Price tiers: source stores JSON like
#   [{"qFrom": 1, "qTo": 9, "price": 0.0273}, {"qFrom": 10, ...}]
# We pre-compute the unit price at fixed reference quantities so SQL can
# ORDER BY price directly. Exact price at an arbitrary qty is computed at
# query time from the same JSON (stored verbatim in the table).
# ---------------------------------------------------------------------------
PRICE_REF_QTYS = (1, 10, 100, 1000)


def price_at_qty(price_tiers, qty):
    """Unit price for the tier containing `qty`. None if unpriceable."""
    if not price_tiers:
        return None
    best = None
    for tier in price_tiers:
        try:
            q_from = tier.get("qFrom") or 1
            q_to = tier.get("qTo")  # None/0 means "and up"
            price = float(tier["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty >= q_from and (not q_to or qty <= q_to):
            return price
        # Track the highest tier below qty as fallback (qty beyond table).
        if qty >= q_from:
            best = price
    return best


# ---------------------------------------------------------------------------
# Recipe loading and validation.
# ---------------------------------------------------------------------------
_VALID_PARSERS = set(units._UNIT_FAMILIES.keys()) | {"text"}
_COL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Column names that would collide with the common part columns.
_RESERVED = {
    "lcsc", "mpn", "manufacturer", "category", "subcategory", "package",
    "joints", "basic", "preferred", "description", "datasheet", "stock",
    "price_json", "l_mm", "w_mm", "area_mm2", "size_source",
    "price_q1", "price_q10", "price_q100", "price_q1000", "attrs_json",
}


def load_recipes(recipes_dir, only=None):
    """Read and validate every *.json recipe. Returns list of dicts.
    Raises ValueError with a clear message on a malformed recipe — recipes
    are hand-edited, so errors must be loud and specific."""
    recipes = []
    for fn in sorted(os.listdir(recipes_dir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(recipes_dir, fn)
        # encoding is EXPLICIT: recipes may contain non-ASCII match patterns
        # (e.g. cap_polymer's '插件' THT marker) and Windows' default locale
        # codec (cp1252) chokes on them. JSON is UTF-8 by spec anyway.
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
        name = r.get("name")
        if not name or not _COL_NAME_RE.match(name):
            raise ValueError(f"{fn}: recipe 'name' missing or not snake_case")
        if only and name not in only:
            continue
        cm = r.get("category_match", {})
        if not cm.get("category") and not cm.get("subcategory"):
            raise ValueError(f"{fn}: category_match needs 'category' and/or 'subcategory' regex")

        # Allow plain-string entries inside "columns" to serve as inline
        # comments (JSON has no native comment syntax). We drop them here so
        # every later consumer — validation below, _sql_columns_for, the
        # build loop, describe/search — only ever sees real column dicts.
        real_columns = [c for c in r.get("columns", []) if isinstance(c, dict)]
        r["columns"] = real_columns

        for col in real_columns:
            cn = col.get("name", "")
            if not _COL_NAME_RE.match(cn):
                raise ValueError(f"{fn}: bad column name {cn!r}")
            if cn in _RESERVED:
                raise ValueError(f"{fn}: column {cn!r} collides with a built-in column")
            if "flag" in col:
                if not isinstance(col["flag"], list):
                    raise ValueError(f"{fn}:{cn}: 'flag' must be a list of regex strings")
                # flag_false, when present, must likewise be a list of regexes.
                if "flag_false" in col and not isinstance(col["flag_false"], list):
                    raise ValueError(f"{fn}:{cn}: 'flag_false' must be a list of regex strings")
            elif col.get("parse") not in _VALID_PARSERS:
                raise ValueError(
                    f"{fn}:{cn}: parse={col.get('parse')!r} not one of {sorted(_VALID_PARSERS)}")
            if not col.get("attrs"):
                raise ValueError(f"{fn}:{cn}: needs non-empty 'attrs' alias list")
            if col.get("range", "max") not in ("min", "max", "both"):
                raise ValueError(f"{fn}:{cn}: range must be min|max|both")
        r["_file"] = fn
        recipes.append(r)
    return recipes


def _sql_columns_for(recipe):
    """Expand a recipe's column specs into concrete SQL column names.
    A numeric column with range='both' becomes <name>_min and <name>_max.
    Returns list of (sql_name, sql_type, colspec, bound) where bound is
    'min'/'max'/None (None = single-value or text/flag column)."""
    out = []
    for col in recipe.get("columns", []):
        if "flag" in col:
            out.append((col["name"], "INTEGER", col, None))
        elif col.get("parse") == "text":
            out.append((col["name"], "TEXT", col, None))
        elif col.get("range", "max") == "both":
            out.append((col["name"] + "_min", "REAL", col, "min"))
            out.append((col["name"] + "_max", "REAL", col, "max"))
        else:
            out.append((col["name"], "REAL", col, col.get("range", "max")))
    return out


def _match_category_ids(src, recipe):
    """Return the list of category_ids whose category/subcategory match the
    recipe's regexes (case-insensitive re.search on each independently)."""
    cm = recipe["category_match"]
    cat_re = re.compile(cm["category"], re.I) if cm.get("category") else None
    sub_re = re.compile(cm["subcategory"], re.I) if cm.get("subcategory") else None
    ids = []
    matched_names = []
    for cid, cat, sub in src.execute("SELECT id, category, subcategory FROM categories"):
        if cat_re and not cat_re.search(cat or ""):
            continue
        if sub_re and not sub_re.search(sub or ""):
            continue
        ids.append(cid)
        matched_names.append(f"{cat} / {sub}")
    return ids, matched_names


def _find_attr(attrs, aliases):
    """Case-insensitively find the first alias present in the merged
    attribute dict. Aliases are matched as SUBSTRINGS of attribute keys
    when no exact key matches — LCSC keys vary like 'Output Current' vs
    'Current - Output' vs 'Output Current (Max)'. Exact match wins."""
    lower_map = {k.lower(): k for k in attrs}
    # Pass 1: exact (case-insensitive) key match.
    for alias in aliases:
        k = lower_map.get(alias.lower())
        if k is not None:
            return attrs[k]
    # Pass 2: alias as substring of a key.
    for alias in aliases:
        al = alias.lower()
        for kl, k in lower_map.items():
            if al in kl:
                return attrs[k]
    return None


def _probe_source_columns(src):
    """Find out which columns actually exist in the source DB's
    v_components view.

    WHY THIS EXISTS: we originally assumed the exact yaqwsx/jlcparts
    schema (see module docstring), but real downloaded snapshots (e.g.
    CDFER's daily-rebuilt cache.sqlite3) can lag behind or diverge from
    that schema slightly — a column like jlc_extra might not exist yet
    in the snapshot that was published. Rather than crash immediately
    with a raw sqlite3.OperationalError, we detect what's really there
    ONCE per build and build every subsequent SELECT to match reality.

    Returns a set of lowercase column names found on v_components.
    "LIMIT 0" runs the query but fetches zero rows — cheap way to read
    just the column list via cursor.description.
    """
    cur = src.execute("SELECT * FROM v_components LIMIT 0")
    return {d[0].lower() for d in cur.description}


# Columns we NEED to build anything useful. If any of these is missing,
# the source DB schema has changed in a way this tool can't paper over,
# so we stop with a clear error rather than silently building garbage.
_ESSENTIAL_SOURCE_COLUMNS = {
    "lcsc", "category", "subcategory", "mfr", "package",
    "description", "datasheet", "stock", "price", "extra",
}

# Columns that are nice-to-have but safe to default when absent, mapped
# to the SQL literal we substitute in their place. This is what makes the
# builder tolerant of schema drift like the missing jlc_extra column.
_OPTIONAL_SOURCE_DEFAULTS = {
    "manufacturer": "NULL",     # unknown manufacturer name
    "joints": "0",              # pin/pad count unknown -> 0
    "basic": "0",                # assume extended (not basic) if flag absent
    "preferred": "0",           # assume not preferred if flag absent
    "jlc_extra": "'{}'",        # empty JSON object -> merge_attributes()
                                 # then behaves exactly as if JLC added
                                 # nothing on top of the LCSC attributes
}


def _build_source_select(available_cols):
    """Construct the SELECT column list for streaming v_components,
    substituting safe literals for any optional column the real DB is
    missing (see _OPTIONAL_SOURCE_DEFAULTS). Raises ValueError naming the
    exact missing column(s) if something ESSENTIAL is absent — that's a
    real schema break we can't work around, so fail loudly instead of
    producing a derived DB full of nonsense.

    Returns (select_clause_str, ordered_list_of_output_names) where the
    output names are used later to pull values out of each fetched row
    by position (sqlite3.Row still supports name-based access too).
    """
    missing_essential = _ESSENTIAL_SOURCE_COLUMNS - available_cols
    if missing_essential:
        raise ValueError(
            f"Source DB is missing essential column(s): {sorted(missing_essential)}. "
            f"This is a bigger schema change than the builder can adapt to — "
            f"the jlcparts/CDFER database format may have changed. "
            f"Available columns were: {sorted(available_cols)}"
        )

    wanted = ["lcsc", "category", "subcategory", "mfr", "manufacturer",
              "package", "joints", "basic", "preferred", "description",
              "datasheet", "stock", "price", "extra", "jlc_extra"]
    pieces = []
    for col in wanted:
        if col in available_cols:
            pieces.append(col)
        else:
            # Optional column absent from this snapshot -> substitute the
            # documented default, aliased back to the expected name so
            # every downstream `row["colname"]` lookup keeps working
            # unchanged, regardless of what the real DB provided.
            default = _OPTIONAL_SOURCE_DEFAULTS[col]
            pieces.append(f"{default} AS {col}")
    return ", ".join(pieces), wanted



def build(source_db_path, derived_db_path, recipes_dir, only=None, progress=print):
    """Build (or rebuild) every typed table. Main entry point.

    Streams the source DB; memory use is O(1) in part count. Rebuilds are
    idempotent: each t_<recipe> is dropped and recreated atomically."""
    recipes = load_recipes(recipes_dir, only=only)
    if not recipes:
        raise SystemExit("No recipes found/selected — nothing to build.")

    # Source opened read-only via URI so we can never corrupt the download.
    src = sqlite3.connect(f"file:{source_db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    out = sqlite3.connect(derived_db_path)
    out.execute("PRAGMA journal_mode = WAL")
    out.execute("PRAGMA synchronous = NORMAL")

    out.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    # ---- adapt to whatever schema the real downloaded DB actually has ----
    # (see _probe_source_columns / _build_source_select docstrings above for
    # why this is necessary: CDFER's daily snapshot can lag the exact
    # yaqwsx schema we originally coded against, e.g. a missing jlc_extra
    # column). This raises with a clear message if something essential
    # is gone; otherwise it tells us which optional columns got defaulted
    # so we can warn the user once, up front, instead of them wondering
    # later why every part's jlc_extra-derived attributes are empty.
    available_cols = _probe_source_columns(src)
    try:
        select_clause, select_names = _build_source_select(available_cols)
    except ValueError as e:
        raise SystemExit(str(e))
    missing_optional = sorted(
        c for c in _OPTIONAL_SOURCE_DEFAULTS if c not in available_cols)
    if missing_optional:
        progress(f"NOTE: source DB is missing optional column(s) "
                 f"{missing_optional} — defaulting them (see builder.py "
                 f"_OPTIONAL_SOURCE_DEFAULTS). This is expected for some "
                 f"CDFER snapshots and does not break the build.")

    report = {}
    for recipe in recipes:
        name = recipe["name"]
        table = f"t_{name}"
        cat_ids, cat_names = _match_category_ids(src, recipe)
        if not cat_ids:
            progress(f"WARNING: recipe '{name}' matched ZERO categories — "
                     f"check category_match regexes with `trawl categories --grep ...`")
            report[name] = {"categories": 0, "parts": 0}
            continue
        progress(f"[{name}] categories matched ({len(cat_ids)}): "
                 + "; ".join(cat_names[:6]) + ("…" if len(cat_names) > 6 else ""))

        cols = _sql_columns_for(recipe)

        # ---- create the table fresh (drop-and-rebuild keeps this simple) ----
        out.execute(f"DROP TABLE IF EXISTS {table}")
        common = """
            lcsc INTEGER PRIMARY KEY,
            mpn TEXT, manufacturer TEXT,
            category TEXT, subcategory TEXT,
            package TEXT, joints INTEGER,
            basic INTEGER, preferred INTEGER,
            description TEXT, datasheet TEXT,
            stock INTEGER, price_json TEXT,
            price_q1 REAL, price_q10 REAL, price_q100 REAL, price_q1000 REAL,
            l_mm REAL, w_mm REAL, area_mm2 REAL, size_source TEXT,
            attrs_json TEXT
        """
        typed = ", ".join(f"{c} {t}" for c, t, _, _ in cols)
        out.execute(f"CREATE TABLE {table} ({common}{',' if typed else ''} {typed})")

        # ---- stream source rows for the matched categories ----
        # select_clause was built once above from the PROBED schema, so
        # this works whether or not the real DB has jlc_extra/preferred/
        # basic/joints/manufacturer — missing ones arrive as safe defaults
        # (see _build_source_select), but every row["colname"] access below
        # keeps working unchanged.
        placeholders = ",".join("?" * len(cat_ids))
        cur = src.execute(
            f"""SELECT {select_clause}
                FROM v_components WHERE category_id IN ({placeholders})""",
            cat_ids)

        # Placeholder count derived from the table we just created — this
        # can never drift out of sync with the schema above.
        n_table_cols = len(out.execute(f"PRAGMA table_info({table})").fetchall())
        insert_sql = (f"INSERT INTO {table} VALUES ("
                      + ",".join("?" * n_table_cols) + ")")
        batch, n_parts, coverage = [], 0, {c: 0 for c, _, _, _ in cols}

        for row in cur:
            attrs = merge_attributes(row["extra"], row["jlc_extra"])

            # Physical size from package name (see packages.py priority chain).
            size = packages.resolve_package_size(row["package"])

            # Price tiers -> reference unit prices for SQL sorting.
            try:
                tiers = json.loads(row["price"] or "[]")
            except json.JSONDecodeError:
                tiers = []
            prices = [price_at_qty(tiers, q) for q in PRICE_REF_QTYS]

            values = [
                row["lcsc"], row["mfr"], row["manufacturer"],
                row["category"], row["subcategory"],
                row["package"], row["joints"],
                row["basic"], row["preferred"],
                row["description"], row["datasheet"],
                row["stock"], row["price"],
                prices[0], prices[1], prices[2], prices[3],
                size["l_mm"], size["w_mm"], size["area_mm2"], size["source"],
                # Raw merged attributes kept verbatim per part — lets search
                # output show the exact source strings behind every number.
                json.dumps(attrs, ensure_ascii=False),
            ]

            # ---- typed attribute columns ----
            for sql_name, _t, colspec, bound in cols:
                # Step 1: try to find the value in the parametric attributes
                # via the recipe's alias list (exact then substring match).
                raw = _find_attr(attrs, colspec["attrs"])
                raw_s = _attr_value_to_str(raw) if raw is not None else None

                # Step 2: DESCRIPTION FALLBACK.
                # Some specs aren't clean parametric attributes in LCSC data
                # at all — the clearest example is inductor SHIELDING, which
                # is not a parametric field, only a word in the description
                # ("... 10uH Shielded ...", "Semi-shielded", "Unshielded").
                # When a column opts in with "from_description": true and we
                # either found nothing, OR found something that won't parse,
                # we scan the description string as a secondary source.
                #
                # We deliberately run this fallback for flags whenever the
                # attribute lookup produced nothing usable, because that's
                # exactly the shielding situation. For non-flag columns it
                # only kicks in when the attribute was entirely absent, to
                # avoid overriding a real (if unparseable) attribute value.
                if colspec.get("from_description"):
                    # Scavenge description PLUS package name: some properties
                    # live only in the package string — e.g. polymer caps mark
                    # through-hole as '插件,D6.3xL8mm' (Chinese "plug-in") in
                    # `package`, and the description doesn't always repeat it.
                    # Concatenating is safe: flag regexes are specific words
                    # that can't fire on the other field by accident.
                    scavenge_text = f"{row['description'] or ''} {row['package'] or ''}"
                    if "flag" in colspec:
                        # For flags, prefer whichever source actually yields a
                        # definite yes/no. Try the attribute first; if that's
                        # unknown (None), try the description text. flag_false
                        # (if present) enables honest ternary classification.
                        attr_flag = (units.parse_flag(raw_s, colspec["flag"],
                                                      colspec.get("flag_false"))
                                     if raw_s is not None else None)
                        if attr_flag is None:
                            raw_s = scavenge_text
                    elif raw_s is None:
                        raw_s = scavenge_text

                # Step 3: nothing to work with -> store NULL and move on.
                if raw_s is None:
                    values.append(None)
                    continue

                # Step 4: convert the raw string into the typed value
                # according to the column's kind.
                if "flag" in colspec:
                    # Flag: regex-match the truthy patterns, with optional
                    # explicit false patterns for honest ternary results
                    # (see units.parse_flag).
                    v = units.parse_flag(raw_s, colspec["flag"],
                                         colspec.get("flag_false"))
                elif colspec.get("parse") == "text":
                    # Text: store the trimmed string as-is (or NULL if blank).
                    v = raw_s.strip() or None
                else:
                    # Numeric: parse to (min, max) then keep the requested
                    # bound ('min' or 'max'); NULL if it doesn't parse.
                    parsed = units.parse_quantity(raw_s, colspec["parse"])
                    if parsed is None:
                        v = None
                    else:
                        v = parsed[0] if bound == "min" else parsed[1]
                values.append(v)
                if v is not None:
                    coverage[sql_name] += 1

            batch.append(values)
            n_parts += 1
            if len(batch) >= 2000:
                out.executemany(insert_sql, batch)
                batch = []
        if batch:
            out.executemany(insert_sql, batch)

        # ---- indexes on everything we expect to filter/sort by ----
        for c, t, _, _ in cols:
            if t == "REAL" or t == "INTEGER":
                out.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{c} ON {table}({c})")
        for c in ("stock", "area_mm2", "price_q100", "basic", "preferred", "package"):
            out.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{c} ON {table}({c})")
        out.commit()

        cov_str = ", ".join(f"{c}={100 * n // max(n_parts, 1)}%" for c, n in coverage.items())
        progress(f"[{name}] {n_parts} parts ingested. Attribute coverage: {cov_str}")
        report[name] = {"categories": len(cat_ids), "parts": n_parts, "coverage": coverage}

    # Store the recipe set + source info so the DB is self-describing:
    # the search layer reads column parser kinds from here, and `update`
    # can show what the derived DB was built from.
    # IMPORTANT: on a partial build (--only), merge into the existing stored
    # recipe list instead of replacing it — other tables stay described.
    existing = {}
    row = out.execute("SELECT value FROM meta WHERE key='recipes'").fetchone()
    if row:
        try:
            existing = {r["name"]: r for r in json.loads(row[0])}
        except (json.JSONDecodeError, KeyError, TypeError):
            existing = {}
    for r in recipes:
        existing[r["name"]] = {k: v for k, v in r.items() if not k.startswith("_")}
    out.execute("INSERT OR REPLACE INTO meta VALUES ('recipes', ?)",
                (json.dumps(list(existing.values())),))
    out.execute("INSERT OR REPLACE INTO meta VALUES ('source_db', ?)", (source_db_path,))
    out.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', datetime('now'))")
    out.commit()
    out.close()
    src.close()
    return report
