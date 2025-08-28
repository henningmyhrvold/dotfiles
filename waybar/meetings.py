#!/usr/bin/env python3
# Waybar meetings module for Thunderbird calendars
# - Detects default TB profile
# - Scans ALL relevant SQLite files (calendar-data/*.sqlite and profile/*.sqlite)
# - Safe snapshot (incl. WAL/SHM), read-only
# - Auto-detects timestamp units (us/ms/s) per DB
# - Handles recurring events (RRULE) + negative exceptions
# - Counts events that start OR overlap today (incl. all-day)
# - Outputs: {"text": "HH:MM | N", "tooltip": "...", "class": "meetings"}
# Set DEBUG=1 in the environment to get extra details in the tooltip.

import os
import sqlite3
import shutil
import tempfile
import json
import configparser
from datetime import datetime, time, timezone

try:
    from dateutil.rrule import rrulestr
    from dateutil.tz import gettz, tzlocal
except ImportError:
    print(json.dumps({"text": "Error", "tooltip": "python-dateutil not installed"}))
    raise SystemExit(1)

# ---------- Utilities ----------

def find_thunderbird_profile():
    """Find the default Thunderbird profile path via profiles.ini."""
    profiles_ini_path = os.path.expanduser('~/.thunderbird/profiles.ini')
    if not os.path.exists(profiles_ini_path):
        return None

    cfg = configparser.ConfigParser()
    cfg.read(profiles_ini_path)

    # Map path->section for profile sections
    path_to_section = {}
    for s in cfg.sections():
        if s.startswith('Profile') and 'Path' in cfg[s]:
            path_to_section[cfg[s]['Path']] = s

    # Prefer Install:Default (newer TB)
    for s in cfg.sections():
        if s.startswith('Install') and 'Default' in cfg[s]:
            key = cfg[s]['Default']
            if key in path_to_section:
                psec = path_to_section[key]
                path = cfg[psec]['Path']
                is_rel = cfg.getint(psec, 'IsRelative', fallback=1)
                base = os.path.dirname(profiles_ini_path)
                return os.path.join(base, path) if is_rel else path

    # Fallback: Profile with Default=true (older TB)
    for s in cfg.sections():
        if s.startswith('Profile') and cfg.getboolean(s, 'Default', fallback=False):
            path = cfg[s]['Path']
            is_rel = cfg.getint(s, 'IsRelative', fallback=1)
            base = os.path.dirname(profiles_ini_path)
            return os.path.join(base, path) if is_rel else path

    return None

def list_candidate_sqlites(profile_path):
    """
    Return a list of candidate .sqlite files to inspect:
    - profile/*.sqlite
    - profile/calendar-data/*.sqlite
    (Thunderbird may keep per-calendar caches in multiple DBs.)
    """
    candidates = set()

    # Profile root
    for name in os.listdir(profile_path):
        if name.endswith('.sqlite'):
            candidates.add(os.path.join(profile_path, name))

    # calendar-data subdir
    caldir = os.path.join(profile_path, 'calendar-data')
    if os.path.isdir(caldir):
        for name in os.listdir(caldir):
            if name.endswith('.sqlite'):
                candidates.add(os.path.join(caldir, name))

    # Heuristic: if no candidates yet, scan one more level deep under calendar-data
    if not candidates and os.path.isdir(caldir):
        for root, _, files in os.walk(caldir):
            for name in files:
                if name.endswith('.sqlite'):
                    candidates.add(os.path.join(root, name))

    # Return sorted (largest first) to prioritize likely stores
    return sorted(candidates, key=lambda p: -os.path.getsize(p))

def snapshot_sqlite(db_path, tmpdir):
    """Copy db + sidecars (WAL/SHM) into tmpdir; return path to the copied DB."""
    base = os.path.join(tmpdir, os.path.basename(db_path))
    shutil.copy2(db_path, base)
    for ext in ('-wal', '-shm'):
        side = db_path + ext
        if os.path.exists(side):
            shutil.copy2(side, base + ext)
    return base

def open_ro(path):
    """Open an SQLite DB in read-only URI mode with a small busy timeout."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    conn.execute("PRAGMA busy_timeout=2000")
    return conn

def has_cal_events(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cal_events'")
    return cur.fetchone() is not None

def detect_unit(conn):
    """Return epoch unit factor (1 for s, 1_000 for ms, 1_000_000 for us)."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(event_start) FROM cal_events WHERE event_start IS NOT NULL")
        val = cur.fetchone()[0]
        if val is None:
            return 1_000_000  # default to us if empty table
        v = abs(int(val))
        if v >= 10**15: return 1_000_000
        if v >= 10**12: return 1_000
        if v >= 10**9:  return 1
        return 1_000_000
    except Exception:
        return 1_000_000

def to_epoch(dt: datetime, factor: int) -> int:
    return int(dt.timestamp() * factor)

def from_epoch(ts: int, factor: int, tzinfo):
    try:
        return datetime.fromtimestamp(int(ts) / factor, tz=tzinfo)
    except Exception:
        return None

# ---------- Core ----------

