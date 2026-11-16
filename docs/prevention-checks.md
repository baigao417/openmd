# Executable maintenance contracts

| Contract | Check | Historical incident IDs |
|---|---|---|
| Installed wheel, external cwd, empty config, narrow PATH, cp936/cp1252, Chinese and space paths, final artifact | `python scripts/verify_install_contract.py --wheel <wheel>`; CI builds first | I006/I012/I034/I036/I049–I053/I065/I071 |
| Process identity, exit ordering, temp ownership, atomic checkpoint | `python -m pytest tests/test_runtime_processes.py tests/test_tempdirs.py tests/test_hardkill_recovery.py tests/test_browser_host.py -q` | I007/I015/I021/I023/I037/I041/I054/I055/I057/I060/I061/I069/I073 |
| State-page negative and legal neighbor positive, original words/readability/coverage | `python -m pytest tests/test_prevention_contracts.py tests/test_content_target_validation.py tests/test_readable_output.py tests/ocr -q` | I009/I010/I014/I016/I022/I026/I029/I035/I038/I039/I042/I043/I046/I048/I059 |
| Single declaration owner, generated routes/cards/docs, collection bindings | `python -m openmd.routing.generate --check`; plugin and planning tests | I003/I011/I020/I024/I025/I033/I040/I045/I047/I066 |
| OpenCLI envelopes and actual dispatcher ABI, typed arguments/errors | `python scripts/verify_external_contracts.py --offline --host <pinned host>` | I001/I013/I017/I018/I028/I032/I067/I073 |
| Artifact identity, readback, code tree, explicit unverified scope | `python scripts/validate_delivery_evidence.py --root <checkout> --evidence <receipt>` | I020/I031/I034/I047/I049/I056/I058/I059/I072 |
| Actor/source/side effect/scope, no worker authority escalation | `python scripts/validate_run_gate.py --authority <trusted-user-record> --gate <delegation> --action <action> --target <directory> -- <command>` | I062/I064/I068/I070/I074 |
| Bounded Codex session-start receipt | `python scripts/check_codex_startup.py --timeout 30 -- <startup-probe-command>` | I015/I027/I030/I063/I065/I068/I070/I071 |

The gate wrapper validates a separately trusted user authority record and its
hash. A worker or dispatcher cannot create new user authorization by relabeling
its own receipt. The wrapper only constrains actions submitted through it; it
is not a shell sandbox or a way to authenticate an arbitrary document's author.
The startup probe terminates its owned probe process after success or timeout;
it does not launch a normal work session for completion.

`routing/declarations.json` owns bundled routes, tool cards, capabilities and
collection defaults. Generate projections with `python -m openmd.routing.generate`.
Runtime provider instances remain separate. Plugin descriptors own plugin
declarations and do not grant execution permissions.

Fixture and stub checks are offline regression evidence. Live availability,
OCR/ASR accuracy, reader quality and exhaustive collection still need appropriate
real evidence and explicit permissions. Incident coverage means a failure class
has an executable boundary; it is not proof that every historical incident was
reproduced or that future upstream changes cannot break it.
