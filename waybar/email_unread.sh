#!/bin/bash

THUNDERBIRD_HOME="$HOME/.thunderbird"

cd "$THUNDERBIRD_HOME" || exit

# Find all INBOX.msf files
MSF_FILES=$(find . -name 'INBOX.msf' -o -name 'INBOX-*.msf')

TOTAL=0

for file in $MSF_FILES; do
    # Get the last line with (^A1=
    LINE=$(grep '(^A1=' "$file" | tail -1)
    if [ -n "$LINE" ]; then
        # Extract the hex value after ^A1=
        HEX=$(echo "$LINE" | sed -r 's/.*\(\^A1=([0-9A-Fa-f]+)\).*/\1/')
        if [ -n "$HEX" ]; then
            # Convert hex to dec
            COUNT=$((0x$HEX))
            TOTAL=$((TOTAL + COUNT))
        fi
    fi
done

echo "$TOTAL"
