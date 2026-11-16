import { beforeEach, describe, expect, it, vi } from 'vitest';

const errors = vi.hoisted(() => ({
  CliError: class CliError extends Error {},
  CommandExecutionError: class CommandExecutionError extends Error {},
  ConfigError: class ConfigError extends Error {},
  EXIT_CODES: { NOPERM: 77 },
  getErrorMessage: (error: unknown) => error instanceof Error ? error.message : String(error),
}));

vi.mock('@jackwener/opencli/errors', () => errors);

const fsMocks = vi.hoisted(() => ({
  existsSync: vi.fn(), readFileSync: vi.fn(), mkdirSync: vi.fn(), writeFileSync: vi.fn(), renameSync: vi.fn(),
}));
vi.mock('node:fs', () => fsMocks);
vi.mock('node:os', () => ({ homedir: () => '/Users/tester' }));

const auth = await import('../patches/xiaoyuzhou-auth.custom.js');

function response(status: number, payload: unknown) {
  return { ok: status >= 200 && status < 300, status, text: vi.fn().mockResolvedValue(JSON.stringify(payload)) };
}

describe('xiaoyuzhou auth patch', () => {
  beforeEach(() => {
    Object.values(fsMocks).forEach(mock => mock.mockReset());
    vi.useRealTimers();
  });

  it('uses server expiry or JWT exp and never guesses the legacy 20-minute TTL', () => {
    const now = Date.parse('2026-08-30T00:00:00Z');
    expect(auth.normalizeXiaoyuzhouCredentials({ access_token: 'a', refresh_token: 'r', expires_in: 3600 }, { now })).toMatchObject({ expires_at: now + 3600000, expires_source: 'response.expires_in' });
    const header = Buffer.from(JSON.stringify({ exp: Math.floor((now + 7200000) / 1000) })).toString('base64url');
    expect(auth.normalizeXiaoyuzhouCredentials({ access_token: `a.${header}.x`, refresh_token: 'r' }, { now })).toMatchObject({ expires_at: now + 7200000, expires_source: 'access_token.jwt.exp' });
    expect(auth.normalizeXiaoyuzhouCredentials({ access_token: 'a', refresh_token: 'r' }, { now })).toMatchObject({ expires_at: 0, refresh_at: 0, expires_source: 'unknown' });
  });

  it('preserves an unrotated refresh token and persists atomically', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(200, { success: true, 'x-jike-access-token': 'new-access', expires_in: 3600 }));
    const result = await auth.refreshXiaoyuzhouCredentials({ access_token: 'old-access', refresh_token: 'old-refresh', device_id: 'd' }, fetchMock, { now: Date.now() });
    expect(result.refresh_token).toBe('old-refresh');
    expect(result.expires_source).toBe('response.expires_in');
    expect(fsMocks.renameSync).toHaveBeenCalled();
    const written = fsMocks.writeFileSync.mock.calls[0][1] as string;
    expect(written).not.toContain('old-access');
    expect(written).toContain('new-access');
  });

  it('coalesces concurrent refreshes and redacts token-shaped response text', async () => {
    let resolve!: (value: unknown) => void;
    const pending = new Promise(r => { resolve = r; });
    const fetchMock = vi.fn().mockReturnValue(pending);
    const credentials = { access_token: 'old-access', refresh_token: 'old-refresh', device_id: 'd' };
    const a = auth.refreshXiaoyuzhouCredentials(credentials, fetchMock);
    const b = auth.refreshXiaoyuzhouCredentials(credentials, fetchMock);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolve(response(200, { success: true, 'x-jike-access-token': 'new-access', 'x-jike-refresh-token': 'new-refresh', expires_at: Date.now() + 3600000 }));
    await expect(Promise.all([a, b])).resolves.toHaveLength(2);
    expect(auth.redactXiaoyuzhouText('refresh=old-refresh access=old-access', ['old-refresh', 'old-access'])).toBe('refresh=[REDACTED] access=[REDACTED]');
  });

  it('retries transient refresh failures but does not retry auth failures', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(503, { message: 'temporary old-refresh' }))
      .mockResolvedValueOnce(response(200, { success: true, 'x-jike-access-token': 'new-access', 'x-jike-refresh-token': 'new-refresh', expires_in: 3600 }));
    await expect(auth.refreshXiaoyuzhouCredentials({ access_token: 'old-access', refresh_token: 'old-refresh', device_id: 'd' }, fetchMock)).resolves.toMatchObject({ access_token: 'new-access' });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const unauthorized = vi.fn().mockResolvedValue(response(401, { message: 'invalid old-refresh' }));
    await expect(auth.refreshXiaoyuzhouCredentials({ access_token: 'old-access', refresh_token: 'old-refresh', device_id: 'd2' }, unauthorized)).rejects.toThrow('AUTH_REQUIRED');
    expect(unauthorized).toHaveBeenCalledTimes(1);
  });
});
