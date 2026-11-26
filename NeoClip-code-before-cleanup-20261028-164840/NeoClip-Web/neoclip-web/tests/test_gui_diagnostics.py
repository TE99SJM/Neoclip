import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.gui_diagnostics import configure_gui_diagnostics, gui_diagnostics_enabled

HTML = '<html data-test-diagnostics="false"><!-- test-gui-diagnostics --></html>'


@pytest.mark.parametrize("value", [None, "", "0", "false", "off", "no", "test", "8001"])
def test_gui_defaults_off_without_explicit_host_flag(monkeypatch, value):
    monkeypatch.delenv("NEOCLIP_TEST_DIAGNOSTICS", raising=False)
    if value is not None:
        monkeypatch.setenv("NEOCLIP_TEST_DIAGNOSTICS", value)
    assert not gui_diagnostics_enabled()
    result = configure_gui_diagnostics(HTML)
    assert 'data-test-diagnostics="false"' in result
    assert "test-diagnostics.js" not in result
    assert "<!--" not in result


@pytest.mark.parametrize("value", ["1", "true", " yes ", "ON"])
def test_gui_enabled_by_each_containers_own_environment(monkeypatch, value):
    monkeypatch.setenv("NEOCLIP_TEST_DIAGNOSTICS", value)
    assert gui_diagnostics_enabled()
    result = configure_gui_diagnostics(HTML)
    assert 'data-test-diagnostics="true"' in result
    assert result.count("/static/test-diagnostics.js") == 1
    monkeypatch.delenv("NEOCLIP_TEST_DIAGNOSTICS")
    assert "test-diagnostics.js" not in configure_gui_diagnostics(HTML)


def test_real_home_route_and_status_use_the_server_setting(tmp_path, monkeypatch):
    for name in ("MEDIA_ROOT", "EXPORT_ROOT", "DATA_ROOT"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    main = importlib.import_module("app.main")
    # No lifespan needed: this test serves HTML without starting encoders/scans.
    client = TestClient(main.app)
    for value, expected in [("0", False), ("1", True), ("0", False)]:
        monkeypatch.setenv("NEOCLIP_TEST_DIAGNOSTICS", value)
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert ("/static/test-diagnostics.js" in page.text) == expected
        assert f'data-test-diagnostics="{str(expected).lower()}"' in page.text
        assert client.get("/api/status").json()["test_diagnostics"] is expected
    assert client.get("/static/test-diagnostics.js").status_code == 200
    assert (
        "<!-- test-gui-diagnostics -->"
        in (Path(main.WEB_ROOT) / "index.html").read_text()
    )
