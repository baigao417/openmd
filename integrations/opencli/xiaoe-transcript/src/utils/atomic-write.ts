import { randomUUID } from "node:crypto";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

const writeQueues = new Map<string, Promise<void>>();

async function atomicWriteUtf8Now(filePath: string, data: string): Promise<void> {
  await mkdir(path.dirname(filePath), { recursive: true });
  const tempPath = path.join(
    path.dirname(filePath),
    `.${path.basename(filePath)}.${process.pid}.${Date.now()}.${randomUUID()}.tmp`
  );
  try {
    await writeFile(tempPath, data, "utf8");
    try {
      await rename(tempPath, filePath);
    } catch (error) {
      const code = typeof error === "object" && error !== null && "code" in error ? String(error.code) : "";
      if (code !== "EPERM" && code !== "EEXIST") {
        throw error;
      }
      await rm(filePath, { force: true });
      await rename(tempPath, filePath);
    }
  } catch (error) {
    await rm(tempPath, { force: true }).catch(() => undefined);
    throw error;
  }
}

export async function atomicWriteUtf8(filePath: string, data: string): Promise<void> {
  const queueKey = path.resolve(filePath);
  const previous = writeQueues.get(queueKey) ?? Promise.resolve();
  const next = previous.catch(() => undefined).then(async () => {
    await atomicWriteUtf8Now(filePath, data);
  });
  writeQueues.set(queueKey, next);
  try {
    await next;
  } finally {
    if (writeQueues.get(queueKey) === next) {
      writeQueues.delete(queueKey);
    }
  }
}
