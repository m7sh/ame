"""
theme.py - Omarchy Desktop & Terminal Theme Sync Engine.

Detects, parses, and hot-reloads colors from Omarchy Linux theme state
(~/.local/state/omarchy/current/theme/colors.toml) and generates matching Textual TCSS.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import tomllib  # Python 3.11+ standard library
except ImportError:
    import tomli as tomllib  # type: ignore


@dataclass
class ThemeColors:
    name: str = "Cyberpunk Dracula"
    mode: str = "dark"
    background: str = "#121212"
    panel: str = "#18181e"
    surface: str = "#1e1e24"
    surface_hover: str = "#282a36"
    surface_active: str = "#35374a"
    border: str = "#27272a"
    foreground: str = "#f8f8f2"
    muted: str = "#6272a4"
    accent: str = "#00e5ff"
    primary: str = "#00e5ff"
    secondary: str = "#bd93f9"
    success: str = "#50fa7b"
    warning: str = "#f1fa8c"
    danger: str = "#ff5555"
    blue: str = "#8be9fd"


class OmarchyThemeManager:
    """Manages Omarchy theme detection, color extraction, and live CSS generation."""

    def __init__(self):
        self.state_dir = Path.home() / ".local/state/omarchy/current/theme"
        self.colors_toml = self.state_dir / "colors.toml"
        self._last_mtime: float = 0.0
        self.current_theme: ThemeColors = self.load_theme()

    def get_theme_name(self) -> str:
        """Fetch human-readable current Omarchy theme name."""
        # Try `omarchy theme current` command first
        try:
            res = subprocess.run(
                ["omarchy", "theme", "current"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

        # Check README.md or preview in state dir
        readme = self.state_dir / "README.md"
        if readme.exists():
            try:
                first_line = readme.read_text(encoding="utf-8").split("\n")[0]
                name = first_line.replace("#", "").strip()
                if name:
                    return name
            except Exception:
                pass

        return "Omarchy Theme"

    def load_theme(self) -> ThemeColors:
        """Load color definitions from active Omarchy theme colors.toml or fall back."""
        if not self.colors_toml.exists():
            # Alternative path check
            alt_path = Path.home() / ".config/omarchy/current/theme/colors.toml"
            if alt_path.exists():
                self.colors_toml = alt_path

        if self.colors_toml.exists():
            try:
                self._last_mtime = self.colors_toml.stat().st_mtime
                with open(self.colors_toml, "rb") as f:
                    data = tomllib.load(f)

                theme_name = self.get_theme_name()
                mode = data.get("mode", "dark")

                bg = data.get("background", "#121212")
                dark_bg = data.get("dark_background") or data.get("darker_background") or bg
                lighter_bg = data.get("lighter_background") or data.get("surface") or bg
                fg = data.get("foreground", "#f8f8f2")
                muted = data.get("muted") or data.get("dark_foreground", "#6272a4")
                accent = data.get("accent", "#00e5ff")
                cyan = data.get("cyan", accent)
                magenta = data.get("magenta", "#bd93f9")
                selection = data.get("selection") or data.get("selection_background", "#27272a")

                green = data.get("green", "#50fa7b")
                yellow = data.get("yellow") or data.get("orange", "#f1fa8c")
                red = data.get("red") or data.get("focus_red", "#ff5555")
                blue = data.get("blue", cyan)

                return ThemeColors(
                    name=theme_name,
                    mode=mode,
                    background=bg,
                    panel=dark_bg,
                    surface=lighter_bg,
                    surface_hover=selection,
                    surface_active=data.get("color0", selection),
                    border=selection,
                    foreground=fg,
                    muted=muted,
                    accent=accent,
                    primary=cyan,
                    secondary=magenta,
                    success=green,
                    warning=yellow,
                    danger=red,
                    blue=blue,
                )
            except Exception as e:
                pass

        # Fallback default Dark Cyberpunk
        return ThemeColors()

    def has_theme_changed(self) -> bool:
        """Check if Omarchy theme colors.toml modification time has changed."""
        if self.colors_toml.exists():
            try:
                mtime = self.colors_toml.stat().st_mtime
                if mtime > self._last_mtime:
                    return True
            except OSError:
                pass
        return False

    def reload(self) -> Optional[ThemeColors]:
        """Reload theme if changed. Returns new ThemeColors or None."""
        if self.has_theme_changed():
            self.current_theme = self.load_theme()
            return self.current_theme
        return None

    def generate_tcss(self, theme: Optional[ThemeColors] = None) -> str:
        """Generate dynamic Textual TCSS using active theme palette."""
        t = theme or self.current_theme

        return f"""/* Auto-generated Omarchy Synced Theme: {t.name} ({t.mode}) */

