# Production 2.1.4-240

Promotes test snapshot `a87dd2e1ffd3a8a2109bbd46013ff5f83b048280` to `neoclip-240-stable` at the user's request. Application code matches that snapshot except for the production build label. This includes the compact UI, native controls, pull-to-refresh, source renaming, shared queue, folder clip counts and player diagnostics.

The repeated HLS track-reset issue is still present in this production snapshot. The continuous-encoding change is developed separately on `neoclip-240-test` and is not included here.

Deploy the canonical `NeoClip-code-before-cleanup-20261028-164840/NeoClip-Web/neoclip-web/app` to the existing main build context. Keep its Compose file, mounts, exports, `/data`, and shared queue configuration. Rebuild and recreate only the `neoclip` service after any active render finishes. The expected `/api/state` build is `2.1.4-240`.
