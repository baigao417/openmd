const GET_WORKBENCH_BASE_URL = process.env.GET_WORKBENCH_BASE_URL || "http://127.0.0.1:19829";
export async function getGetWorkbenchStatus() {
    try {
        const response = await fetch(`${GET_WORKBENCH_BASE_URL}/api/health`);
        if (!response.ok) {
            return null;
        }
        return await response.json();
    }
    catch {
        return null;
    }
}
export async function shutdownGetWorkbench() {
    try {
        const response = await fetch(`${GET_WORKBENCH_BASE_URL}/shutdown`, { method: "POST" });
        return response.ok;
    }
    catch {
        return false;
    }
}
export { GET_WORKBENCH_BASE_URL };
