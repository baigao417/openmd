import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

const hostOpenCliRegistryUrl = pathToFileURL(
  path.join(path.dirname(process.execPath), "node_modules", "@jackwener", "opencli", "dist", "src", "registry.js")
);
const { cli: registerCli, Strategy } = await import(hostOpenCliRegistryUrl.href);
const pluginRoot = path.dirname(fileURLToPath(import.meta.url));

const WRITE_COMMAND_NAMES = new Set([
  "batch-transcript",
  "export-md",
  "export-subscription",
  "helper-start",
  "helper-stop",
  "transcript",
  "video-submit",
  "workbench-start",
  "workbench-stop"
]);

function cli(definition) {
  return registerCli({
    access: WRITE_COMMAND_NAMES.has(definition.name) ? "write" : "read",
    ...definition
  });
}

const workflowModuleUrl = new URL("./dist/src/workflow.js", import.meta.url);
const configModuleUrl = new URL("./dist/src/app/runtime-options.js", import.meta.url);
const getExportCommandModuleUrl = new URL("./dist/src/app/get-export-command.js", import.meta.url);
const getHelperClientUrl = new URL("./dist/src/app/get-helper-client.js", import.meta.url);
const getWorkbenchClientUrl = new URL("./dist/src/app/get-workbench-client.js", import.meta.url);
const helperClientUrl = new URL("./dist/src/app/helper-client.js", import.meta.url);
const getRuntimeOptionsModuleUrl = new URL("./dist/src/app/get-export-runtime-options.js", import.meta.url);
const transcriptConfigModuleUrl = new URL("./dist/src/app/transcript-runtime-options.js", import.meta.url);
const xiaoeBatchCommandModuleUrl = new URL("./dist/src/app/xiaoe-batch-command.js", import.meta.url);
const xiaoePageModuleUrl = new URL("./dist/src/app/xiaoe-page.js", import.meta.url);
const xiaohongshuBatchCommandModuleUrl = new URL("./dist/src/app/xiaohongshu-batch-command.js", import.meta.url);
const tingwuPageModuleUrl = new URL("./dist/src/app/tingwu-page.js", import.meta.url);
const tingwuExportModuleUrl = new URL("./dist/src/pipeline/tingwu-export.js", import.meta.url);
const tingwuHelperClientUrl = new URL("./dist/src/app/tingwu-helper-client.js", import.meta.url);
const aiVideoCommandModuleUrl = new URL("./dist/src/app/ai-video-upload-command.js", import.meta.url);

async function loadWorkflow() {
  return await import(workflowModuleUrl.href);
}

async function loadRuntimeOptions() {
  return await import(configModuleUrl.href);
}

async function loadGetExportCommand() {
  return await import(getExportCommandModuleUrl.href);
}

async function loadGetHelperClient() {
  return await import(getHelperClientUrl.href);
}

async function loadGetWorkbenchClient() {
  return await import(getWorkbenchClientUrl.href);
}

async function loadHelperClient() {
  return await import(helperClientUrl.href);
}

async function loadGetRuntimeOptions() {
  return await import(getRuntimeOptionsModuleUrl.href);
}

async function loadTranscriptRuntimeOptions() {
  return await import(transcriptConfigModuleUrl.href);
}

async function loadXiaoeBatchCommand() {
  return await import(xiaoeBatchCommandModuleUrl.href);
}

async function loadXiaoePage() {
  return await import(xiaoePageModuleUrl.href);
}

async function loadXiaohongshuBatchCommand() {
  return await import(xiaohongshuBatchCommandModuleUrl.href);
}

async function loadTingwuPage() {
  return await import(tingwuPageModuleUrl.href);
}

async function loadTingwuExport() {
  return await import(tingwuExportModuleUrl.href);
}

async function loadTingwuHelperClient() {
  return await import(tingwuHelperClientUrl.href);
}

async function loadAiVideoCommand() {
  return await import(aiVideoCommandModuleUrl.href);
}

