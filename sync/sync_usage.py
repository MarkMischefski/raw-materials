"""
SQL Server to Supabase Sync
============================
Syncs usage, transfers and stock reconciliation from SQL Server to Supabase.

Settings to configure:
  - SQL_SERVER, SQL_DATABASE, SQL_TABLE, STOCKTAKE_TABLE, STOCKTAKE_CTRL, STOCK_TRANS_TABLE
  - SQL_USER / SQL_PASSWORD (or leave blank for Windows auth)
  - WEEKS_BACK

Mappings are configured in the app:
  Settings > Branch Materials  (Stock Code field)
  Settings > Branch Locations  (Location No. field)

pip install pyodbc requests
"""

import sys
import pyodbc, requests, json, logging
from datetime import datetime, timedelta, date
from collections import defaultdict

# ── CONFIG ─────────────────────────────────────────────────────────────────────
SQL_SERVER   = "expol-akl-sql01"
SQL_DATABASE = "EXPOL"
SQL_TABLE           = "dbo.X_RAW_MATERIAL_TRANS"      # bag weight usage table
STOCKTAKE_TABLE     = "dbo.STOCKTAKE_TOTALS"    # stocktake counted quantities
STOCKTAKE_CTRL      = "dbo.STOCKTAKE_CTRL"      # stocktake initialisation times
STOCK_TRANS_TABLE   = "dbo.STOCK_TRANS"         # full transaction history
PURCHORD_LINES_TABLE = "dbo.PURCHORD_LINES"      # purchase order lines
SQL_USER     = "RawMaterialTracker"
SQL_PASSWORD = ""

SUPABASE_URL = "https://jrpysikxibrbbellroid.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpycHlzaWt4aWJyYmJlbGxyb2lkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA2MDM0OTksImV4cCI6MjA5NjE3OTQ5OX0.6ffi5GLkjs6qQ3Oqks8c1niu7-yrqrIG5P5aWWSFlN0"

WEEKS_BACK       = 8
TRANSIT_LOCATION = "10"   # in-transit — excluded from all calcs

# Locations removed from Settings > Branch Locations (so their branch is hidden in the
# app and not synced for usage/recon) but still needed to resolve transfers, so the
# receiving branch gets its incoming stock. EXO LOCNO -> app branch id.
TRANSFER_EXTRA_LOCATIONS = {
    "6": "christchurch",   # CHCH
    "9": "rolleston",      # ROLL
}

TRANSTYPE_RECEIPT   = 1
TRANSTYPE_USAGE     = 3
TRANSTYPE_ADJUST_IN = 4
TRANSTYPE_TRANSFER  = 2

# ── LOGGING ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("sync_usage.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── SQL SERVER ──────────────────────────────────────────────────────────────────
def get_conn():
    if SQL_USER:
        cs = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={SQL_SERVER};DATABASE={SQL_DATABASE};UID={SQL_USER};PWD={SQL_PASSWORD};"
    else:
        cs = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={SQL_SERVER};DATABASE={SQL_DATABASE};Trusted_Connection=yes;"
    return pyodbc.connect(cs)

def week_ending_sunday(d):
    """Return the Sunday on or after date d (the week-ending Sunday)."""
    days_ahead = (6 - d.weekday()) % 7  # 0 if already Sunday, up to 6 if Monday
    return d + timedelta(days=days_ahead)

# ── SUPABASE ────────────────────────────────────────────────────────────────────
HDR = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

def sb_get(path, params=""):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}?{params}", headers=HDR)
    r.raise_for_status()
    return r.json()

def sb_upsert(path, rows, conflict):
    h = {**HDR, "Prefer": "resolution=merge-duplicates"}
    total = 0
    for i in range(0, len(rows), 100):
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}?on_conflict={conflict}",
                          headers=h, data=json.dumps(rows[i:i+100]))
        if not r.ok: log.error(f"Upsert {path} failed: {r.status_code} {r.text[:200]}")
        else: total += min(100, len(rows)-i)
    return total

def sb_delete(path, filter_str):
    # Use prepared request to avoid URL encoding of filter operators
    from requests import Request, Session
    url = f"{SUPABASE_URL}/rest/v1/{path}?{filter_str}"
    req = Request("DELETE", url, headers=HDR)
    prepared = req.prepare()
    prepared.url = url  # override to prevent encoding
    s2 = Session()
    r = s2.send(prepared)
    log.info(f"DELETE {path}?{filter_str} -> {r.status_code}")
    return r.status_code

def sb_post(path, rows):
    h = {**HDR, "Prefer": "return=minimal"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=h, data=json.dumps(rows))
    if not r.ok: log.error(f"POST {path} failed: {r.status_code} {r.text[:200]}")
    return r.ok

# ── LOAD SUPABASE REFERENCE DATA ────────────────────────────────────────────────
def load_supabase():
    weeks       = sb_get("weeks",            "select=id,branch_id,week_number,week_ending")
    materials   = sb_get("materials",        "select=id,name,branch_id,stock_code,material_type_id")
    branch_locs = sb_get("branch_locations", "select=id,branch_id,location")
    return weeks, materials, branch_locs

def ensure_future_weeks(weeks, branch_locs, weeks_ahead=26):
    """
    Ensure each branch has week rows at least `weeks_ahead` weeks into the future.
    Creates new rows as needed, extending beyond week 52 and into subsequent years
    by continuing the week_number sequence.
    """
    today = date.today()
    target_end = week_ending_sunday(today + timedelta(weeks=weeks_ahead))

    # Get distinct branch_ids from branch_locs
    branch_ids = list({l["branch_id"] for l in branch_locs})

    # Build per-branch: latest week_ending and max week_number
    branch_state = {}
    for w in weeks:
        bid = w["branch_id"]
        we  = w["week_ending"][:10]
        wn  = w["week_number"]
        if bid not in branch_state:
            branch_state[bid] = {"max_ending": we, "max_number": wn}
        else:
            if we > branch_state[bid]["max_ending"]:
                branch_state[bid]["max_ending"]  = we
                branch_state[bid]["max_number"]   = wn

    new_rows = []
    for bid in branch_ids:
        if bid not in branch_state:
            log.warning(f"  Branch {bid} has no weeks at all — skipping future-week extension")
            continue
        st = branch_state[bid]
        cur_end = date.fromisoformat(st["max_ending"])
        cur_num = st["max_number"]

        while cur_end < target_end:
            cur_end += timedelta(weeks=1)
            cur_num += 1
            new_rows.append({
                "branch_id":   bid,
                "week_number": cur_num,
                "week_ending": cur_end.isoformat(),
            })

    if new_rows:
        log.info(f"Creating {len(new_rows)} future week rows across {len(branch_ids)} branches...")
        sb_post("weeks", new_rows)
        log.info("Future weeks created OK")
        # Reload weeks so rest of sync uses updated list
        return sb_get("weeks", "select=id,branch_id,week_number,week_ending")
    else:
        log.info("Future weeks OK — no new rows needed")
        return weeks