Screen {{
    background: {t.background};
    color: {t.foreground};
    layout: vertical;
    overflow: hidden;
}}

/* Header Bar */
HeaderBar {{
    height: 3;
    dock: top;
    background: {t.panel};
    border-bottom: heavy {t.border};
    padding: 0 1;
}}

#header_container {{
    width: 100%;
    height: 100%;
    align: left middle;
}}

#header_logo {{
    width: auto;
    padding-right: 2;
}}

#header_view_indicator {{
    width: 1fr;
    text-align: left;
}}

#header_badges {{
    width: auto;
    align: right middle;
}}

.badge {{
    margin-left: 1;
    padding: 0 1;
    background: {t.background};
    border: round {t.border};
}}

/* Main Body Layout */
#main_body_container {{
    height: 1fr;
    width: 100%;
    layout: horizontal;
    background: {t.background};
}}

/* Sidebar Navigation (~25%) */
SidebarNav {{
    width: 25%;
    min-width: 26;
    max-width: 38;
    height: 100%;
    background: {t.panel};
    border-right: heavy {t.border};
}}

#sidebar_container {{
    height: 100%;
    width: 100%;
    padding: 1 1 0 1;
}}

.nav_section_title {{
    height: 1;
    margin-top: 1;
    margin-bottom: 0;
    text-style: bold;
    color: {t.secondary};
}}

#nav_list {{
    height: auto;
    margin-bottom: 1;
    background: transparent;
    border: none;
}}

#nav_list > ListItem {{
    padding: 0 1;
    height: 2;
    background: transparent;
    color: {t.foreground};
}}

#nav_list > ListItem:hover {{
    background: {t.surface_hover};
    color: {t.primary};
}}

#nav_list > ListItem.-selected {{
    background: {t.surface_hover};
    color: {t.primary};
    text-style: bold;
    border-left: wide {t.primary};
}}

#sidebar_help {{
    height: 1fr;
    border-top: solid {t.border};
    padding-top: 1;
}}

.help_row {{
    height: 1;
    color: {t.muted};
    padding-left: 1;
}}

/* Content Area (~75%) */
#content_switcher {{
    width: 1fr;
    height: 100%;
    background: {t.background};
    padding: 0 1;
}}

.view_content_container {{
    height: 100%;
    width: 100%;
    layout: vertical;
}}

.view_banner {{
    height: 2;
    padding: 0 1;
    background: {t.panel};
    border-bottom: solid {t.border};
    margin-bottom: 1;
    content-align: left middle;
}}

/* Sticky Bottom Player Bar */
BottomPlayerBar {{
    height: 6;
    dock: bottom;
    background: {t.panel};
    border-top: heavy {t.border};
    padding: 0 1;
}}

#player_bar_container {{
    height: 100%;
    width: 100%;
    layout: vertical;
}}

#player_track_row {{
    height: 1;
    width: 100%;
    align: left middle;
}}

#player_title_info {{
    width: 1fr;
    text-overflow: ellipsis;
}}

#player_meta_badges {{
    width: auto;
    align: right middle;
}}

#player_quality_badge {{
    margin-right: 1;
    padding: 0 1;
    background: {t.background};
    border: round {t.border};
}}

#player_autoplay_badge {{
    padding: 0 1;
    background: {t.background};
    border: round {t.border};
}}

