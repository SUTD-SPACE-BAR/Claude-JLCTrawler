"""
convert_v2.py — turn a jlcparts "source-db-v2" cache into the classic schema
the builder reads (components / categories / manufacturers / v_components).

WHY: in 2026 yaqwsx/jlcparts changed its cache format (meta.format =
'source-db-v2': tables jlc_components + lcsc_components, category names
inline, prices as "1-9:0.12,10-:0.10" text, attributes split between the
JLC and LCSC tables). The builder, its recipes and the safety guards are
written against the classic layout, so rather than fork all of them we
convert once, and everything downstream keeps working unchanged.

Mapping:
  jlc_components.attributes   -> jlc_extra  {"attributes": {...}}  (overlay)
  lcsc_components.attributes  -> extra      {"attributes": {...}}  (base)
  price "a-b:p,c-:q"          -> [{"qFrom":a,"qTo":b,"price":p}, ...]
  fetched_at                  -> last_update
  basic / library_type='base' -> basic

Like CDFER's in-stock file, only parts with stock >= --min-stock (default 5)
are kept.

Usage: python3 convert_v2.py <cache.sqlite3> <out.sqlite3> [--min-stock 5]
"""

import argparse
import json
import os
import sqlite3
import sys


def parse_price(text):
    """'1-9:1.55,10-29:1.27,1000-:0.83' -> builder's JSON tier list."""
    tiers = []
    for chunk in (text or "").split(","):
        if ":" not in chunk:
            continue
        rng, price = chunk.split(":", 1)
        lo, _, hi = rng.partition("-")
        try:
            tiers.append({"qFrom": int(lo), "qTo": int(hi) if hi else None,
                          "price": float(price)})
        except ValueError:
            continue
    return json.dumps(tiers)


def wrap_attrs(text):
    """Store attributes the way the classic `extra` column did."""
    try:
        obj = json.loads(text) if text else {}
    except json.JSONDecodeError:
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    return json.dumps({"attributes": obj}, ensure_ascii=False)


def convert(src_path, out_path, min_stock=5, progress=print):
    src = sqlite3.connect(f"file:{src_path}?mode=ro&immutable=1", uri=True)
    fmt = src.execute("SELECT value FROM meta WHERE key='format'").fetchone()
    if not fmt or fmt[0] != "source-db-v2":
        sys.exit(f"{src_path} is not a source-db-v2 cache (meta.format={fmt}).")
    jcols = {r[1] for r in src.execute("PRAGMA table_info(jlc_components)")}

    if os.path.exists(out_path):
        os.remove(out_path)
    out = sqlite3.connect(out_path)
    out.executescript("""
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE components (
            lcsc INTEGER PRIMARY KEY NOT NULL,
            category_id INTEGER NOT NULL,
            mfr TEXT NOT NULL, package TEXT NOT NULL, joints INTEGER NOT NULL,
            manufacturer_id INTEGER NOT NULL,
            basic INTEGER NOT NULL, preferred INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL, datasheet TEXT NOT NULL,
            stock INTEGER NOT NULL, price TEXT NOT NULL,
            last_update INTEGER NOT NULL, extra TEXT,
            flag INTEGER NOT NULL DEFAULT 0,
            last_on_stock INTEGER NOT NULL DEFAULT 0,
            jlc_extra TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE categories (id INTEGER PRIMARY KEY, category TEXT NOT NULL,
                                 subcategory TEXT NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE VIEW v_components AS
            SELECT c.lcsc, c.category_id, cat.category, cat.subcategory,
                   c.mfr, c.package, c.joints, m.name AS manufacturer,
                   c.basic, c.preferred, c.description, c.datasheet,
                   c.stock, c.last_on_stock, c.price, c.extra, c.jlc_extra
            FROM components c
            LEFT JOIN manufacturers m ON c.manufacturer_id = m.id
            LEFT JOIN categories cat ON c.category_id = cat.id;
    """)

    basic_expr = ("COALESCE(j.basic, 0) OR j.library_type = 'base'"
                  if "basic" in jcols else "j.library_type = 'base'")
    cur = src.execute(f"""
        SELECT j.lcsc, j.category, j.subcategory, j.mfr, j.package, j.joints,
               COALESCE(NULLIF(j.manufacturer, ''), l.manufacturer, '') AS manufacturer,
               ({basic_expr}) AS basic, j.preferred, j.description, j.datasheet,
               j.stock, j.price, j.fetched_at, j.last_on_stock,
               j.attributes AS jlc_attrs, l.attributes AS lcsc_attrs
        FROM jlc_components j
        LEFT JOIN lcsc_components l ON l.lcsc = j.lcsc
        WHERE j.present = 1 AND j.stock >= ?""", (min_stock,))

    cats, mans, batch, n = {}, {}, [], 0
    for r in cur:
        (lcsc, cat, sub, mfr, pkg, joints, man, basic, pref, desc, ds,
         stock, price, fetched, last_on, jattrs, lattrs) = r
        key = (cat or "", sub or "")
        cid = cats.setdefault(key, len(cats) + 1)
        mid = mans.setdefault(man or "", len(mans) + 1)
        batch.append((lcsc, cid, mfr or "", pkg or "", joints or 0, mid,
                      1 if basic else 0, pref or 0, desc or "", ds or "",
                      stock, parse_price(price), fetched or 0,
                      wrap_attrs(lattrs), 0, last_on or 0, wrap_attrs(jattrs)))
        n += 1
        if len(batch) >= 20000:
            out.executemany("INSERT INTO components VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch = []
            progress(f"  {n:,} parts …")
    if batch:
        out.executemany("INSERT INTO components VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    out.executemany("INSERT INTO categories VALUES (?,?,?)",
                    [(i, c, s) for (c, s), i in cats.items()])
    out.executemany("INSERT INTO manufacturers VALUES (?,?)",
                    [(i, m) for m, i in mans.items()])
    out.execute("CREATE INDEX idx_components_category ON components(category_id)")
    out.executemany("INSERT INTO meta VALUES (?,?)", [
        ("converted_from", os.path.abspath(src_path)),
        ("source_format", "source-db-v2"),
        ("min_stock", str(min_stock)),
    ])
    out.commit()
    out.close()
    src.close()
    progress(f"Converted {n:,} parts in {len(cats):,} categories -> {out_path}")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--min-stock", type=int, default=5)
    a = ap.parse_args()
    convert(a.src, a.out, a.min_stock)
