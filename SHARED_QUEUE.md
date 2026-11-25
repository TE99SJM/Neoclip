# Main and test clip queue

Both apps can use the main server's `data/neoclip_queue.json`. Each app still
uses its own `/data` directory for proxy jobs and derived metadata. Set the test
container's `NEOCLIP_QUEUE_FILE=/main-data/neoclip_queue.json` and bind the main
data directory at `/main-data`. Mount the directory, not the individual JSON:
queue writes atomically replace that file.

Both writers must run this update before sharing the file. The queue store uses
an adjacent `flock` file, stable clip IDs, and merges only the changes made by
the current writer. A stale session save cannot restore a clip removed or
rendered in the other app. Library polling refreshes the visible queue and folder
counts without touching the active video. Each folder counts descendant videos
with queued clips once, irrespective of the number of clips per video.

The source selection stays local to each running app. Clip and render requests
include their source path; Remove includes the clip ID so an outdated row index
cannot delete a different clip. A shared render lease prevents simultaneous
renders in main and test. The test branch's rename support moves the latest
shared queue and redirects subsequent requests using the old filename.

`scripts/deploy_shared_queue.py --main-ref COMMIT --test-ref COMMIT` configures
Samuel's existing containers. It builds main before stopping the containers,
merges existing test clips once (keeping main's existing editor marks), updates
the test bind mount/configuration, and checks both running apps. It refuses to
interrupt an active render or discard local test checkout changes. It creates
no backups and leaves each app's existing export mount in place.

The main update is based on the requested v2.1.0 rollback. Test retains the newer
player and rename features. Its cached HLS segments are served from local disk
without checking the NAS on each request; missing segments still validate the
source, and proxy expiry/deletion rules still apply. This addresses one preview
failure path; physical iPhone lock/wake behavior still needs device verification.

Validation includes concurrent process writes, two independent API instances,
stable-ID removal, shared render exclusion, migration, queue refresh, folder
counts, NAS-offline cached playback, and existing FFmpeg integration tests.
