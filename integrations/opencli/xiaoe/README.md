# Xiaoe read adapters

Only `play-url`, `catalog` and `content` belong here. Media download, local ASR
and Markdown writing belong to Openmd. There is no transcription plugin.

`openmd.runtime.opencli_adapters` installs these packaged sources into
`<Openmd data>/opencli/clis/xiaoe`, an isolated user adapter directory, verifies
owned file hashes and refuses to overwrite user edits. It executes through the
pinned OpenCLI dispatcher and closes the background tab after the command.
The normal user's `~/.opencli` is never changed. Browser sessions stay managed
by the existing bridge; adapters never read cookies or credential files.

The browser gate permits media discovery. Download and local ASR additionally
require their respective gates. Cloud ASR is never enabled by these adapters.
