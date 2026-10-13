"""SQLite storage. The price history in here is the asset — it is what lets
you say 'this is 22% under the market' instead of 'this feels cheap'."""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Optional

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    url           TEXT,
    title         TEXT,
    body          TEXT,
    price_chf     INTEGER,
    km            INTEGER,
    year          INTEGER,
    make          TEXT,
    model         TEXT,
    model_key     TEXT,
    fuel          TEXT,
    gearbox       TEXT,
    seller_type   TEXT,
    region        TEXT,
    photo_count   INTEGER,
    published_at  TEXT,
    seller_tier   TEXT,
    ext_source    TEXT,
    mfk_date      TEXT,
    inspected     INTEGER,
    had_accident  INTEGER,
    band          TEXT,
    enriched_at   TEXT,
    seen_sweep    INTEGER,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    sold_guess    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY,
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    observed_at TEXT NOT NULL,
    price_chf   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    mode        TEXT NOT NULL,
    score       REAL NOT NULL,
    reasons     TEXT,
    sent_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

"""


# Sources whose listings are market data. Pasted and manual entries are NOT:
# they are cars chosen because they looked cheap, so letting them into the
# comparables drags every baseline down and makes future finds look like
# smaller bargains than they are. They are subjects, never comparables.
MARKET_SOURCES = ("autoscout24", "tutti", "anibis")
_MARKET_SQL = "source IN ('autoscout24','tutti','anibis')"

# The one definition of "trader". Sources spell it differently -- tutti says
# "dealer", AutoScout24 says "professional" -- and three separate lists of it
# had already drifted apart.
DEALER_TYPES = ("dealer", "professional", "commercial")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Indexes run AFTER migration. An index on a column that an older database
# doesn't have yet would otherwise crash connect() on upgrade -- caught by
# tests/test_core.py on its first run.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_listings_modelkey ON listings(model_key, year);
CREATE INDEX IF NOT EXISTS idx_listings_active   ON listings(active, last_seen);
CREATE INDEX IF NOT EXISTS idx_hist_listing      ON price_history(listing_id);
CREATE INDEX IF NOT EXISTS idx_listings_band     ON listings(source, band, active);
"""

def _declared_columns(schema: str) -> dict[str, list[tuple[str, str]]]:
    """Parse CREATE TABLE statements into {table: [(column, type), ...]}.

    Migrations are derived from the schema itself rather than a hand-kept
    list. A hand-kept list had already drifted: the test suite's first run
    found columns the list didn't cover.
    """
    out: dict = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);",
                         schema, re.S):
        cols = []
        for raw in m.group(2).split("\n"):
            line = raw.strip().rstrip(",")
            if not line or line.upper().startswith(("UNIQUE", "PRIMARY",
                                                     "FOREIGN", "CHECK")):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[0].isidentifier():
                cols.append((parts[0], parts[1]))
        out[m.group(1)] = cols
    return out


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any column the schema declares that the database lacks.

    Added as nullable: SQLite's ALTER TABLE ADD COLUMN cannot add a NOT NULL
    column without a default, and rows that predate a column genuinely have
    no value for it.
    """
    for table, cols in _declared_columns(SCHEMA).items():
        have = {r["name"] for r in
                conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def connect(path: str) -> sqlite3.Connection:
    # willomotors-index and willomotors-listen are separate processes writing this file.
    # WAL lets readers proceed while a write is in progress, and the timeout
    # makes a writer wait for a lock instead of failing instantly with
    # "database is locked" when both happen at once.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
    from .ledger import ensure as _ensure_ledger
    _ensure_ledger(conn)
    return conn


def upsert(conn: sqlite3.Connection, li: Listing, band: str | None = None,
           sweep_id: int | None = None) -> tuple[int, bool, Optional[int]]:
    """Insert or update a listing.

    Returns (listing_id, is_new, previous_price).
    """
    ts = now()
    cur = conn.execute(
        "SELECT id, price_chf FROM listings WHERE source=? AND external_id=?",
        (li.source, li.external_id),
    )
    row = cur.fetchone()

    if row is None:
        cur = conn.execute(
            """INSERT INTO listings
               (source, external_id, url, title, body, price_chf, km, year,
                make, model, model_key, fuel, gearbox, seller_type, region,
                photo_count, published_at, seller_tier, ext_source,
                mfk_date, inspected, had_accident, band, seen_sweep,
                first_seen, last_seen, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (li.source, li.external_id, li.url, li.title, li.body, li.price_chf,
             li.km, li.year, li.make, li.model, li.model_key, li.fuel,
             li.gearbox, li.seller_type, li.region, li.photo_count,
             li.published_at or None, li.seller_tier or None,
             li.external_source or None, li.mfk_date or None,
             None if li.inspected is None else int(li.inspected),
             None if li.had_accident is None else int(li.had_accident),
             band, sweep_id, ts, ts),
        )
        lid, is_new, prev = cur.lastrowid, True, None
    else:
        lid, prev = row["id"], row["price_chf"]
        is_new = False
        # last_seen always updates -- it is what sold-detection reads. Only
        # the mutable fields are rewritten; unchanged prices write no history.
        conn.execute(
            """UPDATE listings SET price_chf=?, title=?, body=?, km=?,
                      photo_count=?, last_seen=?, active=1,
                      band=COALESCE(?, band),
                      seen_sweep=COALESCE(?, seen_sweep) WHERE id=?""",
            (li.price_chf, li.title, li.body, li.km, li.photo_count, ts,
             band, sweep_id, lid),
        )

    if li.price_chf is not None and li.price_chf != prev:
        conn.execute(
            "INSERT INTO price_history (listing_id, observed_at, price_chf) VALUES (?,?,?)",
            (lid, ts, li.price_chf),
        )

    conn.commit()
    return lid, is_new, prev


