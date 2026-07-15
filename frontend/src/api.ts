import type { EventPage, HealthResponse, JobResponse, WebApi } from "./types";

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

export const webApi: WebApi = {
  getHealth: () => requestJson<HealthResponse>("/api/health"),
  getJob: (jobId: string) => requestJson<JobResponse>(`/api/jobs/${encodeURIComponent(jobId)}`),
  getEvents: (afterEventId: number) =>
    requestJson<EventPage>(`/api/job-events?after_event_id=${afterEventId}&limit=100`),
};
