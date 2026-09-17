"""
theme.py - Omarchy Desktop & Terminal Theme Sync Engine.

Detects, parses, and hot-reloads colors from the active Omarchy Linux theme
(~/.local/state/omarchy/current/theme/colors.toml) and exposes them as a
native Textual ``Theme`` (with custom CSS variables), so the whole UI can be
restyled live without ever touching a stylesheet on disk.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict

from textual.theme import Theme

try:
    import tomllib  # Python 3.11+ standard library
except ImportError:
    import tomli as tomllib  # type: ignore


@dataclass
class ThemeColors:
    """Normalized palette extracted from an Omarchy theme."""

    name: str = "Omarchy"
    mode: str = "dark"
    background: str = "#121212"
    panel: str = "#18181e"
    surface: str = "#1e1e24"
    surface_hover: str = "#282a36"
    surface_active: str = "#35374a"
    border: str = "#2a2a32"
    foreground: str = "#f8f8f2"
    muted: str = "#6b7280"
    accent: str = "#00e5ff"
    primary: str = "#00e5ff"
    secondary: str = "#bd93f9"
    success: str = "#50fa7b"
    warning: str = "#f1fa8c"
    danger: str = "#ff5555"
    blue: str = "#8be9fd"


class OmarchyThemeManager:
    """Manages Omarchy theme detection, color extraction and Textual theming."""

    def __init__(self) -> None:
        self.state_dir = Path.home() / ".local/state/omarchy/current/theme"
        self.colors_toml = self.state_dir / "colors.toml"
        self._last_mtime: float = 0.0
        self.current_theme: ThemeColors = self.load_theme()

    def get_theme_name(self) -> str:
        """Fetch the human-readable current Omarchy theme name."""
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

        readme = self.state_dir / "README.md"
        if readme.exists():
            try:
                first_line = readme.read_text(encoding="utf-8").split("\n")[0]
                name = first_line.replace("#", "").strip()
                if name:
                    return name
            except Exception:
                pass

        return "Omarchy"

    def load_theme(self) -> ThemeColors:
        """Load color definitions from the active Omarchy colors.toml or fall back."""
        if not self.colors_toml.exists():
            alt_path = Path.home() / ".config/omarchy/current/theme/colors.toml"
            if alt_path.exists():
                self.colors_toml = alt_path

        if self.colors_toml.exists():
            try:
                self._last_mtime = self.colors_toml.stat().st_mtime
                with open(self.colors_toml, "rb") as f:
                    data: Dict[str, str] = tomllib.load(f)

                bg = data.get("background", "#121212")
                dark_bg = data.get("dark_background") or data.get("darker_background") or bg
                lighter_bg = data.get("lighter_background") or data.get("surface") or bg
                fg = data.get("foreground", "#f8f8f2")
                muted = data.get("muted") or data.get("dark_foreground", "#6b7280")
                accent = data.get("accent", "#00e5ff")
                cyan = data.get("cyan", accent)
                magenta = data.get("magenta", "#bd93f9")
                selection = data.get("selection") or data.get("selection_background", "#2a2a32")

                return ThemeColors(
                    name=self.get_theme_name(),
                    mode=data.get("mode", "dark"),
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
                    success=data.get("green", "#50fa7b"),
                    warning=data.get("yellow") or data.get("orange", "#f1fa8c"),
                    danger=data.get("red") or data.get("focus_red", "#ff5555"),
                    blue=data.get("blue", cyan),
                )
            except Exception:
                pass

        return ThemeColors()

    def has_theme_changed(self) -> bool:
        """Return True if the Omarchy colors.toml changed on disk."""
        if self.colors_toml.exists():
            try:
                return self.colors_toml.stat().st_mtime > self._last_mtime
            except OSError:
                pass
        return False

    def reload(self) -> Optional[ThemeColors]:
        """Reload and return the theme if it changed, otherwise None."""
        if self.has_theme_changed():
            self.current_theme = self.load_theme()
            return self.current_theme
        return None

    @staticmethod
    def build_textual_theme(colors: ThemeColors, version: int = 0) -> Theme:
        """Compile a palette into a native Textual Theme with custom variables."""
        return Theme(
            name=f"omarchy-synced-{version}",
            primary=colors.primary,
            secondary=colors.secondary,
            accent=colors.accent,
            foreground=colors.foreground,
            background=colors.background,
            surface=colors.surface,
            panel=colors.panel,
            warning=colors.warning,
            error=colors.danger,
            success=colors.success,
            dark=colors.mode != "light",
            variables={
                "ame-bg": colors.background,
                "ame-panel": colors.panel,
                "ame-surface": colors.surface,
                "ame-hover": colors.surface_hover,
                "ame-active": colors.surface_active,
                "ame-border": colors.border,
                "ame-fg": colors.foreground,
                "ame-muted": colors.muted,
                "ame-accent": colors.accent,
                "ame-primary": colors.primary,
                "ame-secondary": colors.secondary,
                "ame-success": colors.success,
                "ame-warning": colors.warning,
                "ame-danger": colors.danger,
                "ame-blue": colors.blue,
            },
        )
