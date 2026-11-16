import type { XiaoeRunState } from "./config-store.js";

const HELPER_BASE_URL = process.env.XIAOE_HELPER_BASE_URL || "http://127.0.0.1:19826";

export async function getHelperStatus(): Promise<{ ok: boolean; security?: { token?: string }; config: unknown; state: XiaoeRunState } | null> {
  try {
    const response = await fetch(`${HELPER_BASE_URL}/status`);
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

async function helperJsonHeaders(): Promise<Record<string, string>> {
  const status = await getHelperStatus();
  return {
    "Content-Type": "application/json",
    ...(status?.security?.token ? { "X-OpenCLI-Helper-Token": status.security.token } : {})
  };
}

export async function updateHelperConfig(body: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(`${HELPER_BASE_URL}/config`, {
    method: "POST",
    headers: await helperJsonHeaders(),
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`helper config update failed: ${response.status}`);
  }
  return await response.json();
}

export async function runHelperCurrent(body: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(`${HELPER_BASE_URL}/run-current`, {
    method: "POST",
    headers: await helperJsonHeaders(),
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`helper run failed: ${response.status}`);
  }
  return await response.json();
}

export async function readHelperCatalog(body: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(`${HELPER_BASE_URL}/catalog`, {
    method: "POST",
    headers: await helperJsonHeaders(),
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`helper catalog failed: ${response.status}`);
  }
  return await response.json();
}

export async function runHelperBatch(body: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(`${HELPER_BASE_URL}/batch-run`, {
    method: "POST",
    headers: await helperJsonHeaders(),
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`helper batch failed: ${response.status}`);
  }
  return await response.json();
}

export async function getHelperBatchStatus(): Promise<unknown | null> {
  try {
    const response = await fetch(`${HELPER_BASE_URL}/batch-status`);
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

export async function shutdownHelper(): Promise<boolean> {
  try {
    const response = await fetch(`${HELPER_BASE_URL}/shutdown`, {
      method: "POST",
      headers: await helperJsonHeaders()
    });
    return response.ok;
  } catch {
    return false;
  }
}

export { HELPER_BASE_URL };