# ── USAGE SYNC ──────────────────────────────────────────────────────────────────
def sync_usage(conn, weeks, materials, branch_locs, from_date):
    log.info("--- Syncing usage ---")
    q = f"""
        SET DATEFIRST 1;
        SELECT CAST(LOCATION AS VARCHAR(50)), STOCKCODE,
            CAST(DATEADD(day, 7 - DATEPART(weekday, TRANSDATE), CAST(TRANSDATE AS DATE)) AS DATE),
            SUM(CAST(BAGWEIGHT AS FLOAT)), COUNT(*)
        FROM {SQL_TABLE}
        WHERE TRANSDATE >= ? AND BAGWEIGHT > 0 AND STOCKCODE IS NOT NULL
        GROUP BY CAST(LOCATION AS VARCHAR(50)), STOCKCODE,
            CAST(DATEADD(day, 7 - DATEPART(weekday, TRANSDATE), CAST(TRANSDATE AS DATE)) AS DATE)
        ORDER BY 3, 1, 2
    """
    cur = conn.cursor(); cur.execute(q, from_date); sql_rows = cur.fetchall()
    log.info(f"SQL returned {len(sql_rows)} usage rows")
    if sql_rows:
        log.info(f"Week endings: {sorted(set(str(r[2]) for r in sql_rows))}")
        log.info(f"Locations: {sorted(set(str(r[0]) for r in sql_rows))}")
        log.info(f"Stock codes: {sorted(set(str(r[1]) for r in sql_rows))}")

    week_lookup = {(w["branch_id"], w["week_ending"]): w["id"] for w in weeks}
    mat_lookup  = {(m["branch_id"], m["stock_code"]): m["id"] for m in materials if m.get("stock_code")}
    loc_lookup  = {l["location"]: l["branch_id"] for l in branch_locs}

    rows, skipped = [], []
    for location, stock_code, week_end, total_kg, _ in sql_rows:
        location = str(location).strip(); stock_code = str(stock_code).strip()
        week_end = str(week_end)
        branch_id = loc_lookup.get(location)
        if not branch_id: skipped.append(f"LOCATION='{location}' not mapped"); continue
        mat_id = mat_lookup.get((branch_id, stock_code))
        if not mat_id: skipped.append(f"STOCKCODE='{stock_code}' not mapped for '{branch_id}'"); continue
        week_id = week_lookup.get((branch_id, week_end))
        if not week_id: skipped.append(f"No week for {branch_id} ending {week_end}"); continue
        rows.append({"week_id": week_id, "material_id": mat_id, "actual_qty": round(float(total_kg))})

    log.info(f"Mapped {len(rows)} usage rows, skipped {len(set(skipped))}")
    for s in sorted(set(skipped)): log.warning(f"  SKIP: {s}")
    if rows:
        total = sb_upsert("usage", rows, "week_id,material_id")
        log.info(f"Upserted {total} usage rows")

