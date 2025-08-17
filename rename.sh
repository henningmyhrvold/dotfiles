#!/usr/bin/env bash

find . -type d -name "*henning*" -print0 | xargs -0 -I {} sh -c 'mv "$1" "${1/henning/youruser}"' sh {}
git grep -l henning | xargs sed -i 's/henning/youruser/g'
