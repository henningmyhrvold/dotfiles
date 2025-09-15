# Dotfiles

Configuration and customization files to personalize Linux and macOS.
This repository contains my personal dotfiles

## Usage

**Change the henning username with youruser**
```bash
find . -type d -name "*henning*" -print0 | xargs -0 -I {} sh -c 'mv "$1" "${1/henning/youruser}"' sh {}
git grep -l henning | xargs sed -i 's/henning/youruser/g'
```


## Repository Structure

List of folders and dotfiles in them:

- `cool-retro-term/` - Cool Retro Term
- `dunst/` - Notifications
- `fonts/` - Fonts
- `ghostty/` - Terminal
- `hypr/` - Hyprland Compositor
- `nvim/` - Neovim
- `sddm/` - Login manager
- `tmux` - tmux Terminal Multiplexer
- `tmux-sessionizer` - tmux Sessionizer Plugin
- `wallpaper/` - Wallpaper
- `waybar/` - Waybar
- `wezterm/` - Wezterm
- `wofi/` - App Launcher
- `zsh/` - zsh Shell


## How Dotfiles are Managed

There are many ways to manage your dotfiles. I use an Ansible playbook

## Using Thunderbird

You need to login to an email account and has a calendar that you use. On the calendar, right click on the calendar name, then click properties. Make sure the offline support is checked. 
