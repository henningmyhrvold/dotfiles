#!/bin/bash

# Detect the default Thunderbird profile
PROFILE=$(sed -n '/\[Profile0\]/,/^$/p' ~/.thunderbird/profiles.ini | grep 'Path=' | cut -d= -f2)

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