cli({
  site: "sewing",
  name: "video-probe",
  description: "Probe Gemini/Qwen web pages for video upload controls and requested mode",
  strategy: Strategy.UI,
  browser: true,
  siteSession: "persistent",
  defaultWindowMode: "foreground",
  defaultFormat: "table",
  args: [
    { name: "provider", required: true, choices: ["gemini", "qwen"], help: "Target provider: gemini or qwen" },
    { name: "mode", help: "Requested model/mode, default pro for gemini and think for qwen" },
    { name: "url", help: "Override provider URL" }
  ],
  columns: ["provider", "status", "url", "fileInputs", "canSeeFileInput", "modeClicked", "loginHint", "message"],
  func: async (page, kwargs) => {
    const { runAiVideoProbeCommand } = await loadAiVideoCommand();
    return await runAiVideoProbeCommand(page, kwargs);
  }
});

cli({
  site: "sewing",
  name: "video-submit",
  description: "Upload local sewing videos to Gemini/Qwen, send a prompt, wait for strict JSON, and optionally save it",
  strategy: Strategy.UI,
  browser: true,
  siteSession: "persistent",
  defaultWindowMode: "foreground",
  defaultFormat: "table",
  args: [
    { name: "provider", required: true, choices: ["gemini", "qwen"], help: "Target provider: gemini or qwen" },
    { name: "files", required: true, help: "Local video path(s), separated by |, ;, or newlines" },
    { name: "prompt-file", help: "Prompt markdown/text file to send after upload" },
    { name: "prompt", help: "Inline prompt text; overrides prompt-file when provided" },
    { name: "out", help: "Where to save the captured JSON draft" },
    { name: "mode", help: "Requested model/mode, default pro for gemini and think for qwen" },
    { name: "url", help: "Override provider URL" },
    { name: "timeout", type: "int", default: 600, help: "Seconds to wait for a parseable JSON response" },
    { name: "dry-run", type: "bool", default: false, help: "Navigate/probe but stop before uploading files" }
  ],
  columns: ["provider", "status", "uploaded", "records", "out", "batchId", "modeClicked", "sentBy", "message"],
  func: async (page, kwargs) => {
    const { runAiVideoSubmitCommand } = await loadAiVideoCommand();
    return await runAiVideoSubmitCommand(page, kwargs);
  }
});

cli({
  site: "get",
  name: "subscriptions",
  description: "List Get subscriptions that can be exported as local transcript markdown",
  strategy: Strategy.PUBLIC,
  browser: false,
  defaultFormat: "table",
  columns: ["creatorId", "creatorName", "statsInfo", "subjectUrl"],
  func: async (_page, kwargs) => {
    const { runGetSubscriptionsCommand } = await loadGetExportCommand();
    return await runGetSubscriptionsCommand(kwargs);
  }
});

cli({
  site: "get",
  name: "export-subscription",
  description: "Export all transcript text files from a subscribed Get subject to local markdown files",
  strategy: Strategy.PUBLIC,
  browser: false,
  defaultFormat: "table",
  args: [
    { name: "subject-url", help: "订阅博主主题页 URL，优先推荐" },
    { name: "creator-id", help: "Get 订阅博主 creatorId / id_alias" },
    { name: "creator-name", help: "Get 订阅博主名称（creator-id 模式下用于辅助匹配）" },
    { name: "out", help: "输出根目录，默认用户 Documents/Openmd/transcripts" },
    { name: "start-date", help: "开始日期，格式 YYYY-MM-DD" },
    { name: "end-date", help: "结束日期，格式 YYYY-MM-DD" },
    { name: "skip-existing", type: "bool", help: "跳过已导出的条目", default: true }
  ],
  columns: ["creatorDir", "creatorId", "creatorName", "total", "success", "skipped", "failed"],
  func: async (_page, kwargs) => {
    const { runGetExportSubscriptionCommand } = await loadGetExportCommand();
    return await runGetExportSubscriptionCommand(kwargs);
  }
});

