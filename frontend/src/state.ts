import type { JobEvent } from "./types";

export type ConnectionStatus = "connecting" | "connected" | "polling" | "reconnecting" | "error";

export interface StreamState {
  cursor: number;
  events: JobEvent[];
  connection: ConnectionStatus;
}

export type StreamAction =
  | { type: "events"; events: JobEvent[] }
  | { type: "connection"; connection: ConnectionStatus };

export const initialStreamState: StreamState = {
  cursor: 0,
  events: [],
  connection: "connecting",
};

export function streamReducer(state: StreamState, action: StreamAction): StreamState {
  if (action.type === "connection") {
    return state.connection === action.connection ? state : { ...state, connection: action.connection };
  }
  const fresh = action.events
    .filter((event) => event.event_id > state.cursor)
    .sort((left, right) => left.event_id - right.event_id)
    .filter((event, index, events) => index === 0 || event.event_id !== events[index - 1].event_id);
  if (fresh.length === 0) return state;
  return {
    ...state,
    cursor: fresh[fresh.length - 1].event_id,
    events: [...state.events, ...fresh],
  };
}
