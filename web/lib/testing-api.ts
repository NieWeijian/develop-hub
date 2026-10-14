const API_PREFIX = '/api/testing';

export function testingApiBase(): string {
  if (process.env.NEXT_PUBLIC_DEVELOP_HUB_API_URL) return process.env.NEXT_PUBLIC_DEVELOP_HUB_API_URL.replace(/\/$/, '');
  if (typeof window !== 'undefined') return `${window.location.protocol}//${window.location.hostname}:8175`;
  return 'http://127.0.0.1:8175';
}

function errorText(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(errorText).join('；');
  if (detail && typeof detail === 'object') {
    const issue = detail as { message?: string; msg?: string; errors?: unknown[]; loc?: string[] };
    if (issue.errors?.length) return issue.errors.map(errorText).join('；');
    return [issue.loc?.filter(part => part !== 'body').join(' · '), issue.message || issue.msg].filter(Boolean).join('：') || '请求未完成，请检查配置后重试。';
  }
  return '请求未完成，请稍后重试。';
}

export async function testingApi<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(`${testingApiBase()}${API_PREFIX}${path}`, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: unknown; message?: unknown } | null;
    throw new Error(body ? errorText(body.detail || body.message) : `服务暂时不可用（${response.status}），请稍后重试。`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function testingPost<T>(path: string, body: unknown = {}, method = 'POST'): Promise<T> {
  return testingApi<T>(path, { method, body: JSON.stringify(body) });
}

export function requestKey(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function failureMessage(error: unknown): string {
  return error instanceof Error ? error.message : '操作未完成，请重试。';
}