cli({
  site: "xiaohongshu",
  name: "batch-transcript",
  description: "批量下载某个小红书博主的前 N 条视频并生成逐字稿 Markdown",
  strategy: Strategy.PUBLIC,
  browser: false,
  defaultFormat: "table",
  args: [
    { name: "profile-url", required: true, help: "博主主页 URL" },
    { name: "creator-name", required: true, help: "博主名称" },
    { name: "creator-handle", required: true, help: "小红书号" },
    { name: "out", help: "输出根目录，默认用户 Documents/Openmd/transcripts" },
    { name: "limit", help: "处理视频数量", default: 9 },
    { name: "provider", help: "逐字稿 provider", default: "tingwu", choices: ["tingwu", "auto", "get"] },
    { name: "keep-artifacts", type: "bool", help: "保留中间文件", default: false }
  ],
  columns: ["creatorDir", "total", "success", "failed"],
  func: async (_page, kwargs) => {
    const { runXiaohongshuBatchCommand } = await loadXiaohongshuBatchCommand();
    return await runXiaohongshuBatchCommand(kwargs);
  }
});

cli({
  site: "xiaoe",
  name: "batch-transcript",
  description: "Batch transcribe XiaoeTong live catalog items into Markdown with resume support",
  strategy: Strategy.PUBLIC,
  browser: false,
  defaultFormat: "table",
  args: [
    { name: "course-url", positional: true, required: true, help: "XiaoeTong course/catalog URL" },
    { name: "catalog-file", help: "Previously verified OpenCLI catalog JSON; skips catalog browser navigation" },
    { name: "out", help: "Markdown output directory" },
    { name: "provider", help: "Provider to use: auto | tingwu | get", default: "auto", choices: ["auto", "tingwu", "get"] },
    { name: "status", help: "Catalog status filter, e.g. all or 100%,未学", default: "all" },
    { name: "type", help: "Catalog type filter", default: "直播" },
    { name: "chapter", help: "Chapter keyword filter" },
    { name: "title", help: "Title keyword filter" },
    { name: "resource-id", help: "Comma-separated resource_id list" },
    { name: "limit", help: "Max items to process; 0 means unlimited", default: 0 },
    { name: "skip-existing", type: "bool", help: "Skip Markdown files that already exist", default: true },
    { name: "resume", type: "bool", help: "Resume from .xiaoe-batch/batch-state.json", default: true },
    { name: "restart", type: "bool", help: "Ignore old batch state and start over", default: false },
    { name: "dry-run", type: "bool", help: "List selected items without transcribing", default: false },
    { name: "timeout-minutes", help: "Timeout hint for each single transcript task", default: 120 },
    { name: "reuse-tingwu-existing", type: "bool", help: "Check existing Tingwu transcripts by title before downloading media", default: true },
    { name: "tingwu-existing-only", type: "bool", help: "Only import existing Tingwu transcripts; leave media downloads pending for a later run", default: false },
    { name: "tingwu-existing-search-url", help: "Tingwu home/folder URL(s) to scan for existing transcripts before media download" }
  ],
  columns: ["outDir", "total", "success", "skipped", "failed", "statePath"],
  func: async (kwargsOrPage, maybeKwargs) => {
    const { runXiaoeBatchTranscriptCommand } = await loadXiaoeBatchCommand();
    const kwargs = maybeKwargs && typeof maybeKwargs === "object" ? maybeKwargs : kwargsOrPage;
    return await runXiaoeBatchTranscriptCommand(kwargs);
  }
});

