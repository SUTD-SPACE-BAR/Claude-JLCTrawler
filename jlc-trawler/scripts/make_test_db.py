"""
make_test_db.py — Build a tiny synthetic source DB for pipeline testing.

Replicates the EXACT yaqwsx/jlcparts schema (components/categories/
manufacturers/v_components) and populates it with hand-crafted parts
whose attribute blobs mimic real LCSC messiness: unit suffixes, ranges,
"@" conditions, missing values, jlc_extra overlays, corrupt JSON.

Usage: python3 make_test_db.py /tmp/test-source.sqlite3
NOT part of the production skill — testing infrastructure only.
"""

import json
import sqlite3
import sys


def make(path):
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.executescript("""
        DROP TABLE IF EXISTS components;
        DROP TABLE IF EXISTS categories;
        DROP TABLE IF EXISTS manufacturers;
        DROP VIEW IF EXISTS v_components;
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
        CREATE TABLE categories (id INTEGER PRIMARY KEY, category TEXT NOT NULL, subcategory TEXT NOT NULL);
        CREATE VIEW v_components AS
            SELECT c.lcsc, c.category_id, cat.category, cat.subcategory,
                   c.mfr, c.package, c.joints, m.name AS manufacturer,
                   c.basic, c.preferred, c.description, c.datasheet,
                   c.stock, c.last_on_stock, c.price, c.extra, c.jlc_extra
            FROM components c
            LEFT JOIN manufacturers m ON c.manufacturer_id = m.id
            LEFT JOIN categories cat ON c.category_id = cat.id;
    """)

    c.executemany("INSERT INTO categories VALUES (?,?,?)", [
        (1, "Power Management ICs", "Linear Voltage Regulators (LDO)"),
        (2, "Power Management ICs", "DC-DC Converters"),
        (3, "Inductors, Coils, Chokes", "Power Inductors"),
        (4, "Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT"),
        (5, "Resistors", "Chip Resistor - Surface Mount"),
        (6, "Crystals", "SMD Crystal Resonators"),
        (7, "Crystals", "DIP Crystal Resonators"),
        (8, "Transistors", "MOSFETs"),
    ])
    c.executemany("INSERT INTO manufacturers VALUES (?,?)", [
        (1, "Texas Instruments"), (2, "Sunlord"), (3, "UNI-ROYAL"), (4, "Samsung"),
        (5, "YXC"), (6, "Infineon"),
    ])

    price = lambda *tiers: json.dumps(
        [{"qFrom": q, "qTo": t, "price": p} for q, t, p in tiers])
    attrs = lambda d: json.dumps({"attributes": d})

    parts = [
        # --- LDOs: vary current, dropout, package, basic-ness ---
        (6186, 1, "XC6206P332MR", "SOT-23-3", 3, 1, 1, 0,
         "3.3V 200mA LDO", "https://ds/xc6206.pdf", 500000,
         price((1, 9, 0.02), (10, 99, 0.015), (100, 0, 0.01)), 0,
         attrs({"Output Current": "200mA", "Output Voltage": "3.3V",
                "Input Voltage": "1.8V~6V", "Dropout Voltage": "250mV @ 100mA",
                "Quiescent Current": "1uA", "Output Type": "Fixed"}), 0, 0, "{}"),
        (23456, 1, "AMS1117-3.3", "SOT-223", 4, 1, 1, 0,
         "3.3V 1A LDO", "https://ds/ams1117.pdf", 900000,
         price((1, 9, 0.12), (10, 99, 0.09), (100, 0, 0.07)), 0,
         attrs({"Output Current": "1A", "Output Voltage": "3.3V",
                "Input Voltage": "4.5V~15V", "Dropout Voltage": "1.1V @ 800mA",
                "Quiescent Current": "5mA", "Output Type": "Fixed"}), 0, 0, "{}"),
        (34567, 1, "TPS7A2033PDBVR", "SOT-23-5", 5, 1, 0, 1,
         "3.3V 300mA low-Iq LDO", "https://ds/tps7a20.pdf", 40000,
         price((1, 9, 0.35), (10, 99, 0.28), (100, 0, 0.22)), 0,
         attrs({"Output Current": "300mA", "Output Voltage": "3.3V",
                "Input Voltage": "1.6V~6V", "Dropout Voltage": "120mV @ 300mA",
                "Quiescent Current (Iq)": "5.7uA", "Output Type": "Fixed"}),
         0, 0,
         # jlc_extra overlay: JLC adds an attribute + tries to blank one (must be ignored)
         json.dumps({"attributes": {"Number of Outputs": "1", "Output Current": "-"}})),
        (45678, 1, "ME6211C33M5G", "SOT-23-5", 5, 2, 0, 0,
         "3.3V 500mA LDO", "https://ds/me6211.pdf", 120000,
         price((1, 9, 0.05), (10, 99, 0.04), (100, 0, 0.03)), 0,
         attrs({"Output Current": "500mA", "Output Voltage": "3.3V",
                "Input Voltage": "2V~6.5V", "Dropout Voltage": "100mV @ 100mA",
                "Output Type": "Fixed"}), 0, 0, "{}"),
        (56789, 1, "BROKEN-EXTRA", "DFN-6", 7, 2, 0, 0,
         "LDO with corrupt extra blob", "https://ds/x.pdf", 800,
         price((1, 0, 0.10)), 0,
         "{not valid json!!", 0, 0, "{}"),  # corrupt on purpose — must not crash

        # --- DC-DC ---
        (7890, 2, "TPS5450DDA", "SOIC-8", 9, 1, 0, 0,
         "5A 500kHz step-down converter", "https://ds/tps5450.pdf", 15000,
         price((1, 9, 1.2), (10, 0, 0.95)), 0,
         attrs({"Output Current": "5A", "Input Voltage": "5.5V~36V",
                "Output Voltage": "1.22V~31V", "Switching Frequency": "500kHz",
                "Topology": "Buck"}), 0, 0, "{}"),

        # --- Power inductors: the shielded-flag showcase ---
        (101, 3, "SWPA4030S100MT", "SMD,4x4x3mm", 2, 2, 1, 0,
         "10uH shielded power inductor", "https://ds/swpa.pdf", 80000,
         price((1, 9, 0.04), (10, 0, 0.03)), 0,
         attrs({"Inductance": "10uH", "Rated Current": "1.1A",
                "Saturation Current": "1.5A", "DC Resistance": "150mΩ",
                "Shielding": "Shielded", "Tolerance": "±20%"}), 0, 0, "{}"),
        (102, 3, "NRS5030T100M", "SMD,5x5x3mm", 2, 2, 0, 0,
         "10uH semi-shielded inductor", "https://ds/nrs.pdf", 30000,
         price((1, 9, 0.09), (10, 0, 0.07)), 0,
         attrs({"Inductance": "10uH", "Rated Current": "2.1A",
                "Saturation Current": "2.6A", "DC Resistance": "72mΩ",
                "Shielding": "Semi-Shielded", "Tolerance": "±20%"}), 0, 0, "{}"),
        (103, 3, "CDRH104R-100M", "SMD,10x10x4mm", 2, 2, 0, 0,
         "10uH unshielded inductor", "https://ds/cdrh.pdf", 5000,
         price((1, 0, 0.15)), 0,
         attrs({"Inductance": "10uH", "Rated Current": "4A",
                "Saturation Current": "5A", "DC Resistance": "23mΩ",
                "Shielding": "Unshielded", "Tolerance": "±20%"}), 0, 0, "{}"),

        # --- MLCC ---
        (201, 4, "CL10A106KP8NNNC", "0603", 2, 4, 1, 0,
         "10uF 10V X5R 0603", "https://ds/cl10.pdf", 2000000,
         price((1, 0, 0.008)), 0,
         attrs({"Capacitance": "10uF", "Voltage Rated": "10V",
                "Tolerance": "±10%", "Temperature Coefficient": "X5R"}), 0, 0, "{}"),

        # --- Resistor with R-notation resistance ---
        (301, 5, "0402WGF4R70TCE", "0402", 2, 3, 1, 0,
         "4.7Ω ±1% 1/16W 0402", "https://ds/r.pdf", 5000000,
         price((1, 0, 0.0009)), 0,
         attrs({"Resistance": "4.7Ω", "Tolerance": "±1%", "Power": "62.5mW"}),
         0, 0, "{}"),

        # --- inductor whose shielding is ONLY in the description (no
        #     Shielding attribute) — exercises the from_description fallback ---
        (104, 3, "MSSI-4R7", "SMD,3x3x1.5mm", 2, 2, 0, 0,
         "SMD Power Inductor 4.7uH Shielded 3A", "https://ds/mssi.pdf", 60000,
         price((1, 0, 0.05)), 0,
         attrs({"Inductance": "4.7uH", "Rated Current": "3A",
                "Saturation Current (Isat)": "3.5A", "DC Resistance": "40mΩ"}),
         0, 0, "{}"),  # note: NO Shielding attribute at all

        # --- crystals: ppm tolerance/stability; one SMD (kept), one DIP (excluded) ---
        (401, 6, "X322516MSB4SI", "SMD3225-4P", 4, 5, 1, 0,
         "16MHz 10ppm 20pF SMD crystal", "https://ds/yxc.pdf", 300000,
         price((1, 0, 0.08)), 0,
         attrs({"Frequency": "16MHz", "Load Capacitance": "20pF",
                "Frequency Tolerance": "±10ppm", "Frequency Stability": "±30ppm",
                "ESR": "40Ω"}), 0, 0, "{}"),
        (402, 7, "HC49U-8MHZ", "HC-49U", 2, 5, 0, 0,
         "8MHz through-hole crystal (should be EXCLUDED)", "https://ds/hc49u.pdf", 9000,
         price((1, 0, 0.10)), 0,
         attrs({"Frequency": "8MHz", "Frequency Tolerance": "±20ppm"}), 0, 0, "{}"),

        # --- MOSFET with hyphenated real keys incl. single-s (Vgs) operating max ---
        (501, 8, "BSS138", "SOT-23-3", 3, 6, 1, 0,
         "N-Channel 50V 220mA MOSFET", "https://ds/bss138.pdf", 700000,
         price((1, 0, 0.02)), 0,
         attrs({"Drain-Source Voltage (Vdss)": "50V",
                "Continuous Drain Current (Id)": "220mA",
                "Drain-Source On Resistance (RDS(on))": "3.5Ω @ 10V",
                "Gate Threshold Voltage (Vgs(th))": "1.5V",
                "Gate-Source Voltage(Vgs)": "±20V",
                "Type": "N-Channel"}), 0, 0, "{}"),
    ]
    c.executemany(
        "INSERT INTO components VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", parts)
    conn.commit()
    conn.close()
    print(f"Test DB written: {path} ({len(parts)} parts, 5 categories)")


if __name__ == "__main__":
    make(sys.argv[1] if len(sys.argv) > 1 else "/tmp/test-source.sqlite3")
