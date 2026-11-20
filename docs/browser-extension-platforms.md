# Browser extension on macOS and Linux · 浏览器扩展的 macOS / Linux 支持

English · [简体中文](#简体中文)

**Help wanted.** The CLI, MCP server and right-click menu already run on Windows, macOS and Linux. The browser extension currently works only on Windows (Chrome / Edge). Porting it needs no new design, only the per-OS registration described below. If you use a Mac or Linux, you can implement it, verify it on your own machine and send a PR that we can merge on evidence.

## How the extension reaches Openmd

```
extension (fixed ID from the "key" in manifest.json)
  → chrome.runtime.sendNativeMessage("com.openmd.host", …)
  → host manifest com.openmd.host.json      ← found through a per-OS location
  → launcher                                ← runs: <python> -I -m openmd.browser_host <args>
  → openmd.browser_host (native-message protocol, job queue)
  → hidden queue runner: python -I -m openmd.browser_host --runner
  → the normal Openmd save pipeline
```

Everything from `openmd.browser_host` down is plain Python and already cross-platform. The extension ID is identical on every OS because `manifest.json` pins `key`. The part bundled from OpenCLI talks to Openmd's managed OpenCLI, which upstream supports on all three systems.

## What is Windows-only today

| Place | Windows behavior today | What macOS / Linux need |
|---|---|---|
| `src/openmd/browser_setup.py` `install()` | Exits with "目前只支持 Windows" on non-Windows | Remove the early exit; branch per OS |
| `browser_setup.py` `REGISTRY_KEYS`, `_reg()` | Registers the host manifest path in `HKCU\…\NativeMessagingHosts` | Write `com.openmd.host.json` into each browser's user directory (table below); no registry |
| `browser_setup.py` `host_files()`, `write_host_files()` | Writes the launcher `openmd-browser-host.cmd` with cmd syntax | Write `openmd-browser-host` as `#!/bin/sh` + `exec "<python>" -I -m openmd.browser_host "$@"`, mode 0755; manifest `path` must be absolute |
| `browser_setup.py` `launcher_ping()` | Pings the written launcher through `cmd.exe` | Run the launcher file directly with the same ping message |
| `browser_setup.py` `install()` tail | `os.startfile(folder)` opens the extension folder | `open` (macOS) / `xdg-open` (Linux), only when stdout is a terminal |
| `browser_setup.py` `uninstall()` | Deletes the registry keys | Delete exactly the manifest files that `install()` wrote |
| `src/openmd/browser_diagnostics.py` `registered_manifests()` | Reads the registry with `winreg`; returns `{}` elsewhere | Read the manifest files from the same locations, read-only |
| `browser_diagnostics.py` `_host_diagnostics()` | Accepts exactly one launcher form: `"<python>" -I -m openmd.browser_host %*` | Also accept exactly the POSIX form above; never execute launcher text while inspecting |
| `browser_diagnostics.py` `browser_host_diagnostics()` | `supported` is `os.name == "nt"` | `supported` is true on the newly supported OS |
| `src/openmd/browser_host.py` `_spawn_runner()` | Breaks away from Chrome's job object so closing the browser does not kill a long save | Start the runner with `start_new_session=True` and confirm a long save survives closing the browser |
| `browser_host.py` `reveal()` | `explorer /select,` on the saved file | `open -R <file>` (macOS); `xdg-open <folder>` (Linux) |

### Host manifest locations (user scope only)

| Browser | macOS | Linux |
|---|---|---|
| Google Chrome | `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/` | `~/.config/google-chrome/NativeMessagingHosts/` |
| Chromium | `~/Library/Application Support/Chromium/NativeMessagingHosts/` | `~/.config/chromium/NativeMessagingHosts/` |
| Microsoft Edge (stable) | `~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/` | `~/.config/microsoft-edge/NativeMessagingHosts/` |

Sources: [Chrome native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging), [Edge native messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging). Write a manifest only for browsers whose user data directory exists. Snap and Flatpak browsers sandbox native hosts; they are out of scope unless you also verify them.

## Rules a port must keep

- Write user-scope state only: `<openmd data>/browser/` and the manifest files above. No `sudo`, no system directories, no shell profile edits.
- `uninstall` removes exactly what `install` wrote, and nothing it did not write.
- `doctor` stays read-only: it reads files and imports the interpreter with `-I -B`. It never starts the host and never repairs anything.
- The launcher always uses `-I`, so an inherited `PYTHONPATH` cannot swap the code.
- Windows behavior and its tests do not change.
- No credentials, cookies or user paths in error messages or receipts.

## Tests to add (CI runs them on macOS and Linux)

Put them next to `tests/test_browser_host.py` and `tests/test_browser_diagnostics.py`. Point `HOME` and the Openmd data directory at `tmp_path` with `monkeypatch`, and mark POSIX-only tests `skipif(os.name == "nt")` with the reason.

- `install()` writes `com.openmd.host.json` into each existing browser directory, with an absolute `path` and the correct `allowed_origins`.
- The launcher has the exact POSIX form and is executable; `launcher_ping()` succeeds through the written file.
- `registered_manifests()` finds those files; `_host_diagnostics()` accepts the POSIX launcher and rejects anything else.
- `uninstall()` removes only the files `install()` wrote.
- The runner spawn requests a new session on POSIX.

## Verify on your machine and attach the evidence to the PR

1. `pip install -e ".[dev]"` (or `python scripts/dev_setup.py`), then `openmd browser-setup`. Paste the output, including the printed extension ID.
2. Open `chrome://extensions` (or `edge://extensions`), turn on Developer mode, choose **Load unpacked** and select the folder that was printed. Confirm the ID shown by the browser equals the printed ID.
3. Run `openmd doctor --json` and paste the `browser_host` section; it must show `"ready": true`.
4. Open a public article, click the extension and save it. Paste the front matter and the first lines of the `.md`.
5. Start a long save (a long page, or a video with local transcription), close the browser while it runs, then confirm the job finished and the file exists.
6. Click "open folder" after a save and confirm the file manager shows the file.
7. Run `openmd browser-setup --uninstall`, then `openmd doctor --json` again; the host must no longer be listed.
8. Write down the OS version, browser and version, and how Python and Openmd were installed (venv, pipx, uv). In "what was not verified", list every browser in the table above that you did not test.

A platform is marked as supported for the browsers someone verified on a real machine; the rest stay "experimental" in the README until a second person confirms them.

---

## 简体中文

**欢迎贡献。** 命令行、MCP 和右键菜单已经支持 Windows、macOS 和 Linux；浏览器扩展目前只支持 Windows（Chrome / Edge）。移植不需要重新设计，只要按下面的说明补上各系统的注册方式。如果你用 Mac 或 Linux，可以自己实现、在本机验收，再提一个凭证据就能合并的 PR。

## 扩展是怎么连到 Openmd 的

```
扩展（manifest.json 里的 "key" 固定了扩展 ID）
  → chrome.runtime.sendNativeMessage("com.openmd.host", …)
  → 宿主清单 com.openmd.host.json          ← 各系统查找位置不同
  → 启动器                                 ← 执行：<python> -I -m openmd.browser_host <参数>
  → openmd.browser_host（native messaging 协议、任务队列）
  → 隐藏的队列 runner：python -I -m openmd.browser_host --runner
  → 正常的 Openmd 保存流程
```

从 `openmd.browser_host` 往下都是普通 Python，已经跨平台。`manifest.json` 固定了 `key`，所以扩展 ID 在所有系统上都一样。扩展里来自 OpenCLI 的那部分连接的是 Openmd 托管的 OpenCLI，上游本身支持三个系统。

## 目前只在 Windows 上实现的地方

| 位置 | 现在的 Windows 行为 | macOS / Linux 需要 |
|---|---|---|
| `src/openmd/browser_setup.py` `install()` | 非 Windows 直接提示"目前只支持 Windows"并退出 | 去掉提前退出，按系统分支 |
| `browser_setup.py` `REGISTRY_KEYS`、`_reg()` | 把宿主清单路径写进 `HKCU\…\NativeMessagingHosts` | 把 `com.openmd.host.json` 写进各浏览器的用户目录（见下表），不用注册表 |
| `browser_setup.py` `host_files()`、`write_host_files()` | 用 cmd 语法写启动器 `openmd-browser-host.cmd` | 写 `openmd-browser-host`：`#!/bin/sh` 加 `exec "<python>" -I -m openmd.browser_host "$@"`，权限 0755；清单里的 `path` 必须是绝对路径 |
| `browser_setup.py` `launcher_ping()` | 通过 `cmd.exe` ping 写好的启动器 | 直接执行启动器文件，发同样的 ping |
| `browser_setup.py` `install()` 结尾 | `os.startfile(folder)` 打开扩展文件夹 | macOS 用 `open`，Linux 用 `xdg-open`，只在终端交互时打开 |
| `browser_setup.py` `uninstall()` | 删除注册表键 | 只删除 `install()` 写过的清单文件 |
| `src/openmd/browser_diagnostics.py` `registered_manifests()` | 用 `winreg` 读注册表，其他系统返回 `{}` | 只读地从同样的位置读取清单文件 |
| `browser_diagnostics.py` `_host_diagnostics()` | 只接受一种启动器写法：`"<python>" -I -m openmd.browser_host %*` | 再精确接受上面的 POSIX 写法；检查时绝不执行启动器内容 |
| `browser_diagnostics.py` `browser_host_diagnostics()` | `supported` 等于 `os.name == "nt"` | 新支持的系统上 `supported` 为真 |
| `src/openmd/browser_host.py` `_spawn_runner()` | 脱离 Chrome 的作业对象，关掉浏览器也不会杀掉长时间的保存 | 用 `start_new_session=True` 启动 runner，并实测关掉浏览器后长时间保存仍能完成 |
| `browser_host.py` `reveal()` | 用 `explorer /select,` 定位保存的文件 | macOS 用 `open -R <文件>`，Linux 用 `xdg-open <文件夹>` |

### 宿主清单位置（只用当前用户范围）

| 浏览器 | macOS | Linux |
|---|---|---|
| Google Chrome | `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/` | `~/.config/google-chrome/NativeMessagingHosts/` |
| Chromium | `~/Library/Application Support/Chromium/NativeMessagingHosts/` | `~/.config/chromium/NativeMessagingHosts/` |
| Microsoft Edge（稳定版） | `~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/` | `~/.config/microsoft-edge/NativeMessagingHosts/` |

来源：[Chrome native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)、[Edge native messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging)。只给用户数据目录已存在的浏览器写清单。Snap 和 Flatpak 打包的浏览器会把本机宿主关进沙箱，除非你也验证过，否则不在本次范围内。

## 移植必须守住的规则

- 只写当前用户范围的状态：`<openmd 数据目录>/browser/` 和上面那些清单文件。不用 `sudo`，不写系统目录，不改 shell 配置文件。
- `uninstall` 只删除 `install` 写过的东西，一样不多，一样不少。
- `doctor` 保持只读：只读文件，用 `-I -B` 导入解释器；不启动宿主，也不修复任何东西。
- 启动器始终带 `-I`，继承来的 `PYTHONPATH` 不能替换掉代码。
- Windows 的行为和测试不变。
- 错误信息和回执里不出现凭证、cookie 或用户路径。

## 需要补的测试（CI 会在 macOS 和 Linux 上跑）

放在 `tests/test_browser_host.py` 和 `tests/test_browser_diagnostics.py` 旁边。用 `monkeypatch` 把 `HOME` 和 Openmd 数据目录指到 `tmp_path`；只适用于 POSIX 的测试加 `skipif(os.name == "nt")` 并注明原因。

- `install()` 把 `com.openmd.host.json` 写进每个已存在的浏览器目录，`path` 是绝对路径，`allowed_origins` 正确。
- 启动器内容是精确的 POSIX 写法，并且可执行；`launcher_ping()` 通过写好的文件 ping 成功。
- `registered_manifests()` 能找到这些文件；`_host_diagnostics()` 接受 POSIX 启动器，拒绝其他写法。
- `uninstall()` 只删除 `install()` 写过的文件。
- POSIX 上启动 runner 时请求新会话。

## 在你自己的机器上验收，并把证据贴进 PR

1. `pip install -e ".[dev]"`（或 `python scripts/dev_setup.py`），然后运行 `openmd browser-setup`。贴出输出，包括打印出的扩展 ID。
2. 打开 `chrome://extensions`（或 `edge://extensions`），打开开发者模式，点**加载已解压的扩展程序**，选择打印出的文件夹。确认浏览器显示的 ID 和打印出的 ID 一致。
3. 运行 `openmd doctor --json`，贴出 `browser_host` 一节，必须是 `"ready": true`。
4. 打开一篇公开文章，点扩展保存。贴出 `.md` 的 front matter 和开头几行。
5. 发起一次长时间保存（长页面，或需要本地转写的视频），保存过程中关掉浏览器，然后确认任务完成、文件存在。
6. 保存完成后点"打开文件夹"，确认文件管理器定位到了这个文件。
7. 运行 `openmd browser-setup --uninstall`，再运行 `openmd doctor --json`，宿主应该不再出现。
8. 写下系统版本、浏览器及版本、Python 和 Openmd 的安装方式（venv、pipx、uv）。在"没验证什么"里列出上表中你没测过的每个浏览器。

某个平台只把有人在真机上验证过的浏览器标为支持；其余的在 README 里保持"实验"，直到有第二个人确认。