# ── TRANSFER SYNC ───────────────────────────────────────────────────────────────
def sync_transfers(conn, weeks, materials, branch_locs, from_date):
    """
    Transfers travel through transit (loc 10): source -> transit -> destination.
    Pattern in STOCK_TRANS (same REF1 reference groups the legs):
      Leg 1: LOCATION=source,  TOLOCATION=10,   QUANTITY=-8000  (departs source)
      Leg 2: LOCATION=10,      TOLOCATION=dest, QUANTITY=-8000  (departs transit)

    Strategy: find leg 1 (source != transit, tolocation == transit)
    then find the matching leg 2 (location == transit, same REF1, same STOCKCODE)
    to get the final destination. Produces one transfer: source -> destination.

    Direct transfers (no transit) are also picked up:
      LOCATION=source, TOLOCATION=dest, QUANTITY=-3200, REF1 usually 'TRANSFER'
      (paired with LOCATION=dest, TOLOCATION=0, +3200, ignored)
    """
    log.info("--- Syncing ERP transfers ---")
    loc_lookup  = {**TRANSFER_EXTRA_LOCATIONS,
                   **{l["location"]: l["branch_id"] for l in branch_locs
                      if l["location"] != TRANSIT_LOCATION}}
    mat_lookup  = {(m["branch_id"], m["stock_code"]): m for m in materials if m.get("stock_code")}
    week_lookup = {(w["branch_id"], w["week_ending"]): w for w in weeks}

    # Get leg 1: departures from real locations into transit
    q1 = f"""
        SET DATEFIRST 1;
        SELECT t1.SEQNO, t1.TRANSDATE, t1.STOCKCODE, t1.REF1,
               CAST(t1.LOCATION   AS VARCHAR(50)) AS from_loc,
               ABS(CAST(t1.QUANTITY AS FLOAT))    AS qty
        FROM {STOCK_TRANS_TABLE} t1
        WHERE t1.TRANSTYPE = {TRANSTYPE_TRANSFER}
          AND t1.TRANSDATE >= ?
          AND t1.QUANTITY < 0
          AND CAST(t1.LOCATION   AS VARCHAR(50)) NOT IN ('0', '', ?)
          AND CAST(t1.TOLOCATION AS VARCHAR(50)) = ?
          AND t1.REF1 IS NOT NULL AND t1.REF1 != ''
        ORDER BY t1.TRANSDATE, t1.REF1
    """
    cur = conn.cursor()
    cur.execute(q1, from_date, TRANSIT_LOCATION, TRANSIT_LOCATION)
    leg1_rows = cur.fetchall()
    log.info(f"Found {len(leg1_rows)} outbound legs (source -> transit)")

    # Get leg 2: departures from transit to real locations
    q2 = f"""
        SET DATEFIRST 1;
        SELECT t2.REF1, t2.STOCKCODE,
               CAST(t2.TOLOCATION AS VARCHAR(50)) AS to_loc,
               MIN(t2.POSTTIME) AS post_time
        FROM {STOCK_TRANS_TABLE} t2
        WHERE t2.TRANSTYPE = {TRANSTYPE_TRANSFER}
          AND t2.TRANSDATE >= ?
          AND t2.QUANTITY < 0
          AND CAST(t2.LOCATION   AS VARCHAR(50)) = ?
          AND CAST(t2.TOLOCATION AS VARCHAR(50)) NOT IN ('0', '', ?)
          AND t2.REF1 IS NOT NULL AND t2.REF1 != ''
        GROUP BY t2.REF1, t2.STOCKCODE, CAST(t2.TOLOCATION AS VARCHAR(50))
    """
    cur.execute(q2, from_date, TRANSIT_LOCATION, TRANSIT_LOCATION)
    # Build lookup: (ref1, stockcode) -> to_loc
    leg2_lookup = {(str(r[0]).strip(), str(r[1]).strip()): str(r[2]).strip()
                   for r in cur.fetchall()}
    log.info(f"Found {len(leg2_lookup)} inbound legs (transit -> destination)")
    log.info(f"Leg2 sample: {list(leg2_lookup.items())[:5]}")

    # Direct transfers: posted straight from source to destination without transit
    q3 = f"""
        SELECT t.SEQNO, t.TRANSDATE, t.STOCKCODE, t.REF1,
               CAST(t.LOCATION   AS VARCHAR(50)) AS from_loc,
               CAST(t.TOLOCATION AS VARCHAR(50)) AS to_loc,
               ABS(CAST(t.QUANTITY AS FLOAT))    AS qty
        FROM {STOCK_TRANS_TABLE} t
        WHERE t.TRANSTYPE = {TRANSTYPE_TRANSFER}
          AND t.TRANSDATE >= ?
          AND t.QUANTITY < 0
          AND CAST(t.LOCATION   AS VARCHAR(50)) NOT IN ('0', '', ?)
          AND CAST(t.TOLOCATION AS VARCHAR(50)) NOT IN ('0', '', ?)
        ORDER BY t.TRANSDATE, t.SEQNO
    """
    cur.execute(q3, from_date, TRANSIT_LOCATION, TRANSIT_LOCATION)
    direct_rows = cur.fetchall()
    log.info(f"Found {len(direct_rows)} direct transfers (source -> destination, no transit)")

    # Normalise both kinds to (seqno, transdate, stock_code, ref1, from_loc, to_loc, qty)
    candidates, skipped = [], []
    for seqno, transdate, stock_code, ref1, from_loc, qty in leg1_rows:
        ref1, stock_code = str(ref1).strip(), str(stock_code).strip()
        # Find destination from leg 2
        to_loc = leg2_lookup.get((ref1, stock_code))
        if not to_loc:
            skipped.append(f"No destination leg for REF1={ref1} {stock_code}"); continue
        candidates.append((seqno, transdate, stock_code, ref1, from_loc, to_loc, qty))
    for seqno, transdate, stock_code, ref1, from_loc, to_loc, qty in direct_rows:
        candidates.append((seqno, transdate, str(stock_code).strip(), str(ref1 or "").strip(),
                           from_loc, to_loc, qty))

    rows = []
    for seqno, transdate, stock_code, ref1, from_loc, to_loc, qty in candidates:
        from_loc = str(from_loc).strip()
        to_loc   = str(to_loc).strip()
        qty      = round(float(qty))

        from_branch = loc_lookup.get(from_loc)
        to_branch   = loc_lookup.get(to_loc)
        if not from_branch or not to_branch:
            skipped.append(f"Unmapped loc {from_loc}->{to_loc} ({stock_code})"); continue

        from_mat = mat_lookup.get((from_branch, stock_code))
        to_mat   = mat_lookup.get((to_branch,   stock_code))
        if not from_mat or not to_mat:
            skipped.append(f"Unmapped stock {stock_code} {from_branch}->{to_branch}"); continue

        td = transdate.date() if hasattr(transdate, "date") else transdate
        we = week_ending_sunday(td).isoformat()
        fw = week_lookup.get((from_branch, we))
        week_number = fw["week_number"] if fw else None

        rows.append({
            "from_branch_id":   from_branch, "to_branch_id":   to_branch,
            "from_material_id": from_mat["id"], "to_material_id": to_mat["id"],
            "week_number": week_number, "qty": qty,
            # Direct transfers all share REF1='TRANSFER', so add SEQNO to tell them apart
            "note": f"ERP transfer: {qty:,} kg {stock_code} wk{week_number or '?'} ref {ref1}"
                    + (f" #{seqno}" if ref1.upper() in ("", "TRANSFER") else "")
        })

    for sk in sorted(set(skipped))[:10]: log.warning(f"  SKIP: {sk}")
    log.info(f"Mapped {len(rows)} transfer rows, skipped {len(set(skipped))}")
    if rows:
        log.info(f"Sample: {rows[:2]}")
        # Only replace ERP transfers inside the window this run re-imports, so older
        # transfers stay in the app (weeks are numbered the same across branches).
        fd = from_date.date() if hasattr(from_date, "date") else from_date
        first_wk = next((w["week_number"] for w in weeks
                         if w["week_ending"][:10] == week_ending_sunday(fd).isoformat()), None)
        if first_wk is not None:
            del_filter = f"note=like.ERP transfer%&or=(week_number.gte.{first_wk},week_number.is.null)"
        else:
            log.warning("  No week found for window start — replacing all ERP transfers")
            del_filter = "note=like.ERP transfer%"
        del_status = sb_delete("transfers", del_filter)
        log.info(f"Deleted old ERP transfers: HTTP {del_status}")
        ok = sb_post("transfers", rows)
        log.info(f"Posted {len(rows)} ERP transfers: ok={ok}")
    else:
        log.warning("No transfers mapped. Check branch locations include both source and destination.")

