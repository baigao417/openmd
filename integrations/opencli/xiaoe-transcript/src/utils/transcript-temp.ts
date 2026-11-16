import { lstat, mkdtemp, realpath } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

/** Explicit interrupted-run recovery; downloaded segments still pass URL identity checks. */
export async function createTranscriptTempDir(resumeDir = process.env.OPENCLI_XIAOE_RESUME_DIR): Promise<string> {
  if (!resumeDir?.trim()) return await mkdtemp(path.join(os.tmpdir(), "xiaoe-to-md-"));
  const directory = path.resolve(resumeDir.trim());
  const stat = await lstat(directory);
  const actual = await realpath(directory);
  const tempRoot = await realpath(os.tmpdir());
  if (!stat.isDirectory() || stat.isSymbolicLink()
      || path.dirname(actual).toLowerCase() !== tempRoot.toLowerCase()
      || !/^xiaoe-to-md-[a-z0-9]+$/i.test(path.basename(actual))) {
    throw new Error("Resume directory must be an existing Xiaoe transcript temp directory");
  }
  const attempts = await lstat(path.join(actual, "download-attempts"));
  if (!attempts.isDirectory() || attempts.isSymbolicLink()) {
    throw new Error("Resume directory has no regular download-attempts directory");
  }
  return actual;
}
