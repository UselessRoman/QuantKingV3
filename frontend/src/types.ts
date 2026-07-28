export interface HealthResponse {
  status: "ok";
  service: string;
  checked_at: string;
}

export interface FailureResponse {
  error_type: string;
  message: string;
  retryable: boolean;
}

export interface JobResponse {
  job_id: string;
  job_type: string;
  status: string;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  heartbeat_at: string | null;
  stage: string | null;
  progress: number;
  worker_id: string | null;
  failure: FailureResponse | null;
  row_version: number;
}

export interface JobEvent {
  event_id: number;
  job_id: string;
  event_type: string;
  stage: string | null;
  progress: number | null;
  summary: string;
  occurred_at: string;
}

export interface EventPage {
  events: JobEvent[];
  next_cursor: number;
}

export interface CursorPage<T = Record<string, unknown>> {
  items: T[];
  next_cursor: string | null;
}

export interface EodRequest {
  start_date: string;
  end_date: string;
  datasets: string[];
  publish_intent: "PUBLISH_AFTER_QUALITY";
  base_versions: Record<string, string>;
}

export interface EodEnqueueResponse {
  job_id: string;
  status: string;
  row_version: number;
  created: boolean;
}

export interface WebApi {
  getHealth(): Promise<HealthResponse>;
  getJob(jobId: string): Promise<JobResponse>;
  getEvents(afterEventId: number): Promise<EventPage>;
  getData?(path: string): Promise<unknown>;
  enqueueEod?(request: EodRequest, idempotencyKey: string): Promise<EodEnqueueResponse>;
  cancelJob?(jobId: string, rowVersion: number, reason: string): Promise<JobResponse>;
}