# ── STOCK RECONCILIATION ────────────────────────────────────────────────────────
def get_stocktake_base(conn, location, recon_week_end):
    """
    Get most recent stocktake time, counted qtys and system qtys for a location.
    Returns: (init_time, {stockcode: counted_qty}, {stockcode: system_qty})
    system_qty = what ERP had before the stocktake count
    counted_qty = what was physically counted
    variance = counted - system (posted as a TRANSTYPE=1 REF1=STOCKTAKE adjustment)
    """
    cur = conn.cursor()
    # Only use stocktakes that were initialised ON OR BEFORE the recon week end.
    # A stocktake entered mid-week (e.g. June 30) must not be used when reconciling
    # the previous completed week (e.g. ending June 22) — the app stock at end of
    # that week was calculated without it, and it includes usage from the new week.
    cur.execute(f"""
        SELECT TOP 1 TIME_INITIALISED FROM {STOCKTAKE_CTRL}
        WHERE CAST(LOCNO AS VARCHAR) = ?
          AND TIME_INITIALISED > '2020-01-01'
          AND CAST(TIME_INITIALISED AS DATE) <= ?
        ORDER BY TIME_INITIALISED DESC
    """, str(location), recon_week_end)
    row = cur.fetchone()
    if not row or not row[0]: return None, {}, {}
    init_time = row[0]
    counts = {}; system_qtys = {}
    try:
        # Try named columns first
        cur.execute(f"SELECT STOCKCODE, COUNTED_QTY, SYSTEM_QTY FROM {STOCKTAKE_TABLE} WHERE CAST(LOCATION AS VARCHAR) = ?", str(location))
        for r in cur.fetchall():
            if r[1] is not None:
                counts[str(r[0]).strip()]      = int(r[1] or 0)
                system_qtys[str(r[0]).strip()] = int(r[2] or 0)
    except Exception:
        # Fallback: positional — col 5=system_qty, col 6=counted_qty
        cur.execute(f"SELECT * FROM {STOCKTAKE_TABLE} WHERE CAST(LOCATION AS VARCHAR) = ?", str(location))
        for r in cur.fetchall():
            if len(r) > 6:
                code = str(r[1]).strip()
                system_qtys[code] = int(r[5] or 0) if r[5] is not None else 0
                counts[code]      = int(r[6] or 0) if r[6] is not None else 0
    return init_time, counts, system_qtys

def get_post_stocktake_txns(conn, location, stock_code, since_dt, week_end):
    """
    Sum all transactions for a stock code at a location after the stocktake
    snapshot (since_dt = TIME_INITIALISED), capped at week_end.

    Uses POSTTIME (not TRANSDATE) so that transactions posted after the
    stocktake initialisation are correctly included regardless of their
    transaction date.

    Transfers: LOCATION column holds both sides — negative qty = outgoing,
    positive qty = incoming. No TOLOCATION lookup needed.
    """
    cur = conn.cursor()
    week_end_dt = f"{week_end} 23:59:59"
    loc = str(location)

    # All transactions at this location after stocktake, capped at week end
    # Use POSTTIME for both bounds (matches stocktake TIME_INITIALISED)
    # POSTTIME > since_dt: ensures we only pick up transactions posted AFTER
    # the stocktake snapshot (correct lower bound — stocktake already includes earlier stock).
    # TRANSDATE <= week_end: uses the economic date for the upper bound so that
    # backdated adjustments (e.g. entered Monday but dated last Friday) land in
    # the correct week rather than being missed entirely.
    cur.execute(f"""
        SELECT TRANSTYPE, REF1, SUM(CAST(QUANTITY AS FLOAT))
        FROM {STOCK_TRANS_TABLE}
        WHERE STOCKCODE = ?
          AND CAST(LOCATION AS VARCHAR) = ?
          AND POSTTIME > ?
          AND CAST(TRANSDATE AS DATE) <= ?
        GROUP BY TRANSTYPE, REF1
    """, stock_code, loc, since_dt, week_end)

    totals = {"receipt": 0, "usage": 0, "adjust_in": 0, "adjust_out": 0, "xfer_in": 0, "xfer_out": 0}
    for transtype, ref1, qty in cur.fetchall():
        qty = round(float(qty or 0))
        ref1 = (ref1 or "").strip().upper()
        if transtype == TRANSTYPE_RECEIPT and ref1 == "STOCKTAKE":
            pass  # stocktake variance already in base qty
        elif transtype == TRANSTYPE_RECEIPT:
            totals["receipt"] += qty                    # positive
        elif transtype == TRANSTYPE_USAGE:
            totals["usage"] += qty                      # negative
        elif transtype == TRANSTYPE_ADJUST_IN:
            totals["adjust_in"] += qty                  # positive
        elif transtype == TRANSTYPE_TRANSFER:
            if qty >= 0: totals["xfer_in"]  += qty      # positive = incoming
            else:        totals["xfer_out"] += qty      # negative = outgoing
        else:
            totals["adjust_out"] += qty                 # catch-all for other types

    log.info(f"    {stock_code} loc {loc}: {totals}")
    return totals


# ── STOCKTAKE HISTORY ────────────────────────────────────────────────────────────
def save_stocktake_snapshot(branch_id, location, stock_code, init_time, counted_qty, system_qty):
    """
    Save a stocktake snapshot to Supabase stocktake_history if not already stored.
    Uses UNIQUE(branch_id, location, stock_code, stocktake_date) to avoid duplicates.
    """
    stocktake_date = (init_time.date().isoformat()
                      if hasattr(init_time, "date") else str(init_time)[:10])
    row = {
        "branch_id":     branch_id,
        "location":      str(location),
        "stock_code":    stock_code,
        "stocktake_date": stocktake_date,
        "init_time":     init_time.isoformat() if hasattr(init_time, "isoformat") else str(init_time),
        "counted_qty":   int(counted_qty or 0),
        "system_qty":    int(system_qty or 0),
    }
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stocktake_history",
            headers={**HDR, "Prefer": "resolution=ignore-duplicates"},
            data=json.dumps(row)
        )
        if r.ok:
            log.info(f"  Stocktake saved: {branch_id} loc {location} {stock_code} "
                     f"{stocktake_date} counted={counted_qty:,} system={system_qty:,}")
        elif r.status_code == 409 or "duplicate" in r.text.lower():
            pass  # already stored, fine
        else:
            log.warning(f"  Stocktake save failed: {r.status_code} {r.text[:80]}")
    except Exception as e:
        log.error(f"  Stocktake save error: {e}")

