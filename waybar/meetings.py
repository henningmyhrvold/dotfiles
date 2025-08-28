#!/usr/bin/env python3
# Waybar meetings module for Thunderbird calendars
# - Detects default TB profile
# - Scans ALL relevant SQLite files (calendar-data/*.sqlite and profile/*.sqlite)
# - Safe snapshot (incl. WAL/SHM), read-only
# - Auto-detects timestamp units (us/ms/s) per DB
# - Handles recurring events (RRULE) + negative exceptions
# - Counts events that start OR overlap today (incl. all-day)
# - Displays *meetings left today* = events whose END >= now (ongoing are included)
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

    # Heuristic: if no candidates yet, scan deeper under calendar-data
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

    # We'll collect (start_local, end_local) tuples for all today's instances
    todays_instances = []
    scanned_summary = []     # per-DB debug lines
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
                        # duration used for each recurrence instance
                        duration = (dtend - dtstart) if dtend is not None else None
                        try:
                            rule = rrulestr(rrule_str, dtstart=dtstart)
                            for inst in rule.between(sod_local, eod_local, inc=True):
                                inst_start_local = inst.astimezone(local_tz)
                                inst_end_local = (inst_start_local + duration) if duration else None
                                inst_utc = to_epoch(inst.astimezone(timezone.utc), unit)
                                if (cal_id, inst_utc) not in exceptions:
                                    # Keep if instance is today (starts today or overlaps today)
                                    overlaps_today = (inst_end_local is not None and
                                                      inst_start_local <= eod_local and
                                                      inst_end_local >= sod_local)
                                    starts_today = sod_local <= inst_start_local <= eod_local
                                    if starts_today or overlaps_today:
                                        todays_instances.append((inst_start_local, inst_end_local))
                                        used_dbs.append(db)
                        except Exception:
                            # If RRULE parsing fails, at least consider the base start/end
                            start_local = dtstart.astimezone(local_tz)
                            end_local = dtend.astimezone(local_tz) if dtend else None
                            starts_today = sod_local <= start_local <= eod_local
                            overlaps_today = (end_local is not None and
                                              start_local <= eod_local and end_local >= sod_local)
                            if starts_today or overlaps_today:
                                todays_instances.append((start_local, end_local))
                                used_dbs.append(db)
                    else:
                        single += 1
                        total_single += 1
                        start_local = dtstart.astimezone(local_tz)
                        end_local = dtend.astimezone(local_tz) if dtend else None
                        starts_today = sod_local <= start_local <= eod_local
                        overlaps_today = (end_local is not None and
                                          start_local <= eod_local and end_local >= sod_local)
                        if starts_today or overlaps_today:
                            todays_instances.append((start_local, end_local))
                            used_dbs.append(db)

                # Per-DB debug summary (peek range of starts)
                first_dt = last_dt = None
                try:
                    starts = sorted(
                        [from_epoch(r[2], unit, local_tz) for r in rows if r[2] is not None]
                    )
                    if starts:
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

    if not todays_instances:
        dbg = None
        if debug_on:
            used = ", ".join(sorted({os.path.basename(p) for p in used_dbs})) or "none"
            dbg = ("; ".join(scanned_summary) +
                   f" | today=0 used

