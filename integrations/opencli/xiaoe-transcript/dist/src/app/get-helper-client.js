const GET_HELPER_BASE_URL = process.env.GET_HELPER_BASE_URL || "http://127.0.0.1:19828";
export async function getGetHelperStatus() {
    try {
        const response = await fetch(`${GET_HELPER_BASE_URL}/status`);
        if (!response.ok) {
            return null;
        }
        return await response.json();
    }
    catch {
        return null;
    }
}
export async function updateGetHelperConfig(body) {
    const response = await fetch(`${GET_HELPER_BASE_URL}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    });
    if (!response.ok) {
        throw new Error(`get helper config update failed: ${response.status}`);
    }
    return await response.json();
}
export async function runGetHelperExport(body) {
    const response = await fetch(`${GET_HELPER_BASE_URL}/run-export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    });
    if (!response.ok) {
        throw new Error(`get helper export failed: ${response.status}`);
    }
    return await response.json();
}
export async function shutdownGetHelper() {
    try {
        const response = await fetch(`${GET_HELPER_BASE_URL}/shutdown`, { method: "POST" });
        return response.ok;
    }
    catch {
        return false;
    }
}
export { GET_HELPER_BASE_URL };
