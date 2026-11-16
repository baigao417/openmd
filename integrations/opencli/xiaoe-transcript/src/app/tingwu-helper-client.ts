import type { TranscriptRunState } from "./transcript-config-store.js";

const TINGWU_HELPER_BASE_URL = process.env.TINGWU_HELPER_BASE_URL || "http://127.0.0.1:19827";

export async function getTingwuHelperStatus(): Promise<{ ok: boolean; config: unknown; state: TranscriptRunState } | null> {
  try {
    const response = await fetch(`${TINGWU_HELPER_BASE_URL}/status`);
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

export async function updateTingwuHelperConfig(body: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(`${TINGWU_HELPER_BASE_URL}/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`tingwu helper config update failed: ${response.status}`);
  }
  return await response.json();
}

export async function runTingwuHelperCurrent(body: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(`${TINGWU_HELPER_BASE_URL}/run-current`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`tingwu helper run failed: ${response.status}`);
  }
  return await response.json();
}

export async function shutdownTingwuHelper(): Promise<boolean> {
  try {
    const response = await fetch(`${TINGWU_HELPER_BASE_URL}/shutdown`, { method: "POST" });
    return response.ok;
  } catch {
    return false;
  }
}

export { TINGWU_HELPER_BASE_URL };
