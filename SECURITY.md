# Security policy

## Current status

NeoClip is an early-stage, self-hosted application. The checked-in API currently **does not implement user authentication or access control**.

Among other operations, the web interface can browse media under the configured `MEDIA_ROOT` and invoke a source-video deletion endpoint when the backing files are writable. The app also provides editing and rendering endpoints.

**Do not expose the app directly to the public internet.** Treat it as a trusted-local-use tool unless and until stronger authentication, authorization, and deployment safeguards are implemented and reviewed.

## Safer local evaluation

- Bind the development server to `127.0.0.1` rather than `0.0.0.0` when access is only needed on the same machine.
- Use test media or a disposable copy of your recordings.
- Keep private media, exports, and server data outside the Git repository.
- Do not open port 8000 to the public internet.
- Do not share logs that contain private file paths or filenames.
- Keep Python, FFmpeg, and other dependencies up to date.

Do not assume that read-only source mounts make the entire application safe for public access.

## Report a security issue

Please **do not** publish working exploits, private media, tokens, or other sensitive information in a public issue.

If GitHub private vulnerability reporting is enabled for this repository, use the **Report a vulnerability** option on the repository's Security tab. If that option is not available, contact the maintainer through an existing private contact channel before sharing sensitive technical details. Public issues are suitable for general hardening ideas that do not expose exploitable details.

No formal security support or response-time commitment is currently offered.
