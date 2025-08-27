import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta
from dateutil.rrule import rrulestr
from dateutil.tz import gettz, tzlocal
import sqlite3

# Detect the default Thunderbird profile
home = os.path.expanduser('~')
profiles_ini = os.path.join(home, '.thunderbird', 'profiles.ini')
with open(profiles_ini, 'r') as f:
    content = f.read()

default_path = ''
install_default = ''
in_install = False
in_profile = False
is_default = False
path = ''
for line in content.splitlines():
    if line.startswith('[Install'):
        in_install = True
        in_profile = False
    elif line.startswith('[Profile'):
        in_profile = True
        in_install = False
        path = ''
        is_default = False
    elif line.startswith('['):
        if in_profile and is_default:
            default_path = path
        in_profile = False
        in_install = False
    if line.startswith('Default='):
        value = line.split('=')[1].strip()
        if in_install:
            install_default = value
        elif in_profile:
            if value == '1':
                is_default = True
    if line.startswith('Path='):
        if in_profile:
            path = line.split('=')[1].strip()
if in_profile and is_default:
    default_path = path
PROFILE = install_default if install_default else default_path

# Path to the calendar database
DB = os.path.join(home, '.thunderbird', PROFILE, 'calendar-data', 'local.sqlite')

# Create a temporary copy of the database to avoid lock issues
with tempfile.TemporaryDirectory() as tmpdir:
    tmp_db = os.path.join(tmpdir, 'local.sqlite')
    shutil.copy(DB, tmp_db)

    # Connect to the temporary database copy
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()

    # Get today's start and end in local time, aware
    local_tz = tzlocal()
    now = datetime.now(local_tz)
    today = now.date()
    start_of_day = datetime.combine(today, datetime.min.time()).replace(tzinfo=local_tz)
    end_of_day = start_of_day + timedelta(days=1) - timedelta(seconds=1)
    start_of_day_s = int(start_of_day.timestamp())
    end_of_day_s = int(end_of_day.timestamp())

    # Query for events that could overlap today, including recurring
    cur.execute("""
    SELECT e.id, e.cal_id, e.event_start / 1000000, e.event_end / 1000000, p.value, e.event_start_tz
    FROM cal_events e
    LEFT JOIN cal_properties p ON p.key = 'RRULE' AND p.item_id = e.id AND p.cal_id = e.cal_id
    WHERE (e.event_end / 1000000 > ? OR e.event_end IS NULL) AND (e.event_start / 1000000 < ? + 86400)
    """, (start_of_day_s, start_of_day_s))

    events = cur.fetchall()

    # Collect all instance start times
    instance_starts = []
    for event in events:
        event_id, cal_id, start_s, end_s, rrule_str, start_tz_str = event
        if start_tz_str is None or start_tz_str == 'floating':
            tzinfo = local_tz
        else:
            tzinfo = gettz(start_tz_str) or local_tz  # Fallback if invalid
        start_dt = datetime.fromtimestamp(start_s, tz=tzinfo)
        end_dt = datetime.fromtimestamp(end_s, tz=tzinfo) if end_s is not None else None
        if rrule_str is None:
            # Non-recurring
            if start_of_day <= start_dt <= end_of_day:
                instance_starts.append(start_dt)
        else:
            # Recurring
            rrule = rrulestr(rrule_str, dtstart=start_dt)
            instances = rrule.between(start_of_day, end_of_day, inc=True)
            if end_dt is not None:
                instances = [inst for inst in instances if inst <= end_dt]
            instance_starts.extend(instances)

    conn.close()

# Sort the instances
instance_starts.sort()

# Count total
total = len(instance_starts)

# Find next
next_time = "--"
for inst in instance_starts:
    if inst >= now:
        next_time = inst.strftime("%H:%M")
        break

# Output
print(f"({next_time} | {total})")