def next_sweep_id(conn) -> int:
    """Monotonic id for one band sweep.

    Used instead of comparing timestamps: last_seen has one-second resolution,
    so a sweep that finishes inside the same second as the previous one would
    silently expire nothing. A counter is exact regardless of how fast the
    sweep runs or what the clock does.
    """
    nxt = int(get_state(conn, "sweep_counter", 0) or 0) + 1
    set_state(conn, "sweep_counter", nxt)
    return nxt


def expire_band(conn, source: str, band: str, sweep_id: int) -> int:
    """Mark listings that were absent from a completed band sweep as sold.

    Scoped to one band deliberately. The old rule -- anything not seen for 3
    days is gone -- breaks the moment bands rotate on a multi-day cycle: a car
    sitting in a band you last swept on Tuesday looks exactly like a car that
    sold. Only a band we have *just re-read in full* can tell us what has
    disappeared from it.
    """
    cur = conn.execute(
        """UPDATE listings SET active=0, sold_guess=1
           WHERE source=? AND band=? AND active=1
             AND COALESCE(seen_sweep, -1) != ?""",
        (source, band, sweep_id))
    conn.commit()
    return cur.rowcount


def needs_enrichment(conn, source: str, external_id: str,
                     after_days: int = 30) -> bool:
    """Has this listing's detail page been read recently enough?

    An MFK date, an accident flag and a registration year do not change from
    week to week, so re-reading a detail page nightly is pure cost. Price and
    availability DO change, but those come from the search payload already.
    """
    row = conn.execute(
        "SELECT enriched_at FROM listings WHERE source=? AND external_id=?",
        (source, external_id)).fetchone()
    if not row or not row["enriched_at"]:
        return True
    age = conn.execute("SELECT julianday('now') - julianday(?) AS d",
                       (row["enriched_at"],)).fetchone()["d"]
    return age is None or age > after_days


def mark_enriched(conn, source: str, external_id: str) -> None:
    conn.execute(
        "UPDATE listings SET enriched_at=? WHERE source=? AND external_id=?",
        (now(), source, external_id))
    conn.commit()


def known_model_keys(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT model_key FROM listings "
        "WHERE model_key != '' ORDER BY model_key")]


