export type Status =
  | "new" | "running" | "review" | "approved" | "delivering" | "cancelling"
  | "done" | "failed" | "blocked" | "cancelled" | "closed";

export interface AgentState {
  status?: string;
  text?: string;
  tools?: string[];
  history?: string[];
}

export interface LiveTrace {
  stage?: string;
  roles?: Record<string, AgentState>;
}

export interface Delivery {
  status?: string;
  stage?: string;
  outcome?: string;
  summary?: string;
  review?: string;
  url?: string;
  errors?: Array<{stage?: string; code?: string; message?: string; occurred_at?: string}>;
}

export interface Task {
  id: number;
  text: string;
  status: Status;
  open: boolean;
  source?: string;
  author?: string;
  branch?: string | null;
  attempts?: number;
  created_at?: string;
  updated_at?: string;
  file?: string | null;
  note?: string;
  url?: string;
  cost_usd?: number;
  message_count?: number;
  pending_message_count?: number;
  message_revision?: number;
  last_message_at?: string;
  delivery?: Delivery;
  deployment?: {
    status: string;
    sha?: string;
    artifact_id?: string;
    url?: string;
    error?: string;
    previous_sha?: string;
  };
  execution?: {
    backend?: string;
    validated?: boolean;
    phase?: string;
    base_sha?: string;
    result_sha?: string;
    image?: string;
    recovery?: {status?: string; pending_artifacts?: string[]};
    memory_mib?: number;
    workspace_limit_mib?: number;
  };
  live?: LiveTrace | null;
}

export interface Message {
  id: number;
  role?: string;
  author?: string;
  text?: string;
  mode?: "comment" | "instruction";
  status?: string;
  file?: string;
  created_at?: string;
}

export interface SetupProgress {
  status?: string;
  stage?: string;
  message?: string;
  history?: Array<{stage: string; message: string}>;
}

export interface Setup {
  available?: boolean;
  initialized?: boolean;
  busy?: boolean;
  agents?: Record<string, {available?: boolean; reason?: string}>;
  values?: {
    agent?: string;
    environment?: string;
    summary?: string;
    external_actions?: string;
  };
  progress?: SetupProgress;
}

export interface TasksPayload {
  runtime?: {id?: string; version?: string};
  tasks: Task[];
  total?: number;
  setup?: Setup;
}

export type Filter = "all" | "active" | "attention" | "done";
export type Theme = "light" | "dark";
export type Density = "compact" | "comfortable";
export interface Preferences { theme: Theme; density: Density; sidebarCollapsed: boolean }

export interface AppState {
  tasks: Task[];
  setup: Setup;
  selectedId: number | null;
  filter: Filter;
  query: string;
}
