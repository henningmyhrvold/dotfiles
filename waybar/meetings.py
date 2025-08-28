#!/usr/bin/env python3
# Waybar meetings module for Thunderbird calendars
# - Detects default TB profile
# - Reads calendar SQLite snapshot safely (incl. WAL/SHM)
# - Auto-detects timestamp units (us/ms/s)
# - Handles recurring events (RRULE) and negative exceptions
# - Counts events that start OR overlap today (incl. all-day)
# - Outputs: {"text": "HH:MM | N", "tooltip": "...", "class": "meetings"}
# Set DEBUG=1 in the environment to get extra details in tooltip.

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

def detect_unit(conn) -> int:
    """Return epoch unit factor (1 for seconds, 1000 for ms, 1_000_000 for us)."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(event_start) FROM cal_events WHERE event_start IS NOT NULL")
        val = cur.fetchone()[0]
        if val is None:
            return 1_000_000  # default to us if empty
        v = abs(int(val))
        if v >= 10**15:  # ~microseconds
            return 1_000_000
        if v >= 10**12:  # ~milliseconds
            return 1_000
        if v >= 10**9:   # ~seconds
            return 1
        return 1_000_000
    except Exception:
        return 1_000_000

def find_thunderbird_profile():
    """Find the default Thunderbird profile path using profiles.ini."""
    profiles_ini_path = os.path.expanduser('~/.thunderbird/profiles.ini')
    if not os.path.exists(profiles_ini_path):
        return None

    config = configparser.ConfigParser()
    config.read(profiles_ini_path)

    # Map path->section for profile sections
    profile_path_map = {}
    for section in config.sections():
        if section.startswith('Profile') and 'Path' in config[section]:
            profile_path_map[config[section]['Path']] = section

    # Prefer Install:Default (newer TB)
    for section in config.sections():
        if section.startswith('Install') and 'Default' in config[section]:
            key = config[section]['Default']
            if key in profile_path_map:
                s = profile_path_map[key]
                path = config[s]['Path']
                is_rel = config.getint(s, 'IsRelative', fallback=1)
                base = os.path.dirname(profiles_ini_path)
                return os.path.join(base, path) if is_rel else path

    # Fallback: Profile with Default=true (older TB)
    for section in config.sections():
        if section.startswith('Profile') and config.getboolean(section, 'Default', fallback=False):
            path = config[section]['Path']
            is_rel = config.getint(section, 'IsRelative', fallback=1)
            base = os.path.dirname(profiles_ini_path)
            return os.path.join(base, path) if is_rel else path

    return None

def to_epoch(dt: datetime, factor: int) -> int:
    return int(dt.timestamp() * factor)

def from_epoch(ts: int, factor: int, tzinfo) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ts) / factor, tz=tzinfo)
    except Exception:
        return None

def get_meetings():
    debug = os.environ.get("DEBUG") == "1"

    profile_path = find_thunderbird_profile()
    if not profile_path:
        return "--", 0, "Profile not found", "debug: no profile"

    # Pick the calendar DB (you said 'local.sqlite' is the largest)
    db_filename = "local.sqlite"
    db_path = os.path.join(profile_path, 'calendar-data', db_filename)
    if not os.path.exists(db_path):
        return "--", 0, f"DB '{db_filename}' not found", f"debug: looked in {os.path.dirname(db_path)}"

    # Local timezone and today's boundaries
    local_tz = tzlocal()
    now_local = datetime.now(local_tz)
    sod_local = datetime.combine(now_local.date(), time.min, tzinfo=local_tz)
    eod_local = datetime.combine(now_local.date(), time.max, tzinfo=local_tz)

    scanned = 0
    recurrences_seen = 0
    singles_seen = 0
    unit = None

    # Snapshot DB (copy base + WAL/SHM if present), then open read-only with a small timeout
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db_path = os.path.join(tmpdir, 'db.sqlite')
        try:
            shutil.copy2(db_path, tmp_db_path)
            for ext in ("-wal", "-shm"):
                side = db_path + ext
                if os.path.exists(side):
                    shutil.copy2(side, tmp_db_path + ext)
        except Exception as e:
            return "--", 0, f"DB copy failed: {e}", f"debug: src={db_path}"

        try:
            conn = sqlite3.connect(f"file:{tmp_db_path}?mode=ro", uri=True, timeout=2.0)
            conn.execute("PRAGMA busy_timeout=2000")
            cur = conn.cursor()
        except sqlite3.Error as e:
            return "--", 0, f"DB open failed: {e}", None

        # Determine epoch unit from the data
        unit = detect_unit(conn)

        # Exceptions: cancelled instances (if table exists)
        exceptions = set()
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cal_exceptions'")
            if cur.fetchone():
                cur.execute("SELECT cal_id, recurrence_id FROM cal_exceptions WHERE is_negative = 1")
                exceptions = {(row[0], row[1]) for row in cur.fetchall()}
        except sqlite3.Error:
            pass  # Missing table is fine

        # IMPORTANT: no WHERE filter — fetch everything and filter in Python.
        try:
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
            events = cur.fetchall()
        except sqlite3.Error as e:
            conn.close()
            return "--", 0, f"Query failed: {e}", None

        conn.close()

    # Expand/collect today's instances
    todays = []
    for event_id, cal_id, start_ts, end_ts, tz_str, rrule_str in events:
        scanned += 1
        event_tz = gettz(tz_str) or local_tz

        dtstart = from_epoch(start_ts, unit, event_tz)
        dtend = from_epoch(end_ts, unit, event_tz) if end_ts is not None else None
        if dtstart is None:
            continue

        if rrule_str:
            recurrences_seen += 1
            try:
                rule = rrulestr(rrule_str, dtstart=dtstart)
                for inst in rule.between(sod_local, eod_local, inc=True):
                    inst_utc = to_epoch(inst.astimezone(timezone.utc), unit)
                    if (cal_id, inst_utc) not in exceptions:
                        todays.append(inst.astimezone(local_tz))
            except Exception:
                # If RRULE parsing fails, at least consider the base start
                if sod_local <= dtstart.astimezone(local_tz) <= eod_local:
                    todays.append(dtstart.astimezone(local_tz))
        else:
            singles_seen += 1
            # Single event: include if starts today OR overlaps today (covers all-day)
            starts_today = sod_local <= dtstart.astimezone(local_tz) <= eod_local
            overlaps_today = (dtend is not None and
                              dtstart.astimezone(local_tz) <= eod_local and
                              dtend.astimezone(local_tz) >= sod_local)
            if starts_today or overlaps_today:
                todays.append(dtstart.astimezone(local_tz))

    if not todays:
        dbg = None
        if debug:
            dbg = (f"unit={unit} scanned={scanned} recurrences={recurrences_seen} "
                   f"singles={singles_seen} db={db_path}")
        return "--", 0, None, dbg

    todays.sort()
    total = len(todays)

    # Next meeting at/after now; if none left, keep "--"
    next_meeting = "--"
    for t in todays:
        if t >= now_local:
            next_meeting = t.strftime("%H:%M")
            break

    dbg = None
    if debug:
        first = todays[0].strftime("%H:%M") if todays else "--"
        last = todays[-1].strftime("%H:%M") if todays else "--"
        dbg = (f"unit={unit} scanned={scanned} recurrences={recurrences_seen} "
               f"singles={singles_seen} first={first} last={last} db={db_path}")

    return next_meeting, total, None, dbg

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