cli({
  site: "xiaoe",
  name: "transcript",
  aliases: ["md"],
  description: "Download a XiaoeTong course video, transcribe it, and save AI-friendly Markdown",
  strategy: Strategy.PUBLIC,
  browser: false,
  defaultFormat: "table",
  args: [
    { name: "url", positional: true, required: true, help: "XiaoeTong course video URL" },
    { name: "out", help: "Output directory for the generated Markdown file" },
    { name: "provider", help: "Provider to use: auto | tingwu | get", default: "auto", choices: ["auto", "tingwu", "get"] },
    { name: "keep-artifacts", type: "bool", help: "Keep downloaded media and temp files", default: false },
    { name: "audio-file", help: "Resume transcription using existing local audio for this source; retain the original audio" },
    { name: "title", help: "Original resource title for a local-audio recovery" },
    { name: "timeout-minutes", help: "Timeout hint for long-running download/transcript stages in minutes", default: 30 }
  ],
  columns: ["outputPath", "provider", "coverage"],
  func: async (kwargsOrPage, maybeKwargs) => {
    const kwargs = maybeKwargs && typeof maybeKwargs === "object" ? maybeKwargs : kwargsOrPage;
    const { detectXiaoePage } = await loadXiaoePage();
    const pageInfo = detectXiaoePage({
      url: String(kwargs["url"] ?? "")
    });
    if (!pageInfo.supported) {
      throw new Error(pageInfo.reason || "当前页面不是支持的小鹅通视频页");
    }

    const { runXiaoeToMarkdown } = await loadWorkflow();
    const { resolveRuntimeOptions } = await loadRuntimeOptions();
    const runtime = await resolveRuntimeOptions({
      outDir: typeof kwargs["out"] === "string" ? kwargs["out"] : undefined,
      provider: typeof kwargs["provider"] === "string" ? kwargs["provider"] : undefined,
      persistOutputDir: typeof kwargs["out"] === "string" && kwargs["out"].trim().length > 0
    });
    const keepArtifacts = Boolean(kwargs["keep-artifacts"]);
    const timeoutMinutes = Number.parseInt(String(kwargs["timeout-minutes"] ?? "30"), 10);

    let recoveryDependencies;
    if (typeof kwargs["audio-file"] === "string" && kwargs["audio-file"].trim()) {
      const { stat } = await import("node:fs/promises");
      const { runCommand } = await import("./dist/src/utils/command.js");
      const audioPath = path.resolve(kwargs["audio-file"]);
      if (!(await stat(audioPath)).isFile()) throw new Error("Recovery audio must be a regular local file");
      const probe = await runCommand("ffprobe", ["-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type:format=duration", "-of", "json", audioPath], { timeoutMs: 30000 });
      const metadata = JSON.parse(probe.stdout);
      const duration = Number(metadata.format?.duration);
      if (!metadata.streams?.some(s => s.codec_type === "audio") || !Number.isFinite(duration) || duration <= 0) throw new Error("Recovery file has no valid audio duration");
      recoveryDependencies = { downloadMedia: async () => ({ audioPath, originalPath: audioPath, mediaDurationSec: duration, cleanupPaths: [] }) };
    }
    const result = await runXiaoeToMarkdown({
      url: String(kwargs["url"]),
      outDir: runtime.outDir,
      provider: runtime.provider,
      keepArtifacts: keepArtifacts || !!recoveryDependencies,
      timeoutMinutes,
      resourceId: pageInfo.resourceId,
      ...(recoveryDependencies && typeof kwargs["title"] === "string" ? { outputTitle: kwargs["title"] } : {})
    }, recoveryDependencies);

    return [{
      outputPath: result.outputPath,
      provider: result.provider,
      coverage: `${(result.coverage * 100).toFixed(2)}%`,
      tempDir: result.tempDir
    }];
  }
});

cli({
  site: "media",
  name: "transcript",
  description: "Download a media page, transcribe it, and save AI-friendly Markdown",
  strategy: Strategy.PUBLIC,
  browser: false,
  defaultFormat: "table",
  args: [
    { name: "url", positional: true, required: true, help: "Media page URL" },
    { name: "out", help: "Output directory for the generated Markdown file" },
    { name: "provider", help: "Provider to use: auto | tingwu | get", default: "auto", choices: ["auto", "tingwu", "get"] },
    { name: "keep-artifacts", type: "bool", help: "Keep downloaded media and temp files", default: false },
    { name: "timeout-minutes", help: "Timeout hint for long-running download/transcript stages in minutes", default: 30 }
  ],
  columns: ["outputPath", "provider", "coverage"],
  func: async (_page, kwargs) => {
    const { runMediaToMarkdown } = await loadWorkflow();
    const { resolveTranscriptRuntimeOptions } = await loadTranscriptRuntimeOptions();
    const runtime = await resolveTranscriptRuntimeOptions({
      outDir: typeof kwargs["out"] === "string" ? kwargs["out"] : undefined,
      provider: typeof kwargs["provider"] === "string" ? kwargs["provider"] : undefined,
      persistOutputDir: typeof kwargs["out"] === "string" && kwargs["out"].trim().length > 0
    });
    const keepArtifacts = Boolean(kwargs["keep-artifacts"]);
    const timeoutMinutes = Number.parseInt(String(kwargs["timeout-minutes"] ?? "30"), 10);

    const result = await runMediaToMarkdown({
      url: String(kwargs["url"]),
      outDir: runtime.outDir,
      provider: runtime.provider,
      keepArtifacts,
      timeoutMinutes
    });

    return [{
      outputPath: result.outputPath,
      provider: result.provider,
      coverage: `${(result.coverage * 100).toFixed(2)}%`,
      tempDir: result.tempDir
    }];
  }
});

