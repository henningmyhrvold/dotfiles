# Dotfiles

Configuration and customization files to personalize Linux and macOS.
This repository contains my personal dotfiles

## Usage

**Change myuser name with youruser name**
```bash
find . -type d -name "*myuser*" -print0 | xargs -0 -I {} sh -c 'mv "$1" "${1/myuser/youruser}"' sh {}
git grep -l myuser | xargs sed -i 's/myuser/youruser/g'
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
