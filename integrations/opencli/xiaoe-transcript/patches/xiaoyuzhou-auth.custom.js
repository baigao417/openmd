import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { CliError, CommandExecutionError, ConfigError, EXIT_CODES, getErrorMessage } from '@jackwener/opencli/errors';

// Xiaoyuzhou auth hardening patch: server expiry + refresh rotation + observable retries.
export const XIAOYUZHOU_API_BASE_URL = 'https://api.xiaoyuzhoufm.com';
// Kept for callers that import the old constant. It is no longer used to infer expiry.
export const XIAOYUZHOU_TOKEN_TTL_MS = 20 * 60 * 1000;
export const XIAOYUZHOU_REFRESH_SKEW_MS = 60 * 1000;
export const XIAOYUZHOU_REFRESH_MAX_ATTEMPTS = 3;
export const XIAOYUZHOU_DEFAULT_DEVICE_ID = '81ADBFD6-6921-482B-9AB9-A29E7CC7BB55';
export const XIAOYUZHOU_DEFAULT_DEVICE_PROPERTIES = '';
export const XIAOYUZHOU_DEFAULT_USER_AGENT = 'Xiaoyuzhou/2.98.0 (build:2908; iOS 26.2.1)';

const refreshInFlight = new Map();
const getNowMs = () => Date.now();

export function getXiaoyuzhouCredentialFile() {
    return path.join(os.homedir(), '.opencli', 'xiaoyuzhou.json');
}

function createXiaoyuzhouAuthError(message) {
    return new CliError('AUTH_REQUIRED', message, `Update ${getXiaoyuzhouCredentialFile()} with fresh Xiaoyuzhou credentials before retrying.`, EXIT_CODES.NOPERM);
}

function finiteNumber(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
}

function toEpochMs(value) {
    const parsed = finiteNumber(value);
    if (!parsed) return 0;
    return parsed < 10_000_000_000 ? parsed * 1000 : parsed;
}

function decodeJwtExp(token) {
    if (typeof token !== 'string') return 0;
    const parts = token.split('.');
    if (parts.length !== 3) return 0;
    try {
        const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
        return toEpochMs(payload?.exp);
    } catch {
        return 0;
    }
}

function expiryFrom(raw, accessToken, now) {
    const explicit = toEpochMs(raw.expires_at ?? raw.expiresAt);
    if (explicit) return { expires_at: explicit, expires_source: 'response.expires_at' };
    const expiresIn = finiteNumber(raw.expires_in ?? raw.expiresIn);
    if (expiresIn > 0) return { expires_at: now + (expiresIn < 10_000_000 ? expiresIn * 1000 : expiresIn), expires_source: 'response.expires_in' };
    const jwtExp = decodeJwtExp(accessToken);
    if (jwtExp) return { expires_at: jwtExp, expires_source: 'access_token.jwt.exp' };
    return { expires_at: 0, expires_source: 'unknown' };
}

export function normalizeXiaoyuzhouCredentials(raw = {}, options = {}) {
    const now = options.now ?? getNowMs();
    const accessToken = String(raw.access_token ?? raw.accessToken ?? '').trim();
    const expiry = expiryFrom(raw, accessToken, now);
    return {
        access_token: accessToken,
        refresh_token: String(raw.refresh_token ?? raw.refreshToken ?? '').trim(),
        expires_at: expiry.expires_at,
        // refresh_at is local policy, while expires_at remains server/JWT truth.
        refresh_at: expiry.expires_at > 0 ? expiry.expires_at - XIAOYUZHOU_REFRESH_SKEW_MS : 0,
        expires_source: raw.expires_source ?? expiry.expires_source,
        device_id: String(raw.device_id ?? raw.deviceId ?? XIAOYUZHOU_DEFAULT_DEVICE_ID).trim() || XIAOYUZHOU_DEFAULT_DEVICE_ID,
        device_properties: String(raw.device_properties ?? raw.deviceProperties ?? XIAOYUZHOU_DEFAULT_DEVICE_PROPERTIES),
    };
}

