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

export interface WebApi {
  getHealth(): Promise<HealthResponse>;
  getJob(jobId: string): Promise<JobResponse>;
  getEvents(afterEventId: number): Promise<EventPage>;
}
