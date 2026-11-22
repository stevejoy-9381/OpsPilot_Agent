export type ComponentStatus = 'ok' | 'degraded';

export interface ReadyzResponse {
  status: ComponentStatus;
  components: {
    postgres: ComponentStatus;
    redis: ComponentStatus;
  };
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public statusText: string,
    public data?: unknown
  ) {
    super(`API Error ${status}: ${statusText}`);
    this.name = 'ApiError';
  }
}

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL !== undefined &&
  import.meta.env.VITE_API_BASE_URL !== ''
    ? import.meta.env.VITE_API_BASE_URL
    : 'http://localhost:8000';

export async function apiFetch<T>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  const normalizedBase = API_BASE_URL.replace(/\/$/, '');
  const normalizedEndpoint = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;
  const url = `${normalizedBase}${normalizedEndpoint}`;

  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  const contentType = response.headers.get('content-type');
  const isJson = contentType && contentType.includes('application/json');
  const data = isJson ? await response.json() : await response.text();

  // /readyz returns 503 when degraded; that payload is still a valid response
  if (!response.ok && response.status !== 503) {
    throw new ApiError(response.status, response.statusText, data);
  }

  return data as T;
}

export async function getReadyz(): Promise<ReadyzResponse> {
  return apiFetch<ReadyzResponse>('/readyz');
}