def model_stats(conn, model_key: str, year_from=None, year_to=None) -> dict:
    """Everything /price needs about one model, in one pass."""
    where = ["model_key=?", "price_chf > 0", _MARKET_SQL]
    params: list = [model_key]
    if year_from:
        where.append("year >= ?"); params.append(year_from)
    if year_to:
        where.append("year <= ?"); params.append(year_to)
    clause = " AND ".join(where)

    marks = ",".join("?" * len(DEALER_TYPES))

    def bucket(extra: str, extra_params: list) -> dict:
        rows = conn.execute(
            f"""SELECT price_chf, last_seen, km, year FROM listings
                WHERE {clause} {extra} ORDER BY price_chf""",
            params + extra_params).fetchall()
        if not rows:
            return {"n": 0}
        prices = [r["price_chf"] for r in rows]
        return {
            "n": len(rows),
            "median": int(statistics.median(prices)),
            "low": rows[0]["price_chf"],
            "low_seen": (rows[0]["last_seen"] or "")[:10],
            "high": rows[-1]["price_chf"],
            "high_seen": (rows[-1]["last_seen"] or "")[:10],
        }

    return {
        "model_key": model_key,
        "total": conn.execute(
            f"SELECT COUNT(*) FROM listings WHERE {clause}", params).fetchone()[0],
        "private": bucket(
            f"AND active=1 AND COALESCE(seller_type,'') NOT IN ({marks})",
            list(DEALER_TYPES)),
        "dealer": bucket(
            f"AND active=1 AND COALESCE(seller_type,'') IN ({marks})",
            list(DEALER_TYPES)),
        "gone": bucket("AND sold_guess=1", []),
        # Which price bands this model has actually been seen in. If it only
        # appears in one, the median describes that slice of the market, not
        # the model -- and saying so matters more than the number does.
        "bands": [r[0] for r in conn.execute(
            f"SELECT DISTINCT band FROM listings WHERE {clause} "
            f"AND band IS NOT NULL ORDER BY band", params)],
        "last_seen": conn.execute(
            f"SELECT MAX(last_seen) FROM listings WHERE {clause}",
            params).fetchone()[0],
    }


def year_breakdown(conn, model_key: str, spans=((2004, 2009), (2010, 2013),
                                                (2014, 2017), (2018, 2030))):
    out = []
    for lo, hi in spans:
        rows = conn.execute(
            f"""SELECT price_chf, last_seen FROM listings
               WHERE model_key=? AND price_chf > 0 AND year BETWEEN ? AND ?
                 AND {_MARKET_SQL}
               ORDER BY price_chf""", (model_key, lo, hi)).fetchall()
        if rows:
            out.append({
                "span": f"{lo}–{hi}", "n": len(rows),
                "median": int(statistics.median([r["price_chf"] for r in rows])),
                "low": rows[0]["price_chf"],
                "low_seen": (rows[0]["last_seen"] or "")[:10],
            })
    return out


KM_BUCKETS = ((0, 100_000, "under 100k"), (100_000, 150_000, "100–150k"),
              (150_000, 200_000, "150–200k"), (200_000, 10**9, "over 200k"))


def km_breakdown(conn, model_key: str, year_from=None, year_to=None):
    """Price by mileage bucket.

    Shown as real buckets rather than a formula on purpose. The scoring code
    normalises comparables with a flat 4%-per-10'000km factor, which is a
    guess I made up; the buckets are what the market actually did. Where there
    is enough data, believe the buckets.
    """
    where = ["model_key=?", "price_chf > 0", "km IS NOT NULL", "km > 0",
             _MARKET_SQL]
    params: list = [model_key]
    if year_from:
        where.append("year >= ?"); params.append(year_from)
    if year_to:
        where.append("year <= ?"); params.append(year_to)
    clause = " AND ".join(where)

    out = []
    for lo, hi, label in KM_BUCKETS:
        rows = conn.execute(
            f"SELECT price_chf FROM listings WHERE {clause} AND km >= ? AND km < ?"
            f" ORDER BY price_chf", params + [lo, hi]).fetchall()
        if rows:
            prices = [r["price_chf"] for r in rows]
            out.append({"label": label, "lo": lo, "hi": hi, "n": len(rows),
                        "median": int(statistics.median(prices)),
                        "low": prices[0], "high": prices[-1]})
    return out


def km_bucket_for(km: int) -> str:
    for lo, hi, label in KM_BUCKETS:
        if lo <= km < hi:
            return label
    return ""


def listing_age_days(conn: sqlite3.Connection, listing_id: int) -> float:
    """Age of the ad. Uses the site's own publish timestamp when we have one --
    that is true age from the very first run, instead of waiting weeks for our
    own first_seen to become meaningful."""
    row = conn.execute(
        """SELECT julianday('now') - julianday(COALESCE(published_at, first_seen)) AS d
           FROM listings WHERE id=?""",
        (listing_id,),
    ).fetchone()
    return float(row["d"]) if row and row["d"] is not None else 0.0


def price_drops(conn: sqlite3.Connection, listing_id: int) -> int:
    rows = conn.execute(
        "SELECT price_chf FROM price_history WHERE listing_id=? ORDER BY observed_at",
        (listing_id,),
    ).fetchall()
    prices = [r["price_chf"] for r in rows]
    return sum(1 for a, b in zip(prices, prices[1:]) if b < a)


