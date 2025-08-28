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
    conn = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True, timeout=2.0)
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

def to_epoch(dt, factor):
    return int(dt.timestamp() * factor)

def from_epoch(ts, factor, tzinfo):
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
        return "--", 0, "No .sqlite candidates found", "debug: searched {0}".format(profile_path)

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
                scanned_summary.append("{0}: open-failed:{1}".format(os.path.basename(db), e))
                continue

            try:
                if not has_cal_events(conn):
                    scanned_summary.append("{0}: no cal_events".format(os.path.basename(db)))
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
                    scanned_summary.append("{0}: 0 rows".format(os.path.basename(db)))
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
                    starts = [s for s in starts if s is not None]
                    if starts:
                        first_dt = starts[0].strftime("%Y-%m-%d")
                        last_dt = starts[-1].strftime("%Y-%m-%d")
                except Exception:
                    pass

                scanned_summary.append(
                    "{0}: unit={1} rows={2} recur={3} single={4} first={5} last={6}".format(
                        os.path.basename(db), unit, len(rows), recur, single, first_dt, last_dt
                    )
                )

            except Exception as e:
                try:
                    conn.close()
                except Exception:
                    pass
                scanned_summary.append("{0}: error:{1}".format(os.path.basename(db), e))
                continue

    if not todays_instances:
        dbg = None
        if debug_on:
            used = ", ".join(sorted({os.path.basename(p) for p in used_dbs})) or "none"
            dbg = "; ".join(scanned_summary) + \
                  " | today=0 used_dbs=[{0}] total_scanned={1} recur={2} single={3}".format(
                      used, total_scanned, total_recur, total_single
                  )
        return "--", 0, None, dbg

    # Sort by start time
    todays_instances.sort(key=lambda t: t[0])

    # Compute "meetings left": events whose END >= now.
    # If an event has no end, we treat it as remaining only if it hasn't started yet.
    now_local = datetime.now(tzlocal())
    left_instances = [
        (s, e) for (s, e) in todays_instances
        if (e is not None and e >= now_local) or (e is None and s >= now_local)
    ]
    left_count = len(left_instances)

    # Next meeting time = next start >= now (ignores ongoing)
    next_meeting_time = "--"
    for start_local, _end_local in todays_instances:
        if start_local >= now_local:
            next_meeting_time = start_local.strftime("%H:%M")
            break

    dbg = None
    if debug_on:
        used = ", ".join(sorted({os.path.basename(p) for p in used_dbs}))
        first = todays_instances[0][0].strftime("%H:%M")
        last = todays_instances[-1][0].strftime("%H:%M")
        dbg = "today={0} left={1} used_dbs=[{2}] total_scanned={3} recur={4} single={5} first={6} last={7}".format(
            len(todays_instances), left_count, used, total_scanned, total_recur, total_single, first, last
        )

    return next_meeting_time, left_count, None, dbg

# ---------- Main ----------

if __name__ == "__main__":
    next_time, left_count, error_msg, debug_info = get_meetings()
    tooltip_lines = []
    if error_msg:
        text = "-- | 0"
        tooltip_lines.append("Error: {0}".format(error_msg))
    else:
        text = "{0} | {1}".format(next_time, left_count)
        tooltip_lines.append("Next meeting: {0}".format(next_time))
        tooltip_lines.append("Left today: {0}".format(left_count))
    if debug_info:
        tooltip_lines.append(debug_info)
    print(json.dumps({"text": text, "tooltip": "\n".join(tooltip_lines), "class": "meetings"}))

