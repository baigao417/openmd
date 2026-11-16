# OpenCLI transcript integration

Source and built runtime for the optional `xiaoe-transcript` OpenCLI plugin.
Openmd's public core owns this integration. It is distinct from Python platform
plugins discovered through `openmd.platforms`.

## Build and inspect

```sh
npm ci
npm run build
npm test -- --maxWorkers=1 --no-file-parallelism
```

The OpenCLI manifest loads `xiaoe-transcript.js`, which dynamically imports
`dist/src/`. Keep TypeScript sources and generated runtime together. The UI
assets are loaded from `src/web/get-workbench`. Tests are offline contract tests;
they do not prove browser attachment, login, transcription or full coverage.

## Runtime

- Explicit `--out` wins. Otherwise output goes to the current user's
  `Documents/Openmd/transcripts`; `OPENCLI_TRANSCRIPT_OUT_DIR` overrides it.
- Existing user-selected output directories remain unchanged; there is no
  machine-specific legacy-path rewrite.
- Multiple browser profiles: select `OPENCLI_PROFILE` in the calling process.
  The HTTP client forwards that selection to status and command requests.
- Only with an explicitly authorized input, `OPENCLI_XIAOE_RESUME_DIR` can reuse
  a known interrupted `xiaoe-to-md-*` directory. Playlist identity and file
  lengths must still match. An arbitrary directory is not a recovery source.
- `opencli-plugin.json` describes the optional browser helpers. Installing this
  code does not grant account, upload, cloud transcription or bulk permissions.

## Safety and acceptance

Never persist signed media URLs, cookies, authorization headers or tokens in
Markdown or shared run logs. Fixtures containing signed URL syntax use reserved
example domains and dummy values solely to test redaction/download handling.
Personal runtime records, credential-writing and workstation repair scripts
belong to the maintainer workspace and are not public runtime dependencies.

Read the actual final Markdown, title, source identity, timestamps and coverage.
A loaded command, HTTP success or nonempty player UI is not a transcript.
Cloud providers require their own explicit permission. Do not upload user media
or collect batches while testing the integration without authorization.
