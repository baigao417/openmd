const TINGWU_HELPER_BASE_URL = process.env.TINGWU_HELPER_BASE_URL || "http://127.0.0.1:19827";
export async function getTingwuHelperStatus() {
    try {
        const response = await fetch(`${TINGWU_HELPER_BASE_URL}/status`);
        if (!response.ok)
            return null;
        return await response.json();
    }
    catch {
        return null;
    }
}
export async function updateTingwuHelperConfig(body) {
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
export async function runTingwuHelperCurrent(body) {
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
export async function shutdownTingwuHelper() {
    try {
        const response = await fetch(`${TINGWU_HELPER_BASE_URL}/shutdown`, { method: "POST" });
        return response.ok;
    }
    catch {
        return false;
    }
}
export { TINGWU_HELPER_BASE_URL };