def sync_reconciliation(conn, weeks, materials, branch_locs, orders, order_lines):
    """
    Single-week ERP stock reconciliation for the last completed week (recon_week_end).
    Uses stocktake_history in Supabase as the base (preserving historical stocktakes
    even after ERP overwrites them with a newer count).
    """
    log.info("--- Syncing stock reconciliation ---")
    today = date.today()
    recon_week_end = week_ending_sunday(today - timedelta(days=7)).isoformat()
    cur_week_end   = recon_week_end
    log.info(f"Reconciling transactions up to week ending: {cur_week_end}")

    usage_rows = sb_get("usage", "select=id,week_id,material_id,actual_qty,opening_stock,closing_stock")
    usage_lu   = {(u["week_id"], u["material_id"]): u for u in usage_rows}
    week_lu    = {(w["branch_id"], w["week_ending"]): w for w in weeks}

    # Load stored stocktake history from Supabase
    try:
        stk_history = sb_get("stocktake_history",
                             "select=*&order=branch_id,location,stock_code,stocktake_date.desc")
        log.info(f"Loaded {len(stk_history)} stored stocktake snapshots")
    except Exception as e:
        log.error(f"Failed to load stocktake_history: {e}")
        stk_history = []

    # Build lookup: (branch_id, location, stock_code) -> most recent snapshot <= recon_week_end
    stk_lu = {}
    for row in stk_history:
        if row["stocktake_date"] <= cur_week_end:
            key = (row["branch_id"], row["location"], row["stock_code"])
            if key not in stk_lu:  # already sorted desc so first = most recent
                stk_lu[key] = row

    # Build branch -> location list
    branch_loc_map = {}
    for bl in branch_locs:
        branch_loc_map.setdefault(bl["branch_id"], []).append(bl["location"])

    recon_rows   = []
    closing_rows = []
    today_str    = today.isoformat()

    # Also still save any new ERP stocktakes to history
    for branch_id, locations in branch_loc_map.items():
        branch_mats = [m for m in materials
                       if m["branch_id"] == branch_id and m.get("stock_code")]
        for mat in branch_mats:
            stock_code = mat["stock_code"].strip()
            for loc in locations:
                init_t, counts, sys_qtys = get_stocktake_base(conn, loc, cur_week_end)
                if not init_t:
                    continue
                save_stocktake_snapshot(branch_id, loc, stock_code,
                                        init_t, counts.get(stock_code, 0),
                                        sys_qtys.get(stock_code, 0))

    # Now reload history after saving new snapshots
    try:
        stk_history = sb_get("stocktake_history",
                             "select=*&order=branch_id,location,stock_code,stocktake_date.desc")
        stk_lu = {}
        for row in stk_history:
            if row["stocktake_date"] <= cur_week_end:
                key = (row["branch_id"], row["location"], row["stock_code"])
                if key not in stk_lu:
                    stk_lu[key] = row
        log.info(f"Reloaded {len(stk_history)} stocktake snapshots after save")
    except Exception as e:
        log.warning(f"Failed to reload stocktake_history: {e}")

    for branch_id, locations in branch_loc_map.items():
        branch_mats = [m for m in materials
                       if m["branch_id"] == branch_id and m.get("stock_code")]

        for mat in branch_mats:
            stock_code = mat["stock_code"].strip()
            mat_id     = mat["id"]

            # Aggregate stored stocktake across all locations for this branch+material
            base_qty = 0; latest_init = None; stocktake_date = None
            stk_variance_total = 0
            txns = {"receipt": 0, "usage": 0, "adjust_in": 0,
                    "adjust_out": 0, "xfer_in": 0, "xfer_out": 0}

            for loc in locations:
                key = (branch_id, loc, stock_code)
                snap = stk_lu.get(key)
                if not snap:
                    log.debug(f"  No stored stocktake for {branch_id} loc {loc} {stock_code}")
                    continue

                loc_base    = snap["counted_qty"]
                loc_sys_qty = snap["system_qty"]
                loc_stk_var = loc_base - loc_sys_qty
                base_qty           += loc_base
                stk_variance_total += loc_stk_var

                init_t_str = snap["init_time"]
                # Parse init_time for post-stocktake txn query
                try:
                    from datetime import datetime as dt
                    init_t = dt.fromisoformat(init_t_str.replace("Z", "+00:00"))
                except Exception:
                    init_t = init_t_str

                log.info(f"  Loc {loc}: stocktake={snap['stocktake_date']} "
                         f"system={loc_sys_qty:,} counted={loc_base:,} stk_var={loc_stk_var:+,}")

                if latest_init is None or snap["stocktake_date"] > (stocktake_date or ""):
                    latest_init    = init_t
                    stocktake_date = snap["stocktake_date"]

                # Sum post-stocktake transactions to recon_week_end
                try:
                    t = get_post_stocktake_txns(conn, loc, stock_code, init_t, cur_week_end)
                    log.info(f"  Loc {loc} post-stocktake txns: {t}")
                    for k in txns:
                        txns[k] += t[k]
                except Exception as e:
                    log.error(f"  Txn query failed {stock_code} loc {loc}: {e}")

            if latest_init is None:
                log.warning(f"  No stored stocktake for {branch_id} {mat['name']} — skipping")
                continue

            # base_qty = counted_qty (the physical count) — this IS the correct base.
            # stk_variance_total (counted - system) is NOT added here because:
            # 1. The counted qty already IS the truth
            # 2. ERP posts a TRANSTYPE=1 REF1=STOCKTAKE adjustment for this diff,
            #    which is already excluded from get_post_stocktake_txns
            erp_stock = round(base_qty +
                              txns["receipt"] + txns["usage"] +
                              txns["adjust_in"] + txns["adjust_out"] +
                              txns["xfer_in"]   + txns["xfer_out"])

            # App stock for recon week
            cur_wk  = week_lu.get((branch_id, recon_week_end))
            week_id = cur_wk["id"] if cur_wk else None
            app_stock = None
            if week_id:
                u = usage_lu.get((week_id, mat_id))
                if u:
                    app_stock = u.get("closing_stock")
                    if app_stock is None:
                        op = u.get("opening_stock")
                        if op is not None:
                            prior_wk_end = (date.fromisoformat(recon_week_end)
                                            - timedelta(days=7)).isoformat()
                            branch_ord_ids = {o["id"] for o in orders
                                              if o["branch_id"] == branch_id
                                              and not o.get("is_forecast")}
                            arrived = sum(
                                (l.get("qty") or 0) for l in order_lines
                                if l.get("material_id") == mat_id
                                and l.get("order_id") in branch_ord_ids
                                and l.get("eta")
                                and prior_wk_end < str(l["eta"])[:10] <= recon_week_end
                            )
                            app_stock = op - (u.get("actual_qty") or 0) + arrived

            variance = (erp_stock - app_stock) if app_stock is not None else None
            log.info(f"  {branch_id} {mat['name']} wk {recon_week_end}: "
                     f"ERP={erp_stock:,} app={app_stock} var={variance}")

            # Build clear notes
            parts = [f"Stocktake {stocktake_date}: {base_qty:,} kg"]
            # System variance is informational only - not added to stock calc
            if stk_variance_total != 0:
                parts.append(f"Count vs system at stocktake: {stk_variance_total:+,} kg (info only)")
            if txns["receipt"]:
                parts.append(f"Receipts: {txns['receipt']:+,} kg")
            if txns["usage"]:
                parts.append(f"Usage: {txns['usage']:,} kg")
            if txns["xfer_in"]:
                parts.append(f"Transfer in: {txns['xfer_in']:+,} kg")
            if txns["xfer_out"]:
                parts.append(f"Transfer out: {txns['xfer_out']:,} kg")
            if txns["adjust_in"] or txns["adjust_out"]:
                parts.append(f"Adjustments: {txns['adjust_in']+txns['adjust_out']:+,} kg")
            parts.append(f"= ERP: {erp_stock:,} kg")
            if app_stock is not None:
                parts.append(f"App: {app_stock:,} kg")
                parts.append(f"Variance: {variance:+,} kg")
            note = " | ".join(parts)

            if week_id is None:
                log.warning(f"  No week_id for {branch_id} wk {recon_week_end} — skipping")
                continue

            recon_rows.append({
                "branch_id":        branch_id,
                "material_id":      mat_id,
                "week_id":          week_id,
                "sync_date":        today_str,
                "stocktake_date":   stocktake_date,
                "stocktake_qty":    base_qty,
                "erp_stock":        erp_stock,
                "app_stock":        app_stock,
                "variance":         variance,
                "usage_qty":        txns["usage"],
                "receipt_qty":      txns["receipt"],
                "transfer_in_qty":  txns["xfer_in"],
                "transfer_out_qty": txns["xfer_out"],
                "adjust_in_qty":    txns["adjust_in"],
                "adjust_out_qty":   txns["adjust_out"],
                "notes":            note,
            })

            # Write ERP stock as erp_anchor on the recon week's usage row.
            # This is used by the app's opening_stock cascade as a reference but
            # does NOT override closing_stock (which would wipe out receipts).
            # Instead the app will show a variance indicator when erp_anchor differs
            # from computed stock, helping identify discrepancies.
            if erp_stock >= 0 and stocktake_date is not None and week_id:
                closing_rows.append({
                    "week_id":     week_id,
                    "material_id": mat_id,
                    "erp_anchor":  erp_stock
                })

    log.info(f"Total recon_rows built: {len(recon_rows)}")

    if recon_rows:
        log.info(f"Saving {len(recon_rows)} recon records...")
        try:
            existing = sb_get("stock_reconciliation",
                              "select=id,branch_id,material_id,notes,variance")
            exist_map = {(r["branch_id"], r["material_id"]): r for r in existing}
        except Exception as e:
            log.warning(f"Could not load existing recon: {e}")
            exist_map = {}

        for row in recon_rows:
            key = (row["branch_id"], row["material_id"])
            existing_rec = exist_map.get(key)
            if row.get("variance") == 0 and existing_rec and existing_rec.get("notes"):
                old_notes = existing_rec["notes"]
                if not old_notes.startswith("Resolved"):
                    row["notes"] = f"Resolved {today_str} | " + old_notes
            sb_delete("stock_reconciliation",
                      f"branch_id=eq.{row['branch_id']}&material_id=eq.{row['material_id']}")

        sb_post("stock_reconciliation", recon_rows)
        log.info(f"Saved {len(recon_rows)} recon records")

    if closing_rows:
        log.info(f"Writing {len(closing_rows)} ERP anchor values to usage...")
        # Also clear closing_stock on the same rows so computeStocks uses erp_stock
        # from the recon table rather than a stale app-calculated closing_stock.
        # closing_stock is only for deliberate manual overrides set in the UI.
        for row in closing_rows:
            row["closing_stock"] = None
        sb_upsert("usage", closing_rows, "week_id,material_id")

    log.info("--- Reconciliation complete ---")



