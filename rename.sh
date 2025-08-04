#!/usr/bin/env bash

find . -type d -name "*myuser*" -print0 | xargs -0 -I {} sh -c 'mv "$1" "${1/myuser/hm}"' sh {}
git grep -l myuser | xargs sed -i 's/myuser/hm/g'