def baseline_price(conn: sqlite3.Connection, li: Listing, cfg: dict,
                   seller_scope: str = "private"):
    """Median price of comparable cars, km-adjusted.

    Returns (median_chf, n_comparables) or (None, 0) when there is not enough
    data yet. Early on this returns nothing — that is expected. Let the
    collector run for a few weeks before you trust the price signal.
    """
    pc = cfg.get("price", {})
    min_n = pc.get("min_comparables", 5)
    year_win = pc.get("year_window", 2)
    km_per_year = pc.get("km_depreciation_per_10k", 0.04)

    if not li.model_key:
        return None, 0

    sql = ["SELECT price_chf, km FROM listings",
           "WHERE model_key=? AND price_chf IS NOT NULL AND price_chf > 0",
           f"AND {_MARKET_SQL}"]
    params: list = [li.model_key]

    # Compare like with like. A garage's asking price is retail; it is not
    # what you can sell the same car for privately, and mixing the two pulls
    # the median up and makes every private car look like a bargain.
    marks = ",".join("?" * len(DEALER_TYPES))
    if seller_scope == "private":
        sql.append(f"AND COALESCE(seller_type,'') NOT IN ({marks})")
        params += list(DEALER_TYPES)
    elif seller_scope == "dealer":
        sql.append(f"AND COALESCE(seller_type,'') IN ({marks})")
        params += list(DEALER_TYPES)

    if li.year:
        sql.append("AND year BETWEEN ? AND ?")
        params += [li.year - year_win, li.year + year_win]

    rows = conn.execute(" ".join(sql), params).fetchall()
    comps = [r for r in rows if r["price_chf"]]
    if len(comps) < min_n:
        return None, len(comps)

    # Normalise every comparable to the subject car's mileage so a 90k km
    # example does not drag the median for a 195k km car.
    adjusted = []
    for r in comps:
        p = float(r["price_chf"])
        if li.km and r["km"]:
            # A comparable with LOWER mileage than the subject is worth more,
            # so normalising it to the subject's mileage must bring it DOWN.
            # delta_10k is negative in that case, so the factor is 1 + k*delta.
            # This was 1 - k*delta until 2026-09-23, which inflated every
            # baseline for high-mileage cars and made them look like bargains.
            delta_10k = (r["km"] - li.km) / 10_000.0
            p *= (1.0 + km_per_year * delta_10k)
        adjusted.append(max(p, 0.0))

    return int(statistics.median(adjusted)), len(adjusted)


def estimate(conn, cfg: dict, model_key: str, year=None, km=None) -> dict:
    """THE price estimate. Used by scoring, /price, /deal and the ledger.

    One function on purpose: there used to be three, and a pasted 2012 Golf
    at 200'000 km scored against CHF 12'024 while /price said 7'787 for the
    same car. Two answers from one bot is worse than either being wrong.

    Method is measured, not assumed. Instead of scaling comparables by a
    per-kilometre factor (a guess), it takes the actual median of cars in the
    same year window AND the same mileage bucket. Widens only when it must,
    and says so in `basis`.

    Returns {value, n, basis, confidence, private, dealer} where confidence
    is "high" (real private comparables), "medium" (dealer median x discount)
    or "none".
    """
    out = {"value": None, "n": 0, "basis": "", "confidence": "none",
           "private": None, "dealer": None}
    if not model_key:
        out["basis"] = "model not identified"
        return out

    win = int(cfg.get("price", {}).get("year_window", 3))
    disc = float(cfg.get("price", {}).get("retail_discount", 0.75))
    min_n = int(cfg.get("price", {}).get("min_comparables", 5))

    def medians(use_km: bool) -> tuple:
        where = ["model_key=?", "price_chf > 0", "active=1", _MARKET_SQL]
        params: list = [model_key]
        if year:
            where.append("year BETWEEN ? AND ?")
            params += [year - win, year + win]
        if use_km and km:
            lo, hi = next(((a, b) for a, b, _ in KM_BUCKETS if a <= km < b),
                          (0, 10**9))
            where.append("km >= ? AND km < ?")
            params += [lo, hi]
        clause = " AND ".join(where)
        marks = ",".join("?" * len(DEALER_TYPES))

        def med(extra: str):
            rows = [r[0] for r in conn.execute(
                f"SELECT price_chf FROM listings WHERE {clause} AND "
                f"COALESCE(seller_type,'') {extra} IN ({marks}) "
                f"ORDER BY price_chf", params + list(DEALER_TYPES))]
            return (int(statistics.median(rows)), len(rows), rows[0]) \
                if rows else (None, 0, None)

        return med("NOT"), med("")

    # Try the tightest match first, widen only if it is too thin.
    for use_km, label in ((True, "same year & mileage"), (False, "same years")):
        if use_km and not km:
            continue
        (pv, pn, plo), (dv, dn, _) = medians(use_km)
        out["private"], out["dealer"] = (pv, pn), (dv, dn)

        if pn >= min_n:
            out.update(value=pv, n=pn, confidence="high",
                       basis=f"{pn} private listings, {label}")
            return out

        if dn >= min_n:
            scaled = int(dv * disc)
            # Garage prices carry warranty and margin, so should sit ABOVE
            # private ones. If scaling the dealer median lands below the
            # cheapest private car, the two buckets hold different cars.
            if pn and plo and scaled < plo:
                out["basis"] = ("dealer and private listings don't line up "
                                f"({label})")
                continue
            out.update(value=scaled, n=dn, confidence="medium",
                       basis=f"dealer median × {disc}, {label}"
                             + (f", {pn} private to check against" if pn else ""))
            return out

    if not out["basis"]:
        p, d = out["private"] or (None, 0), out["dealer"] or (None, 0)
        out["basis"] = f"only {p[1]} private and {d[1]} dealer comparables"
    return out