def get_receipt_date(conn, location, stock_code, po_ref, ord_quant=None):
    """
    Find the TRANSDATE of the FIRST receipt transaction in STOCK_TRANS for a
    given stock code at a location. Using the earliest date so that stock lands
    in the week it first physically arrived, even if deliveries were split.
    Returns the date as a string or None.
    """
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT TOP 1 CAST(TRANSDATE AS DATE)
            FROM {STOCK_TRANS_TABLE}
            WHERE STOCKCODE = ?
              AND CAST(LOCATION AS VARCHAR) = ?
              AND TRANSTYPE = {TRANSTYPE_RECEIPT}
              AND REF1 NOT LIKE 'STOCKTAKE%'
              AND (REF1 LIKE ? OR REF1 LIKE ?)
            ORDER BY TRANSDATE ASC
        """, stock_code, str(location), f"%{po_ref}%", f"PO{po_ref}%")
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])[:10]
    except Exception as e:
        log.debug(f"  get_receipt_date failed {stock_code} loc {location} po {po_ref}: {e}")
    return None

def sync_po_lines(conn, weeks, materials, branch_locs, from_date):
    """
    Sync purchase order lines from ERP PURCHORD_LINES into the app.

    For each app order with a po_ref:
    - Pull all ERP lines for that PO
    - Replace app order_lines with ERP lines (keyed on po_line_id)
    - Each ERP line becomes one app order_line with the correct qty, eta, sup_quant
    - Mark received when sup_quant >= corrected qty, fetch received_date from STOCK_TRANS
    - Lock order header eta to earliest non-received line

    Corrected qty: when a PO is "forced fully processed" in EXO (or a line is
    corrected), EXO writes the shortfall/overage to CORRECTION_QUANT, so
    ORD_QUANT - CORRECTION_QUANT (= CORRECTED_QUANT) is the final expected qty
    (equal to SUP_QUANT once forced). E.g. PO 66410 line 89178: 32,000 ordered,
    24,000 supplied, correction 8,000 -> 24,000, so the line counts as received. Using it as the effective ordered qty means
    forced POs are treated as complete, and the app's stock calc counts what
    actually arrived (including over-receipted lines) rather than the original order.

    Orders without a po_ref (forecast/manual) are left untouched.
    """
    log.info("--- Syncing ERP PO lines ---")

    loc_lookup = {l["location"]: l["branch_id"] for l in branch_locs}
    mat_lookup = {(m["branch_id"], m["stock_code"]): m for m in materials if m.get("stock_code")}
    mat_id_to_stock = {m["id"]: m.get("stock_code") for m in materials if m.get("stock_code")}

    # Build branch -> first location mapping for receipt date lookup
    branch_id_to_loc = {}
    for bl in branch_locs:
        if bl["branch_id"] not in branch_id_to_loc:
            branch_id_to_loc[bl["branch_id"]] = bl["location"]

    # Load all ERP PO lines from SQL Server
    q = f"""
        SELECT
            CAST(HDR_SEQNO  AS VARCHAR(50)) AS hdr_seqno,
            CAST(POLINEID   AS VARCHAR(50)) AS po_line_id,
            CAST(STOCKCODE  AS VARCHAR(50)) AS stock_code,
            CAST(BRANCHNO   AS VARCHAR(50)) AS branch_no,
            CAST(ORD_QUANT  AS INT)         AS orig_ord_quant,
            -- = CORRECTED_QUANT, but derived from CORRECTION_QUANT so older rows with
            --   CORRECTED_QUANT unset (0/NULL) aren't mistaken for cancelled lines
            CAST(ORD_QUANT - ISNULL(CORRECTION_QUANT, 0) AS INT) AS ord_quant,
            CAST(SUP_QUANT  AS INT)         AS sup_quant,
            CAST(DUEDATE    AS DATE)        AS due_date
        FROM {PURCHORD_LINES_TABLE}
        WHERE DUEDATE >= '2026-01-01'
          AND STOCKCODE IS NOT NULL
          AND ORD_QUANT > 0
        ORDER BY HDR_SEQNO, POLINEID
    """
    try:
        cur = conn.cursor()
        cur.execute(q)
        rows = cur.fetchall()
        cols = [c[0].lower() for c in cur.description]
        erp_lines = [dict(zip(cols, r)) for r in rows]
        log.info(f"Found {len(erp_lines)} ERP PO lines (from_date={from_date})")
    except Exception as e:
        log.error(f"Failed to load ERP PO lines: {e}"); return

    # Also upsert to erp_po_lines cache table for the ERP badge display.
    # ord_quant here is the corrected qty (see docstring); orig_ord_quant is kept
    # in memory only, for line notes, since erp_po_lines has no column for it.
    upsert_rows = []
    for r in erp_lines:
        upsert_rows.append({
            "hdr_seqno":  str(r["hdr_seqno"]).strip(),
            "po_line_id": str(r["po_line_id"]).strip(),
            "stock_code": str(r["stock_code"]).strip(),
            "branch_no":  str(r["branch_no"]).strip(),
            "ord_quant":  int(r["ord_quant"] or 0),
            "orig_ord_quant": int(r["orig_ord_quant"] or 0),
            "sup_quant":  int(r["sup_quant"] or 0),
            "due_date":   str(r["due_date"])[:10] if r.get("due_date") else None,
        })
    if upsert_rows:
        cache_rows = [{k: v for k, v in r.items() if k != "orig_ord_quant"} for r in upsert_rows]
        sb_upsert("erp_po_lines", cache_rows, "hdr_seqno,po_line_id")
        log.info(f"Upserted {len(cache_rows)} ERP PO lines to cache")

    # Build lookup: hdr_seqno -> [erp_lines]
    from collections import defaultdict
    erp_by_hdr = defaultdict(list)
    for r in upsert_rows:
        erp_by_hdr[r["hdr_seqno"]].append(r)

    # Load app orders and existing order_lines
    try:
        app_orders = sb_get("orders", "select=id,po_ref,eta,received,branch_id,eta_locked,material_id,is_forecast")
        app_lines  = sb_get("order_lines", "select=id,order_id,material_id,eta,received,qty,sup_quant,received_date,po_line_id")
    except Exception as e:
        log.error(f"Failed to load app orders: {e}"); return

    # Group app orders by po_ref
    ords_by_ref = defaultdict(list)
    for o in app_orders:
        if o.get("po_ref") and not o.get("is_forecast"):
            ords_by_ref[str(o["po_ref"]).strip()].append(o)

    lines_to_delete = []  # order_line ids to delete
    lines_to_upsert = []  # new/updated order_lines
    order_updates   = []  # order header eta updates

    today_str = date.today().isoformat()

    for hdr_seqno, erp_lines_for_po in erp_by_hdr.items():
        matching_orders = ords_by_ref.get(hdr_seqno, [])
        if not matching_orders:
            log.debug(f"  PO {hdr_seqno}: no matching app order")
            continue

        if len(matching_orders) > 1:
            log.info(f"  PO {hdr_seqno}: {len(matching_orders)} app orders — split order")

        for app_ord in matching_orders:
            ord_id    = app_ord["id"]
            branch_id = app_ord["branch_id"]
            loc       = branch_id_to_loc.get(branch_id, "1")

            # Existing app lines for this order
            existing = [l for l in app_lines if l["order_id"] == ord_id]
            existing_by_po_line = {str(l.get("po_line_id","")): l for l in existing if l.get("po_line_id")}

            # Find which ERP lines belong to this order's branch/materials
            # Match by stock_code existing in this branch's materials
            branch_stocks = {m.get("stock_code"): m for m in materials
                             if m.get("branch_id") == branch_id and m.get("stock_code")}

            # If this is a split order, only take ERP lines not yet claimed by another order
            # (handled by checking material_id — each split order has different materials)
            ord_mat_ids = {l["material_id"] for l in existing}
            ord_stocks  = {mat_id_to_stock.get(mid) for mid in ord_mat_ids if mat_id_to_stock.get(mid)}

            for el in erp_lines_for_po:
                stock = str(el["stock_code"]).strip()
                if stock not in branch_stocks:
                    continue  # this ERP line doesn't belong to this branch
                if len(matching_orders) > 1 and ord_stocks and stock not in ord_stocks:
                    continue  # split order — only take lines matching this order's materials

                mat = branch_stocks[stock]
                mat_id = mat["id"]
                po_line_id = str(el["po_line_id"]).strip()

                erp_ord  = int(el["ord_quant"] or 0)          # corrected qty
                erp_orig = int(el.get("orig_ord_quant") or erp_ord)
                erp_sup  = int(el["sup_quant"] or 0)
                erp_eta  = str(el["due_date"])[:10] if el.get("due_date") else None
                # Corrected qty of 0 = line cancelled in ERP; treat as done (qty 0 adds no stock)
                received = erp_sup >= erp_ord

                # Get receipt date from STOCK_TRANS whenever any supply exists
                # This applies to partial receipts too — the date tells computeStocks
                # which week to place the supplied portion in the stock calc.
                rcvd_date = None
                if erp_sup > 0:
                    rcvd_date = get_receipt_date(conn, loc, stock, hdr_seqno, erp_ord)
                    if rcvd_date:
                        log.info(f"  PO {hdr_seqno} {stock}: rcvd_date={rcvd_date} "
                                 f"sup={erp_sup:,}/{erp_ord:,}")

                # Check if this line already exists by po_line_id
                existing_line = existing_by_po_line.get(po_line_id)

                if erp_orig != erp_ord:
                    note = (f"ERP: {erp_sup:,} kg supplied — line corrected in ERP from "
                            f"{erp_orig:,} to {erp_ord:,} kg (forced/closed)")
                elif erp_sup > 0:
                    note = f"ERP: {erp_sup:,}/{erp_ord:,} kg supplied"
                else:
                    note = None

                line_row = {
                    "order_id":        ord_id,
                    "material_id":     mat_id,
                    "material_type_id": mat.get("material_type_id"),
                    "qty":             erp_ord,
                    "eta":             erp_eta,
                    "sup_quant":       erp_sup,
                    "received":        received,
                    "received_date":   rcvd_date,
                    "po_line_id":      po_line_id,
                    "notes":           note,
                }

                if existing_line:
                    line_row["id"] = existing_line["id"]
                    log.info(f"  PO {hdr_seqno} line {po_line_id}: update id={existing_line['id']} "
                             f"stock={stock} ord={erp_ord:,} (orig {erp_orig:,}) sup={erp_sup:,} rcvd={received} eta={erp_eta}")
                else:
                    log.info(f"  PO {hdr_seqno} line {po_line_id}: INSERT "
                             f"stock={stock} ord={erp_ord:,} (orig {erp_orig:,}) sup={erp_sup:,} rcvd={received} eta={erp_eta}")

                lines_to_upsert.append(line_row)

            # Delete app lines that no longer exist in ERP for this order
            erp_po_line_ids = {str(el["po_line_id"]).strip() for el in erp_lines_for_po}
            for el in existing:
                if el.get("po_line_id") and str(el["po_line_id"]) not in erp_po_line_ids:
                    lines_to_delete.append(el["id"])
                    log.info(f"  PO {hdr_seqno}: deleting stale line id={el['id']} po_line_id={el.get('po_line_id')}")
                elif not el.get("po_line_id"):
                    # Legacy line without po_line_id — delete so ERP lines replace it
                    lines_to_delete.append(el["id"])
                    log.info(f"  PO {hdr_seqno}: deleting legacy line id={el['id']} (no po_line_id)")

            # Update order header eta to earliest non-received ERP line
            if not app_ord.get("eta_locked"):
                non_rcvd_etas = [str(el["due_date"])[:10] for el in erp_lines_for_po
                                 if el.get("due_date") and int(el.get("sup_quant") or 0) < int(el.get("ord_quant") or 0)]
                if non_rcvd_etas:
                    earliest = min(non_rcvd_etas)
                    if (app_ord.get("eta") or "")[:10] != earliest:
                        order_updates.append({"id": ord_id, "eta": earliest})
                        log.info(f"  PO {hdr_seqno} order {ord_id}: eta -> {earliest}")

    # Apply changes
    if lines_to_delete:
        log.info(f"Deleting {len(lines_to_delete)} stale order lines...")
        for lid in lines_to_delete:
            sb_delete("order_lines", f"id=eq.{lid}")

    if lines_to_upsert:
        log.info(f"Upserting {len(lines_to_upsert)} order lines from ERP...")
        # Split into updates (have id) and inserts (no id)
        to_update = [l for l in lines_to_upsert if "id" in l]
        to_insert = [l for l in lines_to_upsert if "id" not in l]
        if to_update:
            sb_upsert("order_lines", to_update, "id")
        if to_insert:
            sb_post("order_lines", to_insert)
        log.info(f"  Updated {len(to_update)}, inserted {len(to_insert)} lines")

    if order_updates:
        log.info(f"Updating {len(order_updates)} order ETAs...")
        for u in order_updates:
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/orders?id=eq.{u['id']}",
                headers=HDR, data=json.dumps({"eta": u["eta"]})
            )

    # Mark order headers received when ALL their ERP lines are fully supplied
    orders_to_receive = []
    for hdr_seqno, erp_lines_for_po in erp_by_hdr.items():
        matching_orders = ords_by_ref.get(hdr_seqno, [])
        for app_ord in matching_orders:
            if app_ord.get("received"):
                continue  # already marked
            ord_id = app_ord["id"]
            # Get lines for this order from the upserted data
            ord_erp_lines = [l for l in lines_to_upsert if l.get("order_id") == ord_id]
            if not ord_erp_lines:
                continue
            all_received = all(l.get("received") for l in ord_erp_lines)
            if all_received:
                orders_to_receive.append(ord_id)
                log.info(f"  Order {ord_id} (PO {hdr_seqno}): all lines received — marking order received")

    if orders_to_receive:
        log.info(f"Marking {len(orders_to_receive)} orders as received...")
        for ord_id in orders_to_receive:
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/orders?id=eq.{ord_id}",
                headers=HDR, data=json.dumps({"received": True})
            )

    log.info(f"PO sync complete: {len(lines_to_upsert)} lines synced, "
             f"{len(lines_to_delete)} deleted, {len(order_updates)} order ETAs updated")


def sync():
    log.info("=" * 60)
    log.info("Starting SQL Server to Supabase sync")
    from_date = datetime.now() - timedelta(weeks=WEEKS_BACK)
    log.info(f"From: {from_date.date()}")

    # Load Supabase reference data
    log.info("Loading Supabase reference data...")
    try:
        weeks, materials, branch_locs = load_supabase()
    except Exception as e:
        log.error(f"Failed to load Supabase: {e}"); return
    log.info(f"Loaded {len(weeks)} weeks, {len(materials)} materials, {len(branch_locs)} locations")

    if not branch_locs:
        log.error("No branch locations configured. Go to Settings > Branch Locations in the app.")
        return

    # Extend weeks table into the future (26 weeks ahead) so the log and forecast work year-round
    weeks = ensure_future_weeks(weeks, branch_locs, weeks_ahead=26)

    # Connect SQL Server
    log.info("Connecting to SQL Server...")
    try:
        conn = get_conn(); log.info("Connected OK")
    except Exception as e:
        log.error(f"SQL Server connection failed: {e}"); return

    try:
        # 1. Usage
        sync_usage(conn, weeks, materials, branch_locs, from_date)
        # 2. Transfers
        sync_transfers(conn, weeks, materials, branch_locs, from_date)
        # 3. PO lines (ETA updates + received status)
        sync_po_lines(conn, weeks, materials, branch_locs, datetime.now() - timedelta(weeks=26))
        # 4. Reconciliation (needs current orders/order_lines for receipt-aware app_stock calc)
        all_orders = sb_get("orders", "select=id,branch_id,po_ref,eta,received,is_forecast,material_id")
        all_order_lines = sb_get("order_lines", "select=id,order_id,material_id,qty,eta,received")
        sync_reconciliation(conn, weeks, materials, branch_locs, all_orders, all_order_lines)
    except Exception as e:
        log.error(f"Sync error: {e}")
        import traceback; log.error(traceback.format_exc())
    finally:
        conn.close()

    log.info("Sync complete")
    log.info("=" * 60)

if __name__ == "__main__":
    sync()
    sys.exit(0)
