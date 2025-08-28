#!/usr/bin/env python3
# Waybar meetings module for Thunderbird calendars
# - Detects default TB profile
# - Reads calendar SQLite snapshot safely (incl. WAL/SHM)
# - Handles recurring events (RRULE) and exceptions
# - Counts events that start OR overlap today
# - Outputs: {"text": "HH:MM | N", "tooltip": "...", "class": "meetings"}

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

MICROSECOND = 1_000_000  # Your DB uses microseconds since epoch (16 digits)

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

def get_meetings():
    profile_path = find_thunderbird_profile()
    if not profile_path:
        return "--", 0, "Profile not found"

    # Set the calendar DB filename found in your profile
    db_filename = "local.sqlite"  # Confirmed largest
    db_path = os.path.join(profile_path, 'calendar-data', db_filename)
    if not os.path.exists(db_path):
        return "--", 0, f"DB '{db_filename}' not found"

    # Local timezone and today's boundaries
    local_tz = tzlocal()
    now_local = datetime.now(local_tz)
    start_of_day_local = datetime.combine(now_local.date(), time.min, tzinfo=local_tz)
    end_of_day_local = datetime.combine(now_local.date(), time.max, tzinfo=local_tz)
    start_of_day_utc_ts = int(start_of_day_local.astimezone(timezone.utc).timestamp() * MICROSECOND)

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
            return "--", 0, f"DB copy failed: {e}"

        try:
            conn = sqlite3.connect(f"file:{tmp_db_path}?mode=ro", uri=True, timeout=2.0)
            conn.execute("PRAGMA busy_timeout=2000")
            cur = conn.cursor()
        except sqlite3.Error as e:
            return "--", 0, f"DB open failed: {e}"

        # Collect negative exceptions (cancelled instances) if table exists
        exceptions = set()
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cal_exceptions'")
            if cur.fetchone():
                cur.execute("SELECT cal_id, recurrence_id FROM cal_exceptions WHERE is_negative = 1")
                exceptions = {(row[0], row[1]) for row in cur.fetchall()}
        except sqlite3.Error:
            pass  # Missing table is fine

        # Key fix: include recurring masters even if their first event_end is in the past.
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
                WHERE (e.event_end > ? OR e.event_end IS NULL)  -- upcoming or open-ended
                   OR p.value IS NOT NULL                       -- ALWAYS include recurring masters
            """, (start_of_day_utc_ts,))
            events = cur.fetchall()
        except sqlite3.Error as e:
            conn.close()
            return "--", 0, f"Query failed: {e}"

        conn.close()

    # Expand/collect today's instances
    todays_meetings = []
    for event_id, cal_id, start_ts, end_ts, tz_str, rrule_str in events:
        try:
            event_tz = gettz(tz_str) or local_tz
            # TB stores microseconds since epoch; interpret as event_tz for consistency with stored tz
            dtstart = datetime.fromtimestamp(start_ts / MICROSECOND, tz=event_tz)
            dtend = (datetime.fromtimestamp(end_ts / MICROSECOND, tz=event_tz)
                     if end_ts is not None else None)
        except Exception:
            continue  # Skip malformed rows

        if rrule_str:
            # Recurring: expand into today
            try:
                rule = rrulestr(rrule_str, dtstart=dtstart)
                for instance in rule.between(start_of_day_local, end_of_day_local, inc=True):
                    # Build the recurrence_id in microseconds UTC to match exceptions table
                    instance_utc_ts = int(instance.astimezone(timezone.utc).timestamp() * MICROSECOND)
                    if (cal_id, instance_utc_ts) not in exceptions:
                        todays_meetings.append(instance.astimezone(local_tz))
            except (ValueError, TypeError):
                continue
        else:
            # Single event: include if starts today OR overlaps today
            starts_today = start_of_day_local <= dtstart <= end_of_day_local
            overlaps_today = (dtend is not None and
                              dtstart <= end_of_day_local and dtend >= start_of_day_local)
            if starts_today or overlaps_today:
                todays_meetings.append(dtstart.astimezone(local_tz))

    if not todays_meetings:
        return "--", 0, None

    todays_meetings.sort()
    total = len(todays_meetings)

    # Next meeting at/after now; if none left, keep "--"
    next_meeting_time = "--"
    for meeting in todays_meetings:
        if meeting >= now_local:
            next_meeting_time = meeting.strftime("%H:%M")
            break

    return next_meeting_time, total, None

if __name__ == "__main__":
    next_time, total_count, error_msg = get_meetings()
    output = {
        "text": f"{next_time} | {total_count}",
        "tooltip": f"Next meeting: {next_time}\nTotal today: {total_count}",
        "class": "meetings",
    }
    if error_msg:
        output["text"] = "-- | 0"
        output["tooltip"] = f"Error: {error_msg}"
    print(json.dumps(output))

