import type {
  EodEnqueueResponse,
  EodRequest,
  EventPage,
  HealthResponse,
  JobResponse,
  WebApi,
} from "./types";

interface ErrorEnvelope {
  error?: { message?: string };
}

async function requestJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let message = "请求失败。";
    try {
      const body = (await response.json()) as ErrorEnvelope;
      if (body.error?.message) message = body.error.message;
    } catch {
      // Keep the stable public fallback instead of exposing transport details.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

async function sendJson<T>(
  path: string,
  body: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let message = "请求失败。";
    try {
      const value = (await response.json()) as ErrorEnvelope;
      if (value.error?.message) message = value.error.message;
    } catch {
      // Keep the public fallback.
    }
    const error = new Error(message);
    Object.assign(error, { status: response.status });
    throw error;
  }
  return (await response.json()) as T;
}

export const webApi: WebApi = {
  getHealth: () => requestJson<HealthResponse>("/api/health"),
  getJob: (jobId: string) => requestJson<JobResponse>(`/api/jobs/${encodeURIComponent(jobId)}`),
  getEvents: (afterEventId: number) =>
    requestJson<EventPage>(`/api/job-events?after_event_id=${afterEventId}&limit=100`),
  getData: (path: string) => requestJson<unknown>(path),
  enqueueEod: (request: EodRequest, idempotencyKey: string) =>
    sendJson<EodEnqueueResponse>("/api/data/eod-runs", request, {
      "Idempotency-Key": idempotencyKey,
    }),
  cancelJob: (jobId: string, rowVersion: number, reason: string) =>
    sendJson<JobResponse>(
      `/api/jobs/${encodeURIComponent(jobId)}/cancel`,
      { expected_row_version: rowVersion, reason },
    ),
};
