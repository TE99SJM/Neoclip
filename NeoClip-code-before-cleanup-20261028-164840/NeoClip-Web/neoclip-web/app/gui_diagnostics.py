"""Per-container GUI diagnostics; disabled unless the host explicitly opts in."""

import os


def gui_diagnostics_enabled() -> bool:
    return os.getenv("NEOCLIP_TEST_DIAGNOSTICS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def configure_gui_diagnostics(html: str) -> str:
    enabled = gui_diagnostics_enabled()
    return html.replace(
        'data-test-diagnostics="false"',
        f'data-test-diagnostics="{str(enabled).lower()}"',
        1,
    ).replace(
        "<!-- test-gui-diagnostics -->",
        '<script src="/static/test-diagnostics.js"></script>' if enabled else "",
        1,
    )