export function loadXiaoyuzhouCredentials() {
    const filePath = getXiaoyuzhouCredentialFile();
    if (!fs.existsSync(filePath)) throw new ConfigError(`Missing Xiaoyuzhou credentials. Expected ${filePath}`, `Create ${filePath} with access_token and refresh_token.`);
    try {
        const credentials = normalizeXiaoyuzhouCredentials(JSON.parse(fs.readFileSync(filePath, 'utf-8')));
        if (!credentials.access_token || !credentials.refresh_token) throw new ConfigError(`Xiaoyuzhou credential file is missing access_token or refresh_token: ${filePath}`, 'Recreate the file with valid credentials.');
        return credentials;
    } catch (error) {
        if (error instanceof ConfigError) throw error;
        throw new ConfigError(`Failed to parse Xiaoyuzhou credential file: ${filePath}`, `Ensure ${filePath} contains valid JSON. (${getErrorMessage(error)})`);
    }
}

export function saveXiaoyuzhouCredentials(credentials) {
    const filePath = getXiaoyuzhouCredentialFile();
    const tempPath = `${filePath}.tmp-${process.pid}-${Date.now()}`;
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    const payload = JSON.stringify({
        access_token: credentials.access_token,
        refresh_token: credentials.refresh_token,
        expires_at: credentials.expires_at,
        refresh_at: credentials.refresh_at,
        expires_source: credentials.expires_source,
        device_id: credentials.device_id,
        device_properties: credentials.device_properties,
    }, null, 2) + '\n';
    fs.writeFileSync(tempPath, payload, { encoding: 'utf-8', mode: 0o600 });
    fs.renameSync(tempPath, filePath);
}

export function shouldRefreshXiaoyuzhouCredentials(credentials, now = getNowMs()) {
    const refreshAt = finiteNumber(credentials.refresh_at) || (finiteNumber(credentials.expires_at) ? finiteNumber(credentials.expires_at) - XIAOYUZHOU_REFRESH_SKEW_MS : 0);
    return refreshAt > 0 && now >= refreshAt;
}

export function redactXiaoyuzhouText(value, secrets = []) {
    let text = String(value ?? '');
    for (const secret of secrets) if (secret) text = text.split(String(secret)).join('[REDACTED]');
    return text.length > 300 ? `${text.slice(0, 300)}...` : text;
}

function emitAuthEvent(onEvent, event, details = {}) {
    if (typeof onEvent !== 'function') return;
    try { onEvent({ provider: 'xiaoyuzhou', event, ...details }); } catch { /* observability must never break auth */ }
}

