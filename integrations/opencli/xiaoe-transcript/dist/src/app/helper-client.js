const HELPER_BASE_URL = process.env.XIAOE_HELPER_BASE_URL || "http://127.0.0.1:19826";
export async function getHelperStatus() {
    try {
        const response = await fetch(`${HELPER_BASE_URL}/status`);
        if (!response.ok)
            return null;
        return await response.json();
    }
    catch {
        return null;
    }
}
async function helperJsonHeaders() {
    const status = await getHelperStatus();
    return {
        "Content-Type": "application/json",
        ...(status?.security?.token ? { "X-OpenCLI-Helper-Token": status.security.token } : {})
    };
}
export async function updateHelperConfig(body) {
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
export async function runHelperCurrent(body) {
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
export async function readHelperCatalog(body) {
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
export async function runHelperBatch(body) {
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
export async function getHelperBatchStatus() {
    try {
        const response = await fetch(`${HELPER_BASE_URL}/batch-status`);
        if (!response.ok)
            return null;
        return await response.json();
    }
    catch {
        return null;
    }
}
export async function shutdownHelper() {
    try {
        const response = await fetch(`${HELPER_BASE_URL}/shutdown`, {
            method: "POST",
            headers: await helperJsonHeaders()
        });
        return response.ok;
    }
    catch {
        return false;
    }
}
export { HELPER_BASE_URL };