cli({
  site: "tingwu",
  name: "export-md",
  aliases: ["md"],
  description: "Export an existing Tingwu transcript page to AI-friendly Markdown",
  strategy: Strategy.PUBLIC,
  browser: false,
  defaultFormat: "table",
  args: [
    { name: "url", positional: true, required: true, help: "Tingwu transcript page URL" },
    { name: "out", help: "Output directory for the generated Markdown file" }
  ],
  columns: ["outputPath", "provider", "coverage"],
  func: async (kwargsOrPage, maybeKwargs) => {
    const kwargs = maybeKwargs && typeof maybeKwargs === "object" && "url" in maybeKwargs ? maybeKwargs : kwargsOrPage;
    const { detectTingwuTranscriptPage } = await loadTingwuPage();
    const pageInfo = detectTingwuTranscriptPage({
      url: String(kwargs["url"] ?? "")
    });
    if (!pageInfo.supported) {
      throw new Error(pageInfo.reason || "当前页面不是支持的听悟逐字稿页面");
    }

    const { resolveTranscriptRuntimeOptions } = await loadTranscriptRuntimeOptions();
    const runtime = await resolveTranscriptRuntimeOptions({
      outDir: typeof kwargs["out"] === "string" ? kwargs["out"] : undefined,
      persistOutputDir: typeof kwargs["out"] === "string" && kwargs["out"].trim().length > 0
    });
    const { runTingwuExportToMarkdown } = await loadTingwuExport();
    const result = await runTingwuExportToMarkdown({
      url: String(kwargs["url"]),
      outDir: runtime.outDir
    });

    return [{
      outputPath: result.outputPath,
      provider: result.provider,
      coverage: `${(result.coverage * 100).toFixed(2)}%`
    }];
  }
});

cli({
  site: "xiaoe",
  name: "helper-start",
  description: "Start the local Xiaoe transcript helper service in the background",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "baseUrl", "message"],
  func: async () => {
    const { getHelperStatus } = await loadHelperClient();
    const status = await getHelperStatus();
    if (status) {
      return [{ status: "running", baseUrl: "http://127.0.0.1:19826", message: "helper already running" }];
    }

    if (process.platform === "win32") {
      const psCommand = `Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','npx','tsx','src/app/helper-server.ts' -WorkingDirectory '${pluginRoot.replace(/'/g, "''")}' -WindowStyle Hidden`;
      execFileSync("powershell.exe", ["-NoProfile", "-Command", psCommand], {
        cwd: pluginRoot,
        stdio: "ignore",
        windowsHide: true
      });
    } else {
      spawn("npx", ["tsx", "src/app/helper-server.ts"], {
        cwd: pluginRoot,
        detached: true,
        stdio: "ignore",
        windowsHide: true
      }).unref();
    }

    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      const nextStatus = await getHelperStatus();
      if (nextStatus) {
        return [{ status: "running", baseUrl: "http://127.0.0.1:19826", message: "helper started" }];
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
    }

    throw new Error("helper did not start in time");
  }
});

