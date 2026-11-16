import { execFile, type ExecFileException } from "node:child_process";
import fs from "node:fs";

export interface CommandResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

const DEFAULT_TIMEOUT_MS = 120000;
const DEFAULT_MAX_BUFFER = 20 * 1024 * 1024;
const ERROR_TAIL_LINE_COUNT = 25;
const activeChildren = new Set<ReturnType<typeof execFile>>();
const SENSITIVE_QUERY_KEYS = new Set([
  "access_token",
  "ak",
  "auth",
  "auth_key",
  "authorization",
  "cookie",
  "ko_token",
  "refresh_token",
  "sig",
  "sign",
  "signature",
  "x-oss-signature",
  "security-token",
  "t",
  "token",
  "us"
]);

function redactSensitiveUrl(rawValue: string): string {
  try {
    const parsed = new URL(rawValue);
    for (const key of new Set(parsed.searchParams.keys())) {
      if (SENSITIVE_QUERY_KEYS.has(key.toLowerCase())) {
        parsed.searchParams.set(key, "[REDACTED]");
      }
    }
    return parsed.toString();
  } catch {
    return rawValue;
  }
}

export function redactSensitiveText(value: string): string {
  let sanitized = value;

  sanitized = sanitized.replace(/(Cookie:\s*)([^\r\n]+)/gi, "$1[REDACTED]");
  sanitized = sanitized.replace(/(Authorization:\s*)([^\r\n]+)/gi, "$1[REDACTED]");
  sanitized = sanitized.replace(
    /\b(access_token|ko_token|refresh_token|token|auth_key|x-oss-signature|security-token)=([^;\s&]+)/gi,
    (_, key: string) => `${key}=[REDACTED]`
  );
  sanitized = sanitized.replace(/https?:\/\/[^\s"'<>]+/gi, (url) => redactSensitiveUrl(url));

  return sanitized;
}

function quoteArg(value: string): string {
  return /[\s"'`]/.test(value) ? JSON.stringify(value) : value;
}

function formatCommandForLogs(command: string, args: string[]): string {
  return [command, ...args.map((arg) => quoteArg(redactSensitiveText(arg)))].join(" ");
}

function trimGenericExecMessage(message: string): string {
  return message.replace(/^Command failed:[^\r\n]*(?:\r?\n)?/, "").trim();
}

function summarizeOutput(value: string): string {
  const sanitized = redactSensitiveText(value).trim();
  if (!sanitized) {
    return "";
  }

  const lines = sanitized.split(/\r?\n/);
  return lines.slice(-ERROR_TAIL_LINE_COUNT).join("\n");
}

function terminateChildProcess(child: ReturnType<typeof execFile>): void {
  if (process.platform === "win32" && child.pid) {
    execFile("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      windowsHide: true
    }, () => undefined);
    return;
  }
  child.kill();
}

export function terminateActiveCommands(): void {
  for (const child of activeChildren) {
    terminateChildProcess(child);
  }
}

type WatchFileOptions = {
  path?: string;
  directory?: string;
  prefix?: string;
  ignoreIncludes?: string[];
  ignoreSuffixes?: string[];
  stallMs: number;
  pollMs?: number;
};

function getWatchedSize(watchFile: WatchFileOptions): number | undefined {
  if (watchFile.path) {
    return fs.statSync(watchFile.path).size;
  }

  if (watchFile.directory) {
    const entries = fs.readdirSync(watchFile.directory);
    let total = 0;
    let matched = false;
    for (const entry of entries) {
      if (watchFile.prefix && !entry.startsWith(watchFile.prefix)) {
        continue;
      }
      if (watchFile.ignoreIncludes?.some((part) => entry.includes(part))) {
        continue;
      }
      if (watchFile.ignoreSuffixes?.some((suffix) => entry.endsWith(suffix))) {
        continue;
      }
      const stat = fs.statSync(`${watchFile.directory}/${entry}`);
      if (!stat.isFile()) {
        continue;
      }
      matched = true;
      total += stat.size;
    }
    return matched ? total : undefined;
  }

  return undefined;
}

function formatWatchTarget(watchFile: WatchFileOptions): string {
  if (watchFile.path) {
    return watchFile.path;
  }
  if (watchFile.directory) {
    return `${watchFile.directory}/${watchFile.prefix ?? "*"}`;
  }
  return "<unknown>";
}

function isTimeoutError(error: ExecFileException): boolean {
  if (error.killed) {
    return true;
  }
  return /timed out/i.test(error.message);
}

function formatCommandFailure(
  command: string,
  args: string[],
  error: ExecFileException,
  stdout: string,
  stderr: string,
  timeoutMs: number
): string {
  const invocation = formatCommandForLogs(command, args);
  const outputSummary = summarizeOutput(stderr) || summarizeOutput(stdout);

  if (isTimeoutError(error)) {
    return outputSummary
      ? `${invocation} timed out after ${timeoutMs}ms: ${outputSummary}`
      : `${invocation} timed out after ${timeoutMs}ms`;
  }

  const messageSummary = trimGenericExecMessage(redactSensitiveText(error.message));
  const detail = outputSummary || messageSummary;
  const exitCode = typeof error.code === "number" ? ` with exit code ${error.code}` : "";
  const signal = typeof error.signal === "string" ? ` (signal ${error.signal})` : "";

  return detail
    ? `${invocation} failed${exitCode}${signal}: ${detail}`
    : `${invocation} failed${exitCode}${signal}`;
}

export function runCommand(
  command: string,
  args: string[],
  options?: {
    cwd?: string;
    env?: NodeJS.ProcessEnv;
    timeoutMs?: number;
    allowFailure?: boolean;
    watchFile?: WatchFileOptions;
  }
): Promise<CommandResult> {
  return new Promise((resolve, reject) => {
    const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    let watchdogError: string | undefined;
    let watchdogTimer: NodeJS.Timeout | undefined;
    let timeoutTimer: NodeJS.Timeout | undefined;
    let timeoutTriggered = false;
    let lastObservedSize: number | undefined;
    let lastProgressAt = Date.now();
    const child = execFile(
      command,
      args,
      {
        cwd: options?.cwd,
        env: options?.env ? { ...process.env, ...options.env } : undefined,
        windowsHide: true,
        maxBuffer: DEFAULT_MAX_BUFFER
      },
      (error, stdout, stderr) => {
        activeChildren.delete(child);
        if (watchdogTimer) {
          clearInterval(watchdogTimer);
        }
        if (timeoutTimer) {
          clearTimeout(timeoutTimer);
        }
        const exitCode = typeof error?.code === "number" ? error.code : error ? 1 : 0;

        if (error && !options?.allowFailure) {
          if (watchdogError) {
            reject(new Error(`${formatCommandForLogs(command, args)} failed: ${watchdogError}`));
            return;
          }
          if (timeoutTriggered) {
            const outputSummary = summarizeOutput(stderr) || summarizeOutput(stdout);
            reject(new Error(
              outputSummary
                ? `${formatCommandForLogs(command, args)} timed out after ${timeoutMs}ms: ${outputSummary}`
                : `${formatCommandForLogs(command, args)} timed out after ${timeoutMs}ms`
            ));
            return;
          }
          reject(new Error(formatCommandFailure(command, args, error, stdout, stderr, timeoutMs)));
          return;
        }

        resolve({ exitCode, stdout, stderr });
      }
    );
    activeChildren.add(child);

    timeoutTimer = setTimeout(() => {
      timeoutTriggered = true;
      terminateChildProcess(child);
    }, timeoutMs);

    if (options?.watchFile) {
      const watchFile = options.watchFile;
      const pollMs = watchFile.pollMs ?? 5000;
      watchdogTimer = setInterval(() => {
        try {
          const size = getWatchedSize(watchFile);
          if (size !== undefined && (lastObservedSize === undefined || size > lastObservedSize)) {
            lastObservedSize = size;
            lastProgressAt = Date.now();
          }
        } catch {
          // Missing/locked output files are not progress. Keep the original
          // timer running so a child process stuck before first byte is killed.
        }

        if (!watchdogError && Date.now() - lastProgressAt >= watchFile.stallMs) {
          watchdogError = `watched output did not grow for ${watchFile.stallMs}ms: ${formatWatchTarget(watchFile)}`;
          terminateChildProcess(child);
        }
      }, pollMs);
    }
  });
}
