import os from "node:os";
import path from "node:path";

export function defaultOutputDirectory(category = "transcripts"): string {
  return path.resolve(process.env.OPENCLI_TRANSCRIPT_OUT_DIR || path.join(os.homedir(), "Documents", "Openmd", category));
}
