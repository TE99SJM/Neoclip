# Contributing to NeoClip

Thanks for your interest in NeoClip. The project is in an early public stage and help with documentation, bug reports, testing, and code review is welcome.

> **License:** NeoClip is licensed under the [GNU AGPLv3](LICENSE). By submitting a contribution, you confirm you have the right to contribute it and agree that it is licensed under the AGPLv3, the same as the rest of NeoClip. Open an issue before undertaking significant changes so you can coordinate with the maintainer.

## Report a problem

Use the [bug report template](https://github.com/TE99SJM/Neoclip/issues/new/choose). Include:

- The steps that reproduce the issue.
- The expected and actual outcomes.
- Your OS, Python version, FFmpeg version, and browser.
- Whether you are running directly in Python or in a container.
- Logs with secrets, private paths, and identifying media filenames redacted.

Do not upload private source videos or expose a public NeoClip instance in an issue.

## Request a feature

Use the feature request template. Describe the problem you are trying to solve and how you would expect the feature to behave. Screenshots or mockups are welcome, but are not required.

## Proposing changes

1. Open an issue first for substantial changes.
2. Create a branch with a descriptive name.
3. Keep the change focused and include any relevant documentation.
4. Run the tests that apply to the component you changed.
5. Open a pull request describing what changed, how you tested it, and any known limitations.

The code currently lives under a dated backup directory. Check the current repository layout before writing instructions that refer to stable file paths.

## Local tests

From the `NeoClip-Web/neoclip-web` app directory referenced in the README:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx ruff
python -m pytest
python -m ruff check app tests
```

Some tests use FFmpeg and sample files and may require additional local setup. Record failures and your environment in your pull request; do not claim that a test passed if it did not run.

## Safety

NeoClip currently has no user authentication. Do not put it on the open internet or test against shared media you cannot afford to lose. See [SECURITY.md](SECURITY.md).