cli({
  site: "get",
  name: "helper-start",
  description: "Start the local Get transcript export helper service in the background",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "baseUrl", "message"],
  func: async () => {
    const { getGetHelperStatus } = await loadGetHelperClient();
    const status = await getGetHelperStatus();
    if (status) {
      return [{ status: "running", baseUrl: "http://127.0.0.1:19828", message: "helper already running" }];
    }

    if (process.platform === "win32") {
      const psCommand = `Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','npx','tsx','src/app/get-helper-server.ts' -WorkingDirectory '${pluginRoot.replace(/'/g, "''")}' -WindowStyle Hidden`;
      execFileSync("powershell.exe", ["-NoProfile", "-Command", psCommand], {
        cwd: pluginRoot,
        stdio: "ignore",
        windowsHide: true
      });
    } else {
      spawn("npx", ["tsx", "src/app/get-helper-server.ts"], {
        cwd: pluginRoot,
        detached: true,
        stdio: "ignore",
        windowsHide: true
      }).unref();
    }

    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      const nextStatus = await getGetHelperStatus();
      if (nextStatus) {
        return [{ status: "running", baseUrl: "http://127.0.0.1:19828", message: "helper started" }];
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
    }

    throw new Error("helper did not start in time");
  }
});

cli({
  site: "get",
  name: "workbench-start",
  description: "Start the local Get export workbench website",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "url", "message"],
  func: async () => {
    const { getGetWorkbenchStatus, GET_WORKBENCH_BASE_URL } = await loadGetWorkbenchClient();
    const status = await getGetWorkbenchStatus();
    if (status) {
      return [{ status: "running", url: GET_WORKBENCH_BASE_URL, message: "workbench already running" }];
    }

    if (process.platform === "win32") {
      const psCommand = `Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','npx','tsx','src/app/get-workbench-server.ts' -WorkingDirectory '${pluginRoot.replace(/'/g, "''")}' -WindowStyle Hidden`;
      execFileSync("powershell.exe", ["-NoProfile", "-Command", psCommand], {
        cwd: pluginRoot,
        stdio: "ignore",
        windowsHide: true
      });
    } else {
      spawn("npx", ["tsx", "src/app/get-workbench-server.ts"], {
        cwd: pluginRoot,
        detached: true,
        stdio: "ignore",
        windowsHide: true
      }).unref();
    }

    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      const nextStatus = await getGetWorkbenchStatus();
      if (nextStatus) {
        return [{ status: "running", url: GET_WORKBENCH_BASE_URL, message: "workbench started" }];
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
    }

    throw new Error("workbench did not start in time");
  }
});

cli({
  site: "get",
  name: "workbench-stop",
  description: "Stop the local Get export workbench website",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "message"],
  func: async () => {
    const { shutdownGetWorkbench, getGetWorkbenchStatus } = await loadGetWorkbenchClient();
    const stopped = await shutdownGetWorkbench();
    if (stopped) {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        const status = await getGetWorkbenchStatus();
        if (!status) {
          return [{ status: "stopped", message: "workbench stopped" }];
        }
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
    }
    return [{ status: stopped ? "stopping" : "unknown", message: stopped ? "workbench shutdown requested" : "workbench not reachable" }];
  }
});

cli({
  site: "get",
  name: "helper-status",
  description: "Show local Get transcript export helper status and current defaults",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "defaultOutputDir", "lastCreatorName", "lastCreatorDir"],
  func: async () => {
    const { getGetHelperStatus } = await loadGetHelperClient();
    const status = await getGetHelperStatus();
    if (!status) {
      return [{ status: "stopped", defaultOutputDir: "", lastCreatorName: "", lastCreatorDir: "" }];
    }
    return [{
      status: status.state.status,
      defaultOutputDir: status.config.defaultOutputDir,
      lastCreatorName: status.state.lastRun?.creatorName ?? "",
      lastCreatorDir: status.state.lastRun?.creatorDir ?? ""
    }];
  }
});

cli({
  site: "get",
  name: "helper-stop",
  description: "Stop the local Get transcript export helper service",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "message"],
  func: async () => {
    const { shutdownGetHelper, getGetHelperStatus } = await loadGetHelperClient();
    const stopped = await shutdownGetHelper();
    if (stopped) {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        const status = await getGetHelperStatus();
        if (!status) {
          return [{ status: "stopped", message: "helper stopped" }];
        }
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
    }
    return [{ status: stopped ? "stopping" : "unknown", message: stopped ? "helper shutdown requested" : "helper not reachable" }];
  }
});