def get_meetings():
    debug_on = os.environ.get("DEBUG") == "1"

    profile_path = find_thunderbird_profile()
    if not profile_path:
        return "--", 0, "Profile not found", "debug: no profile"

    candidates = list_candidate_sqlites(profile_path)
    if not candidates:
        return "--", 0, "No .sqlite candidates found", f"debug: searched {profile_path}"

    local_tz = tzlocal()
    now_local = datetime.now(local_tz)
    sod_local = datetime.combine(now_local.date(), time.min, tzinfo=local_tz)
    eod_local = datetime.combine(now_local.date(), time.max, tzinfo=local_tz)

    todays = []
    scanned_summary = []  # per-DB debug
    total_scanned = 0
    total_recur = 0
    total_single = 0
    used_dbs = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for db in candidates:
            try:
                snap = snapshot_sqlite(db, tmpdir)
                conn = open_ro(snap)
            except Exception as e:
                scanned_summary.append(f"{os.path.basename(db)}: open-failed:{e}")
                continue

            try:
                if not has_cal_events(conn):
                    scanned_summary.append(f"{os.path.basename(db)}: no cal_events")
                    conn.close()
                    continue

                unit = detect_unit(conn)
                cur = conn.cursor()

                # Negative exceptions (cancelled instances)
                exceptions = set()
                try:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cal_exceptions'")
                    if cur.fetchone():
                        cur.execute("SELECT cal_id, recurrence_id FROM cal_exceptions WHERE is_negative = 1")
                        exceptions = {(r[0], r[1]) for r in cur.fetchall()}
                except sqlite3.Error:
                    pass

                # Fetch all events (filter in Python to avoid dropping recurring masters)
                cur.execute("""
                    SELECT e.id,
                           e.cal_id,
                           e.event_start,
                           e.event_end,
                           e.event_start_tz,
                           p.value AS rrule
                    FROM cal_events e
                    LEFT JOIN cal_properties p
                           ON p.item_id = e.id AND p.key = 'RRULE'
                """)
                rows = cur.fetchall()
                conn.close()

                if not rows:
                    scanned_summary.append(f"{os.path.basename(db)}: 0 rows")
                    continue

                scanned = 0
                recur = 0
                single = 0

                for event_id, cal_id, start_ts, end_ts, tz_str, rrule_str in rows:
                    scanned += 1
                    total_scanned += 1
                    event_tz = gettz(tz_str) or local_tz

                    dtstart = from_epoch(start_ts, unit, event_tz)
                    dtend = from_epoch(end_ts, unit, event_tz) if end_ts is not None else None
                    if dtstart is None:
                        continue

                    if rrule_str:
                        recur += 1
                        total_recur += 1
                        try:
                            rule = rrulestr(rrule_str, dtstart=dtstart)
                            for inst in rule.between(sod_local, eod_local, inc=True):
                                inst_utc = to_epoch(inst.astimezone(timezone.utc), unit)
                                if (cal_id, inst_utc) not in exceptions:
                                    todays.append(inst.astimezone(local_tz))
                                    used_dbs.append(db)
                        except Exception:
                            # If RRULE parsing fails, at least consider the base start
                            if sod_local <= dtstart.astimezone(local_tz) <= eod_local:
                                todays.append(dtstart.astimezone(local_tz))
                                used_dbs.append(db)
                    else:
                        single += 1
                        total_single += 1
                        starts_today = sod_local <= dtstart.astimezone(local_tz) <= eod_local
                        overlaps_today = (dtend is not None and
                                          dtstart.astimezone(local_tz) <= eod_local and
                                          dtend.astimezone(local_tz) >= sod_local)
                        if starts_today or overlaps_today:
                            todays.append(dtstart.astimezone(local_tz))
                            used_dbs.append(db)

                first_dt = None
                last_dt = None
                try:
                    # peek a couple of starts for sanity
                    starts = [from_epoch(r[2], unit, local_tz) for r in rows if r[2] is not None]
                    starts = [s for s in starts if s is not None]
                    if starts:
                        starts.sort()
                        first_dt = starts[0].strftime("%Y-%m-%d")
                        last_dt = starts[-1].strftime("%Y-%m-%d")
                except Exception:
                    pass

                scanned_summary.append(
                    f"{os.path.basename(db)}: unit={unit} rows={len(rows)} "
                    f"recur={recur} single={single} first={first_dt} last={last_dt}"
                )

            except Exception as e:
                try:
                    conn.close()
                except Exception:
                    pass
                scanned_summary.append(f"{os.path.basename(db)}: error:{e}")
                continue

    if not todays:
        dbg = None
        if debug_on:
            used = ", ".join(sorted({os.path.basename(p) for p in used_dbs})) or "none"
            dbg = ("; ".join(scanned_summary) +
                   f" | today=0 used_dbs=[{used}] total_scanned={total_scanned} "
                   f"recur={total_recur} single={total_single}")
        return "--", 0, None, dbg

    todays.sort()
    total = len(todays)

    # Next meeting at/after now; if none left, keep "--"
    next_meeting = "--"
    for t in todays:
        if t >= datetime.now(local_tz):
            next_meeting = t.strftime("%H:%M")
            break

    dbg = None
    if debug_on:
        used = ", ".join(sorted({os.path.basename(p) for p in used_dbs}))
        first = todays[0].strftime("%H:%M")
        last = todays[-1].strftime("%H:%M")
        dbg = (f"today={total} used_dbs=[{used}] total_scanned={total_scanned} "
               f"recur={total_recur} single={total_single} first={first} last={last}")

    return next_meeting, total, None, dbg

# ---------- Main ----------

if __name__ == "__main__":
    next_time, total_count, error_msg, debug_info = get_meetings()
    tooltip_lines = []
    if error_msg:
        text = "-- | 0"
        tooltip_lines.append(f"Error: {error_msg}")
    else:
        text = f"{next_time} | {total_count}"
        tooltip_lines.append(f"Next meeting: {next_time}")
        tooltip_lines.append(f"Total today: {total_count}")
    if debug_info:
        tooltip_lines.append(debug_info)
    print(json.dumps({"text": text, "tooltip": "\n".join(tooltip_lines), "class": "meetings"}))

