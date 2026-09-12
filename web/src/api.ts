export interface Confirmation {
  seq: number;
  barcode: string;
  confirmed_at: string;
}

export interface BatchState {
  id: string;
  state: 'open' | 'complete';
  expected_seq: number;
  total: number;
  barcodes: string[];
  confirmed: Confirmation[];
}

export type RejectReason =
  | 'stale_conflict'
  | 'out_of_order'
  | 'unknown_barcode'
  | 'barcode_already_accepted'
  | 'completed';

export interface SubmitResponse {
  replay: boolean;
  accepted: boolean;
  reason: 'accepted' | 'replay' | RejectReason;
  seq?: number;
  barcode?: string;
  confirmed_at?: string;
  expected_seq: number;
  state: 'open' | 'complete';
  total: number;
  confirmed: Confirmation[];
}

export class RequestTimeoutError extends Error {
  constructor() {
    super('请求超时，服务端可能已确认');
    this.name = 'RequestTimeoutError';
  }
}

const DEFAULT_TIMEOUT_MS = 12_000;

async function request<T>(
  path: string,
  options: RequestInit,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<{ status: number; body: T }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      ...options,
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
    });
    const body = (await res.json()) as T;
    return { status: res.status, body };
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new RequestTimeoutError();
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function requestOrThrow<T>(
  path: string,
  options: RequestInit,
): Promise<T> {
  const { status, body } = await request<T & { detail?: string }>(path, options);
  if (!status.toString().startsWith('2')) {
    throw new Error(body.detail ?? `请求失败（HTTP ${status}）`);
  }
  return body;
}

export function createBatch(barcodes: string[]): Promise<BatchState> {
  return requestOrThrow<BatchState>('/api/batches', {
    method: 'POST',
    body: JSON.stringify({ barcodes }),
  });
}

export function fetchBatch(id: string): Promise<BatchState> {
  return requestOrThrow<BatchState>(`/api/batches/${id}`, { method: 'GET' });
}

// 提交可能返回 200（接受/重放）或 409（迟到、跳号、冻结校验失败）；
// 两种情况都携带权威的 expected_seq，交由调用方按 accepted/reason 裁决。
export async function submitBarcode(
  id: string,
  seq: number,
  barcode: string,
): Promise<SubmitResponse> {
  const { status, body } = await request<SubmitResponse>(
    `/api/batches/${id}/submissions`,
    { method: 'POST', body: JSON.stringify({ seq, barcode }) },
  );
  if (status !== 200 && status !== 409) {
    throw new Error(`提交失败（HTTP ${status}）`);
  }
  return body;
}
