const GET_WORKBENCH_BASE_URL = process.env.GET_WORKBENCH_BASE_URL || "http://127.0.0.1:19829";

export async function getGetWorkbenchStatus(): Promise<{ ok: boolean } | null> {
  try {
    const response = await fetch(`${GET_WORKBENCH_BASE_URL}/api/health`);
    if (!response.ok) {
      return null;
    }
    return await response.json() as { ok: boolean };
  } catch {
    return null;
  }
}

export async function shutdownGetWorkbench(): Promise<boolean> {
  try {
    const response = await fetch(`${GET_WORKBENCH_BASE_URL}/shutdown`, { method: "POST" });
    return response.ok;
  } catch {
    return false;
  }
}

export { GET_WORKBENCH_BASE_URL };
