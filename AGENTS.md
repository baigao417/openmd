# Openmd contributor routing

Read [CONTRIBUTING.md](CONTRIBUTING.md), [code standards](docs/code-standards.md)
and [LESSONS.md](LESSONS.md) before changing behavior.

| Work | Owner |
|---|---|
| Main flow, module ownership | [Architecture](docs/architecture.md) |
| Platform adapters / plugins | [Adding a platform](docs/adding-a-platform.md) |
| CLI, fallback, content, writer, ASR/OCR, extension | [Subsystem map](docs/subsystems/README.md) |
| Routes/cards/capabilities/defaults | `src/openmd/routing/declarations.json`; `python -m openmd.routing.generate` |
| Install/process/content/protocol/evidence checks | [Executable contracts](docs/prevention-checks.md) |

Follow CONTRIBUTING: regression first for bug fixes, full `python -m pytest`
before delivery. Availability and output claims follow CS-6; permission and
secret boundaries follow CS-3/CS-4. Maintainer workflow and private run evidence
are outside this public package.