def market_estimate(conn: sqlite3.Connection, li: Listing, cfg: dict):
    """Thin wrapper kept for callers that pass a Listing."""
    e = estimate(conn, cfg, li.model_key, li.year, li.km)
    basis = {"high": "private", "medium": "dealer-adjusted"}.get(
        e["confidence"], "")
    return e["value"], e["n"], basis


def baseline_coverage(conn) -> dict:
    """How usable is the price baseline yet? Counts model_keys that have
    enough private comparables to produce a median."""
    row = conn.execute(f"""
        SELECT COUNT(*) AS ready FROM (
            SELECT model_key FROM listings
            WHERE price_chf > 0 AND model_key != ''
              AND COALESCE(seller_type,'') NOT IN
                  ({",".join("?" * len(DEALER_TYPES))})
            GROUP BY model_key HAVING COUNT(*) >= 5)""",
        DEALER_TYPES).fetchone()
    any_row = conn.execute("""
        SELECT COUNT(*) AS ready FROM (
            SELECT model_key FROM listings
            WHERE price_chf > 0 AND model_key != ''
            GROUP BY model_key HAVING COUNT(*) >= 5)""").fetchone()
    total = conn.execute(
        "SELECT COUNT(DISTINCT model_key) FROM listings "
        "WHERE model_key != ''").fetchone()[0]
    return {"models_priced_private": row["ready"],
            "models_priced_any": any_row["ready"],
            "models_seen": total}


def record_alert(conn, listing_id: int, mode: str, score: float, reasons: list[str]):
    conn.execute(
        "INSERT INTO alerts (listing_id, mode, score, reasons, sent_at) VALUES (?,?,?,?,?)",
        (listing_id, mode, score, json.dumps(reasons, ensure_ascii=False), now()),
    )
    conn.commit()


def already_alerted(conn, listing_id: int, mode: str, cooloff_days: int = 14) -> bool:
    row = conn.execute(
        """SELECT 1 FROM alerts WHERE listing_id=? AND mode=?
             AND julianday('now') - julianday(sent_at) < ? LIMIT 1""",
        (listing_id, mode, cooloff_days),
    ).fetchone()
    return row is not None


def get_state(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value) -> None:
    conn.execute("INSERT INTO state (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def stats(conn) -> dict:
    g = lambda q: conn.execute(q).fetchone()[0]
    return {
        "listings_total": g("SELECT COUNT(*) FROM listings"),
        "listings_active": g("SELECT COUNT(*) FROM listings WHERE active=1"),
        "likely_sold": g("SELECT COUNT(*) FROM listings WHERE sold_guess=1"),
        "price_observations": g("SELECT COUNT(*) FROM price_history"),
        "alerts_sent": g("SELECT COUNT(*) FROM alerts"),
        "model_keys": g("SELECT COUNT(DISTINCT model_key) FROM listings"),
    }
