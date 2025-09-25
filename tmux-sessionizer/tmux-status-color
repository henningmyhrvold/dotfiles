#!/usr/bin/env bash
# Decide colors based on @env window option
env_tag="$(tmux display-message -p -F "#{window_active} #{window_index} #{window_name} #{@env}" | awk 'NR==1{print $4}')"

# Defaults
bg="#333333"  # editor/neutral grey
fg="#5eacd3"

case "$env_tag" in
  dev)  bg="#22543d" ;;  # green-ish
  prod) bg="#7f1d1d" ;;  # red-ish
  *)    bg="#333333" ;;  # editor / anything else
esac

tmux set -g status-style "bg=$bg,fg=$fg"

