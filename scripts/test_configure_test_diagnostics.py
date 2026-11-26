import json
import stat
from copy import deepcopy

import pytest
from configure_test_diagnostics import FLAG, configure


@pytest.mark.parametrize(
    "environment",
    [
        {"NEOCLIP_QUEUE_FILE": "/main-data/neoclip_queue.json"},
        ["NEOCLIP_QUEUE_FILE=/main-data/neoclip_queue.json", "INHERITED", f"{FLAG}=0"],
        {},
    ],
)
def test_only_test_environment_changes_and_setting_is_repeatable(tmp_path, environment):
    path = tmp_path / "compose.json"
    config = {
        "name": "neoclip-test-server",
        "services": {
            "neoclip-test": {
                "container_name": "neoclip-test-server",
                "environment": environment,
                "ports": ["8001:8000"],
                "volumes": ["/data:/main-data", "/repo/app:/app/app:ro"],
            },
            "another-service": {"environment": {"KEEP": "unchanged"}},
        },
    }
    path.write_text(json.dumps(config))
    path.chmod(0o640)
    original = deepcopy(config)
    configure(path, True)
    first = path.read_text()
    configure(path, True)
    assert path.read_text() == first
    updated = json.loads(first)
    result_env = updated["services"]["neoclip-test"]["environment"]
    assert (
        result_env[FLAG] == "1"
        if isinstance(result_env, dict)
        else f"{FLAG}=1" in result_env
    )
    updated["services"]["neoclip-test"]["environment"] = original["services"][
        "neoclip-test"
    ]["environment"]
    assert updated == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    configure(path, False)
    result = json.loads(path.read_text())["services"]["neoclip-test"]["environment"]
    assert result[FLAG] == "0" if isinstance(result, dict) else f"{FLAG}=0" in result
    assert list(tmp_path.iterdir()) == [path], "no backups or temporary files remain"


@pytest.mark.parametrize(
    "services",
    [
        {"neoclip": {"container_name": "neoclip-web"}},
        {"neoclip-test": {"container_name": "neoclip-web"}},
        {
            "neoclip-test": {
                "container_name": "neoclip-test-server",
                "environment": "invalid",
            }
        },
    ],
)
def test_rejects_main_or_invalid_config_without_writing(tmp_path, services):
    path = tmp_path / "compose.json"
    original = json.dumps({"services": services})
    path.write_text(original)
    with pytest.raises(ValueError):
        configure(path, True)
    assert path.read_text() == original
