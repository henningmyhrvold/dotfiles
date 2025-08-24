#!/bin/bash

THUNDERBIRD_HOME="$HOME/.thunderbird"

cd "$THUNDERBIRD_HOME" || exit

# Find all INBOX.msf files
MSF_FILES=$(find . -name 'INBOX.msf' -o -name 'INBOX-*.msf')

TOTAL=0

for file in $MSF_FILES; do
    # Get the last line with (^A2=
    LINE=$(grep '(^A2=' "$file" | tail -1)
    if [ -n "$LINE" ]; then
        # Extract the hex value after ^A2=
        HEX=$(echo "$LINE" | sed -r 's/.*\(\^A2=([0-9A-Fa-f]+)\).*/\1/')
        if [ -n "$HEX" ]; then
            # Convert hex to dec
            COUNT=$((0x$HEX))
            TOTAL=$((TOTAL + COUNT))
        fi
    fi
done

echo "$TOTAL"
