#!/usr/bin/env python3

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
    exit(1)

# ===================================================================
# THIS FUNCTION HAS BEEN REPLACED WITH A MORE RELIABLE VERSION
# ===================================================================
def find_thunderbird_profile():
    """Finds the default Thunderbird profile path using a more robust method."""
    profiles_ini_path = os.path.expanduser('~/.thunderbird/profiles.ini')
    if not os.path.exists(profiles_ini_path):
        return None

    config = configparser.ConfigParser()
    config.read(profiles_ini_path)

    # Store all available profiles by their path for easy lookup
    profile_path_map = {}
    for section in config.sections():
        if section.startswith('Profile') and 'Path' in config[section]:
            profile_path_map[config[section]['Path']] = section

    # Priority 1: Check the [Install] section, which is the most reliable source.
    for section in config.sections():
        if section.startswith('Install') and 'Default' in config[section]:
            default_path_key = config[section]['Default']
            if default_path_key in profile_path_map:
                profile_section = profile_path_map[default_path_key]
                path = config[profile_section]['Path']
                is_relative = config.getint(profile_section, 'IsRelative', fallback=1)
                if is_relative:
                    return os.path.join(os.path.dirname(profiles_ini_path), path)
                return path

    # Fallback (Priority 2): Find a profile with the 'Default=1' flag.
    for section in config.sections():
        if section.startswith('Profile') and config.getboolean(section, 'Default', fallback=False):
            path = config[section]['Path']
            is_relative = config.getint(section, 'IsRelative', fallback=1)
            if is_relative:
                return os.path.join(os.path.dirname(profiles_ini_path), path)
            return path

    return None # If no profile is found

def get_meetings():
    """
    Connects to the Thunderbird calendar DB, fetches today's meetings,
    and returns the next meeting time and total count.
    """
    profile_path = find_thunderbird_profile()
    if not profile_path:
        return "--", 0, "Profile not found"

    # IMPORTANT: You may still need to change 'local.sqlite' below if you use
    # a synced calendar. First, see if this script works. If not, check
    # the contents of the calendar-data directory as described previously.
    db_path = os.path.join(profile_path, 'calendar-data', 'local.sqlite')
    
    if not os.path.exists(db_path):
        # This error now also tells you which directory it checked
        return "--", 0, f"DB not found in {os.path.dirname(db_path)}"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db_path = os.path.join(tmpdir, 'db.sqlite')
        try:
            shutil.copy2(db_path, tmp_db_path)
        except FileNotFoundError:
             return "--", 0, f"DB copy failed from {db_path}"

        conn = sqlite3.connect(tmp_db_path)
        cur = conn.cursor()

        local_tz = tzlocal()
        now_local = datetime.now(local_tz)
        start_of_day_local = datetime.combine(now_local.date(), time.min, tzinfo=local_tz)
        end_of_day_local = datetime.combine(now_local.date(), time.max, tzinfo=local_tz)
        start_of_day_utc_ts = int(start_of_day_local.astimezone(timezone.utc).timestamp() * 1_000_000)

        cur.execute("SELECT cal_id, recurrence_id FROM cal_exceptions WHERE is_negative = 1")
        exceptions = { (row[0], row[1]) for row in cur.fetchall() }

        cur.execute("""
            SELECT e.id, e.cal_id, e.event_start, e.event_start_tz, p.value AS rrule
            FROM cal_events e
            LEFT JOIN cal_properties p ON p.item_id = e.id AND p.key = 'RRULE'
            WHERE e.event_end > ? OR e.event_end IS NULL
        """, (start_of_day_utc_ts,))
        
        events = cur.fetchall()
        conn.close()

    todays_meetings = []
    for event_id, cal_id, start_ts, tz_str, rrule_str in events:
        try:
            event_tz = gettz(tz_str) or local_tz
            dtstart = datetime.fromtimestamp(start_ts / 1_000_000, tz=event_tz)
        except Exception:
            continue

        if rrule_str:
            try:
                rule = rrulestr(rrule_str, dtstart=dtstart)
                for instance in rule.between(start_of_day_local, end_of_day_local, inc=True):
                    instance_utc_ts = int(instance.astimezone(timezone.utc).timestamp() * 1_000_000)
                    if (cal_id, instance_utc_ts) not in exceptions:
                        todays_meetings.append(instance.astimezone(local_tz))
            except (ValueError, TypeError):
                continue
        else:
            if start_of_day_local <= dtstart <= end_of_day_local:
                todays_meetings.append(dtstart.astimezone(local_tz))

    if not todays_meetings:
        return "--", 0, None

    todays_meetings.sort()
    
    total = len(todays_meetings)
    next_meeting_time = "--"
    
    for meeting in todays_meetings:
        if meeting >= now_local:
            next_meeting_time = meeting.strftime("%H:%M")
            break

    return next_meeting_time, total, None

if __name__ == "__main__":
    next_time, total_count, error_msg = get_meetings()
    
    output = {
        "text": f"({next_time} | {total_count})",
        "tooltip": f"Next meeting: {next_time}\nTotal today: {total_count}",
        "class": "meetings"
    }
    
    if error_msg:
        output["text"] = "(-- | 0)"
        output["tooltip"] = f"Error: {error_msg}"

    print(json.dumps(output))
