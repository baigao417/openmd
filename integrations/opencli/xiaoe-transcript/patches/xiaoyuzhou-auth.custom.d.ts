interface XiaoyuzhouCredentialInput {
  access_token?: string;
  refresh_token?: string;
  device_id?: string;
  expires_in?: number;
  expires_at?: number;
  expires_source?: string;
}

interface NormalizedXiaoyuzhouCredentials {
  access_token: string;
  refresh_token: string;
  expires_at: number;
  refresh_at: number;
  expires_source: string;
  device_id: string;
  device_properties: string;
}

export function normalizeXiaoyuzhouCredentials(
  raw?: XiaoyuzhouCredentialInput,
  options?: { now?: number }
): NormalizedXiaoyuzhouCredentials;

export function refreshXiaoyuzhouCredentials(
  credentials: XiaoyuzhouCredentialInput,
  fetchImpl?: typeof fetch,
  options?: { now?: number }
): Promise<NormalizedXiaoyuzhouCredentials>;

export function redactXiaoyuzhouText(value: string, secrets?: string[]): string;