#player_seek_container {{
    height: 1;
    width: 100%;
    margin: 1 0 0 0;
    align: center middle;
}}

#player_seek_bar {{
    width: 100%;
    content-align: center middle;
}}

#player_controls_row {{
    height: 1;
    width: 100%;
    align: left middle;
    margin-top: 1;
}}

#player_state_badge {{
    width: auto;
    padding-right: 2;
}}

#player_volume_badge {{
    width: auto;
    padding-right: 2;
}}

#player_queue_badge {{
    width: auto;
    padding-right: 2;
}}

#player_hotkeys_hint {{
    width: 1fr;
    text-align: right;
    color: {t.muted};
}}

/* Data Tables */
DataTable {{
    background: {t.background};
    border: solid {t.border};
    height: 1fr;
}}

DataTable > .datatable--header {{
    background: {t.panel};
    color: {t.primary};
    text-style: bold;
}}

DataTable > .datatable--cursor {{
    background: {t.surface_hover};
    color: {t.accent};
    text-style: bold;
}}

DataTable > .datatable--hover {{
    background: {t.surface};
}}

/* Cards & Containers */
#radio_seed_card, #playlist_header_card, #queue_header_card {{
    height: auto;
    background: {t.panel};
    border: solid {t.border};
    padding: 1 2;
    margin-bottom: 1;
}}

#radio_action_buttons, #pl_detail_buttons, #queue_action_buttons {{
    height: 3;
    margin-top: 1;
    align: left middle;
}}

#radio_action_buttons > Button, #pl_detail_buttons > Button, #queue_action_buttons > Button {{
    margin-right: 2;
}}

/* Moods & Playlists Split Pane */
#moods_split_container {{
    height: 1fr;
    width: 100%;
    layout: horizontal;
}}

#moods_left_pane {{
    width: 32%;
    height: 100%;
    background: {t.panel};
    border: solid {t.border};
    padding: 0 1;
    margin-right: 1;
}}

#moods_right_pane {{
    width: 68%;
    height: 100%;
    layout: vertical;
}}

.pane_title {{
    height: 2;
    padding: 0 1;
    border-bottom: solid {t.border};
    content-align: left middle;
    text-style: bold;
}}

#categories_list {{
    height: 1fr;
    background: transparent;
    border: none;
}}

#categories_list > ListItem {{
    padding: 0 1;
    height: 2;
    color: {t.foreground};
}}

#categories_list > ListItem:hover {{
    background: {t.surface_hover};
    color: {t.primary};
}}

#categories_list > ListItem.-selected {{
    background: {t.surface_hover};
    color: {t.primary};
    text-style: bold;
    border-left: wide {t.secondary};
}}

/* Search Box */
#search_bar_container {{
    height: 4;
    margin-bottom: 1;
    background: {t.panel};
    border: solid {t.border};
    padding: 0 1;
    align: left middle;
}}

#search_input {{
    width: 100%;
    background: {t.background};
    border: solid {t.border};
    color: {t.foreground};
}}

#search_input:focus {{
    border: solid {t.primary};
}}

/* Buttons */
Button {{
    background: {t.surface};
    color: {t.foreground};
    border: solid {t.border};
    height: 3;
    padding: 0 2;
}}

Button:hover {{
    background: {t.surface_hover};
    border: solid {t.primary};
}}

Button.-primary {{
    background: {t.primary};
    color: {t.background};
    border: solid {t.primary};
    text-style: bold;
}}

Button.-primary:hover {{
    background: {t.accent};
    color: {t.background};
}}

Button.-error {{
    background: {t.danger};
    color: {t.background};
    border: solid {t.danger};
    text-style: bold;
}}

Button.-error:hover {{
    background: {t.danger};
}}

/* Tabs */
TabbedContent {{
    height: 1fr;
    background: transparent;
}}

Tabs {{
    background: {t.panel};
    border-bottom: solid {t.border};
}}

Tab {{
    color: {t.muted};
    padding: 0 2;
}}

Tab.-active {{
    color: {t.primary};
    text-style: bold;
    border-bottom: wide {t.primary};
}}
"""
