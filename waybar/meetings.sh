#!/bin/bash
# Detect the default Thunderbird profile
PROFILE=$(
awk '
BEGIN { default_path = "" ; install_default = "" }
/^\[Install/ { in_install = 1 }
/^Default=/ { 
  if (in_install) { install_default = substr($0, index($0,"=")+1) } 
  else if (in_profile) { if (substr($0, index($0,"=")+1) == "1") is_default = 1 }
}
/^Path=/ { if (in_profile) path = substr($0, index($0,"=")+1) }
/^\[/ { 
  if (in_profile && is_default) { default_path = path } 
  in_profile = 0 ; in_install = 0 
}
/^\[Profile/ { in_profile = 1 ; path = "" ; is_default = 0 }
END { print (length(install_default) > 0 ? install_default : default_path) }
' ~/.thunderbird/profiles.ini
)
# Path to the calendar database
DB="$HOME/.thunderbird/$PROFILE/calendar-data/local.sqlite"
# Query for start times of events overlapping today (in local time), sorted by start
EVENT_STARTS=$(sqlite3 "$DB" "
SELECT datetime(event_start / 1000000, 'unixepoch', 'localtime')
FROM cal_events
WHERE (event_end / 1000000 > strftime('%s', 'now', 'localtime', 'start of day'))
  AND (event_start / 1000000 < strftime('%s', 'now', 'localtime', 'start of day', '+1 day'))
ORDER BY event_start;
")
# If no events, output default
if [ -z "$EVENT_STARTS" ]; then
    echo "(-- | 0)"
    exit 0
fi
# Count total events
TOTAL=$(echo "$EVENT_STARTS" | wc -l)
# Current time in seconds
NOW=$(date +%s)
# Find the next event start time
NEXT=""
while IFS= read -r start; do
    START_S=$(date -d "$start" +%s)
    if [ "$START_S" -ge "$NOW" ]; then
        NEXT=$(date -d "$start" +%H:%M)
        break
    fi
done <<< "$EVENT_STARTS"
# If no upcoming event, use placeholder
if [ -z "$NEXT" ]; then
    NEXT="--"
fi
# Output in the desired format
echo "($NEXT | $TOTAL)"
