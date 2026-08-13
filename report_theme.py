"""Helpers for persistent light/dark theme support in static HTML reports."""

from __future__ import annotations


THEME_STORAGE_KEY = "jellyfin-library-auditor-theme"


def render_theme_bootstrap_script() -> str:
    """Return an inline script that applies the saved theme before page paint."""
    return (
        "<script>"
        "(function(){"
        f"const key='{THEME_STORAGE_KEY}';"
        "let storedTheme=null;"
        "try{storedTheme=window.localStorage.getItem(key);}catch(error){}"
        "const prefersDark=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;"
        "const theme=storedTheme==='dark'||storedTheme==='light'?storedTheme:(prefersDark?'dark':'light');"
        "document.documentElement.dataset.theme=theme;"
        "document.documentElement.style.colorScheme=theme;"
        "})();"
        "</script>"
    )


def render_theme_toggle() -> str:
    """Return the shared light/dark theme toggle control markup."""
    return (
        '<div class="theme-toggle" aria-label="Color theme">'
        '<label class="theme-toggle-control" for="theme-toggle">'
        "<span>Dark mode</span>"
        '<input id="theme-toggle" type="checkbox" role="switch" aria-label="Enable dark mode">'
        "</label>"
        '<span class="theme-toggle-value" id="theme-toggle-value">Light</span>'
        "</div>"
    )
