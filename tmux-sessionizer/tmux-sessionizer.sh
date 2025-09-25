#!/usr/bin/env bash
session="$(tmux display-message -p '#S')"
projdir="$PWD"

# 1) Editor (neutral)
tmux rename-window -t "$session:1" 'Neovim'
tmux send-keys   -t "$session:1" 'nvim .' C-m
tmux setw        -t "$session:1" @env 'editor'

# 2) Dev (green)
tmux new-window  -t "$session:2" -n 'dev' -c "$projdir"
tmux send-keys   -t "$session:2" 'export APP_ENV=development' C-m
tmux setw        -t "$session:2" @env 'dev'

# 3) Prod (red)
tmux new-window  -t "$session:3" -n 'prod' -c "$projdir"
tmux send-keys   -t "$session:3" 'export APP_ENV=production' C-m
tmux setw        -t "$session:3" @env 'prod'

tmux select-window -t "$session:1"

