# Temporary diagnostics controlled by each server

## Requirement
Show a temporary log and compact playback, proxy and error status at the top of the test app. The setting must survive GitHub updates and must not follow application code when it is promoted to the main server.

## Spec and acceptance checks
- `NEOCLIP_TEST_DIAGNOSTICS` enables the panel only when explicitly true (`1`, `true`, `yes` or `on`, case-insensitive). Default is disabled regardless of branch, build or port.
- The HTML response declares the server's setting and includes the diagnostic script only when enabled. Disabled instances have no panel, log listeners, timers or diagnostic network calls from this feature. HTML remains no-store.
- Enabled instances show a compact panel before the version/player. It observes playback events, current proxy build/progress and application error messages. Latest 100 bounded text entries stay in browser memory only, with Copy and Clear actions. No persistent queue/cache/log file and no changes to media playback.
- The deployment setting lives in `~/neoclip-test-server/compose.json`, outside `repo/`. The setup helper changes only the diagnostic environment variable on the verified `neoclip-test` service/container. It preserves all other settings, mounts and shared queue configuration, is repeatable, and supports disable.
- Same application code on main has no extra UI unless its own environment explicitly enables diagnostics. Ordinary user-facing errors remain available.

## Tickets
1. Add server flag and gated read-only GUI panel.
2. Add a narrowly scoped host Compose configuration helper, dependent on 1.
3. Verify enabled/default-off behavior, bounded safe rendering, no playback writes, configuration preservation/idempotency and existing UI regressions; publish only the playback branch.

## Operations
Set the local test service's environment variable to `1` and recreate only `neoclip-test`. Git checkout/pull changes do not modify the parent compose.json. Set it to `0` to disable later. A complete replacement of that local Compose file would need to retain the setting.

## Actual verification
- 20 canonical and 17 outer Node suites passed, including existing playback, timeline, recovery, queue, seeking and pull-to-refresh checks available in each app copy.
- 15 canonical Python checks passed for the new server flag/home route and existing player diagnostic API; 6 host configuration checks passed for enable/disable, preservation, repeatability and rejecting the main service/invalid input.
- New GUI harness verifies default-off behavior, panel placement, proxy/error reporting, deduplication, bounded text-only entries, clipboard fallback, visibility timer lifecycle, disposal, no added network requests and no playback property/method writes.
- Repository-configured Ruff passed on modified Python app/test files; the same selected rules passed on the host helper/tests. git diff --check passed.
- Reviewed the full change against this spec. No encoder, queue storage, native player controls or main-server deployment changes.
- Not exercised on a physical iPhone or the user's mserver. Server installation and device appearance remain to be checked there.
