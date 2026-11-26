"""Persist the test GUI flag in the host Compose file, outside the git checkout."""

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path

FLAG = "NEOCLIP_TEST_DIAGNOSTICS"


def configure(path: Path, enabled: bool) -> None:
    config = json.loads(path.read_text())
    service = config.get("services", {}).get("neoclip-test")
    if (
        not isinstance(service, dict)
        or service.get("container_name") != "neoclip-test-server"
    ):
        raise ValueError(
            "Expected service neoclip-test with container neoclip-test-server; no changes made"
        )
    value = "1" if enabled else "0"
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        environment[FLAG] = value
    elif isinstance(environment, list) and all(
        isinstance(item, str) for item in environment
    ):
        environment = [item for item in environment if item.split("=", 1)[0] != FLAG]
        environment.append(f"{FLAG}={value}")
    else:
        raise ValueError("Unsupported Compose environment format; no changes made")
    service["environment"] = environment
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(json.dumps(config, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    try:
        configure(args.compose, args.enable)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"{error}\n")
    print(
        f"Test GUI diagnostics {'enabled' if args.enable else 'disabled'} in {args.compose}"
    )
    print("Recreate neoclip-test to apply the setting.")


if __name__ == "__main__":
    main()
