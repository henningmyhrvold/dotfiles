#!/usr/bin/env python3
import os, sqlite3, shutil, tempfile, json, configparser
from datetime import datetime, time, timezone

try:
    from dateutil.rrule import rrulestr
    from dateutil.tz import gettz, tzlocal
except ImportError:
    print(json.dumps({"text": "Error", "tooltip": "python-dateutil not installed"}))
    raise SystemExit(1)

def find_thunderbird_profile():
    profiles_ini_path = os.path.expanduser('~/.thunderbird/profiles.ini')
    if not os.path.exists(profiles_ini_path):
        return None
    cfg = configparser.ConfigParser()
    cfg.read(profiles_ini_path)
    profile_path_map = {cfg[s]['Path']: s for s in cfg.sections() if s.startswith('Profile') and 'Path' in cfg[s]}
    for s in cfg.sections():
        if s.startswith('Install') and 'Default' in cfg[s]:
            key = cfg[s]['Default']
            if key in profile_path_map:
                prof = profile_path_map[key]
                path = cfg[prof]['Path']
                is_rel = cfg.getint(prof, 'IsRelative', fallback=1)
                root = os.path.dirname(profiles_ini_path)
                return os.path.join(root, path) if is_rel else path
    for s in cfg.sections():
        if s.startswith('Profile') and cfg.getboolean(s, 'Default', fallback=False):
            path = cfg[s]['Path']
            is_rel = cfg.getint(s, 'IsRelative', fallback=1)
            root = os.path.dirname(profiles_ini_path)
            return os.path.join(root, path) if is_rel else path
    return None

def detect_calendar_db(profile_path):
    candidates = []
    caldir = os.path.join(profile_path, 'calendar-data')
    if os.path.isdir(caldir):
        for name in os.listdir(caldir):
            if name.endswith('.sqlite'):
                candidates.append(os.path.join(caldir, name))
    legacy = os.path.join(profile_path, 'storage.sqlite')
    if os.path.exists(legacy):
        candidates.append(legacy)
    for db in sorted(candidates, key=lambda p: -os.path.getsize(p)):
        try:
            with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                names = {r[0] for r in cur.fetchall()}
                if 'cal_events' in names:
                    return db
        except Exception:
            continue
    return None

def detect_epoch_unit(conn):
    cur = conn.cursor()
    cur.execute("SELECT event_start FROM cal_events WHERE event_start IS NOT NULL ORDER BY event_start DESC LIMIT 1")
    row = cur.fetchone()
    if not row or row[0] is None:
        return 1_000_000
    v = abs(int(row[0]))
    if v >= 10**15: return 1_000_000   # microseconds
    if v >= 10**12: return 1_000       # milliseconds
    if v >= 10**9:  return 1           # seconds
    return 1_000_000

def to_epoch(dt, factor): return int(dt.timestamp() * factor)
def from_epoch(ts, factor, tzinfo):
    try: return datetime.fromtimestamp(int(ts)/factor, tz=tzinfo)
    except Exception: return None

def get_meetings():
    profile_path = find_thunderbird_profile()
    if not profile_path:
        return "--", 0, "Thunderbird profile not found", None
    db_path = detect_calendar_db(profile_path)
    if not db_path:
        return "--", 0, "No calendar sqlite found under ~/.thunderbird/<profile>/calendar-data", None

    with tempfile.TemporaryDirectory() as t:
        tmp = os.path.join(t, 'calendar.sqlite')
        try:
            shutil.copy2(db_path, tmp)
        except Exception as e:
            return "--", 0, f"DB copy failed: {e}", db_path

        conn = sqlite3.connect(tmp)
        try:
            unit = detect_epoch_unit(conn)
            local_tz = tzlocal()
            now_local = datetime.now(local_tz)
            sod_local = datetime.combine(now_local.date(), time.min, tzinfo=local_tz)
            eod_local = datetime.combine(now_local.date(), time.max, tzinfo=local_tz)
            sod_utc = sod_local.astimezone(timezone.utc)

            exceptions = set()
            try:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cal_exceptions'")
                if cur.fetchone():
                    cur.execute("SELECT cal_id, recurrence_id FROM cal_exceptions WHERE is_negative = 1")
                    exceptions = {(r[0], r[1]) for r in cur.fetchall()}
            except sqlite3.Error:
                pass

            cur = conn.cursor()
            sql = (
                "SELECT e.id, e.cal_id, e.event_start, e.event_end, e.event_start_tz, p.value AS rrule "
                "FROM cal_events e "
                "LEFT JOIN cal_properties p ON p.item_id = e.id AND p.key = 'RRULE' "
                "WHERE (e.event_end IS NULL OR e.event_end >= ?)"
            )
            cur.execute(sql, (to_epoch(sod_utc, unit),))
            rows = cur.fetchall()
        finally:
            conn.close()

    todays, local_tz = [], tzlocal()
    for event_id, cal_id, start_raw, end_raw, tz_str, rrule in rows:
        event_tz = gettz(tz_str) or local_tz
        start_dt = from_epoch(start_raw, unit, event_tz)
        end_dt = from_epoch(end_raw, unit, event_tz) if end_raw else None
        if not start_dt:
            continue

        if rrule:
            try:
                rule = rrulestr(rrule, dtstart=start_dt)
                sod_local = datetime.combine(datetime.now(local_tz).date(), time.min, tzinfo=local_tz)
                eod_local = datetime.combine(datetime.now(local_tz).date(), time.max, tzinfo=local_tz)
                for inst in rule.between(sod_local, eod_local, inc=True):
                    inst_utc_val = to_epoch(inst.astimezone(timezone.utc), unit)
                    if (cal_id, inst_utc_val) not in exceptions:
                        todays.append(inst.astimezone(local_tz))
            except Exception:
                continue
        else:
            sod_local = datetime.combine(datetime.now(local_tz).date(), time.min, tzinfo=local_tz)
            eod_local = datetime.combine(datetime.now(local_tz).date(), time.max, tzinfo=local_tz)
            starts_today = sod_local <= start_dt.astimezone(local_tz) <= eod_local
            overlaps_today = end_dt and (
                start_dt.astimezone(local_tz) <= eod_local and end_dt.astimezone(local_tz) >= sod_local
            )
            if starts_today or overlaps_today:
                todays.append(start_dt.astimezone(local_tz))

    if not todays:
        return "--", 0, None, db_path

    todays.sort()
    nxt = next((dt.strftime("%H:%M") for dt in todays if dt >= datetime.now(tzlocal())), "--")
    return nxt, len(todays), None, db_path

if __name__ == "__main__":
    next_time, total_count, error_msg, db_used = get_meetings()
    tooltip = []
    if error_msg:
        text = "-- | 0"
        tooltip.append(f"Error: {error_msg}")
    else:
        text = f"{next_time} | {total_count}"
        tooltip.append(f"Next meeting: {next_time}")
        tooltip.append(f"Total today: {total_count}")
    if db_used:
        tooltip.append(f"DB: {db_used}")
    print(json.dumps({"text": text, "tooltip": "\\n".join(tooltip), "class": "meetings"}))

