import type {Message, TasksPayload} from "./types";

export const MAX_FILE_BYTES = 20 * 1024 * 1024;

export class RequestError extends Error {
  constructor(message: string, readonly status = 0, readonly kind = "request") {
    super(message);
  }
}

export async function requestJSON<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new RequestError("Network connection unavailable", 0, "network");
  }
  if (response.status === 401) {
    window.location.assign("/login");
    throw new RequestError("Your session has expired", 401, "auth");
  }
  const raw = await response.text();
  let payload: unknown = null;
  try {
    payload = raw ? JSON.parse(raw) : null;
  } catch {
    throw new RequestError("The server returned an invalid response", response.status, "invalid-json");
  }
  if (!response.ok) {
    const message = typeof payload === "object" && payload && "error" in payload
      ? String(payload.error) : `Request failed (${response.status})`;
    throw new RequestError(message, response.status);
  }
  return payload as T;
}

export const api = {
  tasks: () => requestJSON<TasksPayload>("/api/tasks"),
  messages: (id: number, after = 0) => requestJSON<{
    messages: Message[]; message_count?: number; pending_message_count?: number;
  }>(`/api/tasks/${id}/messages?after=${after}&limit=50`),
  createTask: (body: unknown) => requestJSON<{id: number; warning?: string}>("/api/tasks", post(body)),
  sendMessage: (id: number, body: unknown) => requestJSON<{message: Message; warning?: string}>(
    `/api/tasks/${id}/messages`, post(body),
  ),
  setup: (body: unknown) => requestJSON<Record<string, unknown>>("/api/setup", post(body)),
  act: (id: number, verb: string) => requestJSON<{ok: boolean}>(`/api/tasks/${id}/${verb}`, post()),
};

function post(body?: unknown): RequestInit {
  return {
    method: "POST",
    headers: body === undefined ? undefined : {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

export function validateFile(file: File | null): string {
  if (!file || file.size <= MAX_FILE_BYTES) return "";
  return `${file.name || "File"} exceeds the 20 MB attachment limit.`;
}

export async function encodeFile(file: File): Promise<{name: string; data: string}> {
  const error = validateFile(file);
  if (error) throw new RequestError(error, 0, "file");
  const result = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  return {name: file.name, data: result.split(",", 2)[1] ?? ""};
}