export function buildXiaoyuzhouHeaders(credentials, options = {}) {
    const { contentType = 'application/json', includeLocalTime = false, includeRefreshToken = false } = options;
    const headers = {
        'Content-Type': contentType, Host: 'api.xiaoyuzhoufm.com', 'User-Agent': XIAOYUZHOU_DEFAULT_USER_AGENT,
        Market: 'AppStore', 'App-BuildNo': '2908', OS: 'ios', Manufacturer: 'Apple', BundleID: 'app.podcast.cosmos',
        Connection: 'keep-alive', 'abtest-info': '{"old_user_discovery_feed":"enable"}', 'Accept-Language': 'en-HK;q=1.0, zh-Hans-HK;q=0.9',
        Model: 'iPhone18,1', 'app-permissions': '100000', Accept: '*/*', 'App-Version': '2.98.0', WifiConnected: 'true', 'OS-Version': '26.2.1',
        'x-custom-xiaoyuzhou-app-dev': '', 'x-jike-device-id': credentials.device_id || XIAOYUZHOU_DEFAULT_DEVICE_ID,
        'x-jike-device-properties': credentials.device_properties ?? XIAOYUZHOU_DEFAULT_DEVICE_PROPERTIES,
    };
    if (credentials.access_token) headers['x-jike-access-token'] = credentials.access_token;
    if (includeRefreshToken && credentials.refresh_token) headers['x-jike-refresh-token'] = credentials.refresh_token;
    if (includeLocalTime) { headers['Local-Time'] = new Date().toISOString(); headers.Timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
    return headers;
}

async function refreshOnce(credentials, fetchImpl, options) {
    const { onEvent, now = getNowMs() } = options;
    emitAuthEvent(onEvent, 'refresh_started');
    let response;
    try {
        response = await fetchImpl(`${XIAOYUZHOU_API_BASE_URL}/app_auth_tokens.refresh`, {
            method: 'POST',
            headers: buildXiaoyuzhouHeaders(credentials, { contentType: 'application/x-www-form-urlencoded; charset=utf-8', includeLocalTime: true, includeRefreshToken: true }),
            signal: AbortSignal.timeout(20_000),
        });
    } catch (error) {
        const wrapped = new CommandExecutionError(`Failed to refresh Xiaoyuzhou credentials: ${getErrorMessage(error)}`);
        wrapped.retryable = true;
        throw wrapped;
    }
    const bodyText = await response.text();
    if (!response.ok) {
        const message = `Xiaoyuzhou token refresh failed with HTTP ${response.status}${bodyText ? `: ${redactXiaoyuzhouText(bodyText, [credentials.access_token, credentials.refresh_token])}` : ''}`;
        if (response.status === 408 || response.status === 429 || response.status >= 500) {
            const transient = new Error(message);
            transient.retryable = true;
            throw transient;
        }
        throw createXiaoyuzhouAuthError(message);
    }
    let parsed;
    try { parsed = JSON.parse(bodyText); } catch (error) { throw new CommandExecutionError(`Xiaoyuzhou refresh returned invalid JSON: ${getErrorMessage(error)}`); }
    if (!parsed?.success) throw createXiaoyuzhouAuthError(`Xiaoyuzhou refresh API returned success=false${parsed?.message ? `: ${redactXiaoyuzhouText(parsed.message)}` : ''}`);
    const header = (name) => response.headers?.get?.(name) ?? response.headers?.get?.(name.toLowerCase());
    const accessToken = String(parsed['x-jike-access-token'] ?? parsed.access_token ?? parsed.accessToken ?? header('x-jike-access-token') ?? '').trim();
    const refreshToken = String(parsed['x-jike-refresh-token'] ?? parsed.refresh_token ?? parsed.refreshToken ?? header('x-jike-refresh-token') ?? credentials.refresh_token).trim();
    const source = parsed.data && typeof parsed.data === 'object' ? { ...parsed.data, ...parsed } : { ...parsed };
    if (source.expires_at === undefined && source.expiresAt === undefined) source.expires_at = header('expires_at') ?? header('expires-at');
    if (source.expires_in === undefined && source.expiresIn === undefined) source.expires_in = header('expires_in') ?? header('expires-in');
    const next = normalizeXiaoyuzhouCredentials({ ...credentials, ...source, access_token: accessToken, refresh_token: refreshToken }, { now });
    if (!next.access_token || !next.refresh_token) throw createXiaoyuzhouAuthError('Xiaoyuzhou refresh API returned empty access_token or refresh_token');
    saveXiaoyuzhouCredentials(next);
    emitAuthEvent(onEvent, 'refresh_succeeded', { expires_source: next.expires_source });
    return next;
}

export async function refreshXiaoyuzhouCredentials(credentials, fetchImpl = fetch, options = {}) {
    if (!credentials.refresh_token) throw createXiaoyuzhouAuthError('Xiaoyuzhou refresh token is missing');
    const key = `${credentials.device_id}:${credentials.refresh_token}`;
    if (refreshInFlight.has(key)) return refreshInFlight.get(key);
    const task = (async () => {
        let lastError;
        for (let attempt = 1; attempt <= XIAOYUZHOU_REFRESH_MAX_ATTEMPTS; attempt += 1) {
            try { return await refreshOnce(credentials, fetchImpl, options); }
            catch (error) {
                lastError = error;
                const retryable = error?.retryable === true && attempt < XIAOYUZHOU_REFRESH_MAX_ATTEMPTS;
                if (!retryable) throw error;
                emitAuthEvent(options.onEvent, 'refresh_retry', { attempt, reason: getErrorMessage(error) });
                await new Promise(resolve => setTimeout(resolve, 250 * 2 ** (attempt - 1)));
            }
        }
        throw lastError;
    })();
    refreshInFlight.set(key, task);
    try { return await task; } finally { refreshInFlight.delete(key); }
}

function buildApiUrl(endpoint, query) {
    const url = new URL(endpoint, XIAOYUZHOU_API_BASE_URL);
    for (const [key, value] of Object.entries(query ?? {})) if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, String(value));
    return url.toString();
}