cli({
  site: "tingwu",
  name: "helper-start",
  description: "Start the local Tingwu transcript export helper service in the background",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "baseUrl", "message"],
  func: async () => {
    const { getTingwuHelperStatus } = await loadTingwuHelperClient();
    const status = await getTingwuHelperStatus();
    if (status) {
      return [{ status: "running", baseUrl: "http://127.0.0.1:19827", message: "helper already running" }];
    }

    if (process.platform === "win32") {
      const psCommand = `Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','npx','tsx','src/app/tingwu-helper-server.ts' -WorkingDirectory '${pluginRoot.replace(/'/g, "''")}' -WindowStyle Hidden`;
      execFileSync("powershell.exe", ["-NoProfile", "-Command", psCommand], {
        cwd: pluginRoot,
        stdio: "ignore",
        windowsHide: true
      });
    } else {
      spawn("npx", ["tsx", "src/app/tingwu-helper-server.ts"], {
        cwd: pluginRoot,
        detached: true,
        stdio: "ignore",
        windowsHide: true
      }).unref();
    }

    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      const nextStatus = await getTingwuHelperStatus();
      if (nextStatus) {
        return [{ status: "running", baseUrl: "http://127.0.0.1:19827", message: "helper started" }];
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
    }

    throw new Error("helper did not start in time");
  }
});

cli({
  site: "tingwu",
  name: "helper-status",
  description: "Show local Tingwu export helper status and current defaults",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "defaultOutputDir", "lastOutputPath"],
  func: async () => {
    const { getTingwuHelperStatus } = await loadTingwuHelperClient();
    const status = await getTingwuHelperStatus();
    if (!status) {
      return [{ status: "stopped", defaultOutputDir: "", lastOutputPath: "" }];
    }
    return [{
      status: status.state.status,
      defaultOutputDir: status.config.defaultOutputDir,
      lastOutputPath: status.state.lastRun?.outputPath ?? ""
    }];
  }
});

cli({
  site: "tingwu",
  name: "helper-stop",
  description: "Stop the local Tingwu transcript export helper service",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "message"],
  func: async () => {
    const { shutdownTingwuHelper, getTingwuHelperStatus } = await loadTingwuHelperClient();
    const stopped = await shutdownTingwuHelper();
    if (stopped) {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        const status = await getTingwuHelperStatus();
        if (!status) {
          return [{ status: "stopped", message: "helper stopped" }];
        }
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
    }
    return [{ status: stopped ? "stopping" : "unknown", message: stopped ? "helper shutdown requested" : "helper not reachable" }];
  }
});

cli({
  site: "xiaoe",
  name: "helper-status",
  description: "Show local Xiaoe transcript helper status and current defaults",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "defaultOutputDir", "defaultProvider", "lastOutputPath"],
  func: async () => {
    const { getHelperStatus } = await loadHelperClient();
    const status = await getHelperStatus();
    if (!status) {
      return [{ status: "stopped", defaultOutputDir: "", defaultProvider: "", lastOutputPath: "" }];
    }
    return [{
      status: status.state.status,
      defaultOutputDir: status.config.defaultOutputDir,
      defaultProvider: status.config.defaultProvider,
      lastOutputPath: status.state.lastRun?.outputPath ?? ""
    }];
  }
});

cli({
  site: "xiaoe",
  name: "helper-stop",
  description: "Stop the local Xiaoe transcript helper service",
  strategy: Strategy.PUBLIC,
  browser: false,
  columns: ["status", "message"],
  func: async () => {
    const { shutdownHelper, getHelperStatus } = await loadHelperClient();
    const stopped = await shutdownHelper();
    if (stopped) {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        const status = await getHelperStatus();
        if (!status) {
          return [{ status: "stopped", message: "helper stopped" }];
        }
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
    }
    return [{ status: stopped ? "stopping" : "unknown", message: stopped ? "helper shutdown requested" : "helper not reachable" }];
  }
});