async function performXiaoyuzhouJsonRequest(endpoint, options, credentials, fetchImpl) {
    try {
        return await fetchImpl(buildApiUrl(endpoint, options.query), {
            method: options.method ?? 'GET', headers: buildXiaoyuzhouHeaders(credentials, { includeLocalTime: true }),
            body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: AbortSignal.timeout(20_000),
        });
    } catch (error) { throw new CommandExecutionError(`Failed to reach Xiaoyuzhou API: ${getErrorMessage(error)}`); }
}

export async function requestXiaoyuzhouJson(endpoint, options = {}, fetchImpl = fetch) {
    let credentials = options.credentials ?? loadXiaoyuzhouCredentials();
    if (shouldRefreshXiaoyuzhouCredentials(credentials)) credentials = await refreshXiaoyuzhouCredentials(credentials, fetchImpl, options);
    let response = await performXiaoyuzhouJsonRequest(endpoint, options, credentials, fetchImpl);
    if (response.status === 401) {
        emitAuthEvent(options.onEvent, 'request_unauthorized');
        credentials = await refreshXiaoyuzhouCredentials(credentials, fetchImpl, options);
        response = await performXiaoyuzhouJsonRequest(endpoint, options, credentials, fetchImpl);
    }
    const bodyText = await response.text();
    if (!response.ok) throw new CommandExecutionError(`Xiaoyuzhou API request failed with HTTP ${response.status}${bodyText ? `: ${redactXiaoyuzhouText(bodyText, [credentials.access_token, credentials.refresh_token])}` : ''}`);
    let parsed;
    try { parsed = JSON.parse(bodyText); } catch (error) { throw new CommandExecutionError(`Xiaoyuzhou API returned invalid JSON: ${getErrorMessage(error)}`); }
    if (parsed?.success === false) throw new CommandExecutionError(redactXiaoyuzhouText(parsed?.message || parsed?.msg || 'Xiaoyuzhou API returned success=false'));
    return { credentials, raw: parsed, data: parsed?.data };
}

export async function fetchXiaoyuzhouTranscriptBody(url, fetchImpl = fetch) {
    let response;
    try { response = await fetchImpl(url, { method: 'GET', headers: { 'User-Agent': XIAOYUZHOU_DEFAULT_USER_AGENT, Accept: '*/*', Market: 'AppStore' }, signal: AbortSignal.timeout(20_000) }); }
    catch (error) { throw new CommandExecutionError(`Failed to fetch Xiaoyuzhou transcript content: ${getErrorMessage(error)}`); }
    const bodyText = await response.text();
    if (!response.ok) throw new CommandExecutionError(`Xiaoyuzhou transcript download failed with HTTP ${response.status}${bodyText ? `: ${redactXiaoyuzhouText(bodyText)}` : ''}`);
    return bodyText;
}

export function extractTranscriptText(transcriptBody) {
    let parsed;
    try { parsed = JSON.parse(transcriptBody); } catch { return { text: '', segmentCount: 0 }; }
    let items = Array.isArray(parsed) ? parsed : [];
    if (!items.length && parsed && typeof parsed === 'object') {
        for (const key of ['segments', 'data', 'transcript', 'items']) if (Array.isArray(parsed[key])) { items = parsed[key]; break; }
        if (!items.length && typeof parsed.text === 'string' && parsed.text.trim()) return { text: parsed.text.trim(), segmentCount: 1 };
    }
    const textItems = items.filter(item => item && typeof item.text === 'string').map(item => item.text.trim()).filter(Boolean);
    return { text: textItems.join('\n'), segmentCount: textItems.length };
}
