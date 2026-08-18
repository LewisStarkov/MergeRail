import type {AppState, Density, Filter, Preferences, Task, TasksPayload, Theme} from "./types";

export const STATUS_GROUPS: Record<Exclude<Filter, "all">, ReadonlySet<string>> = {
  active: new Set(["new", "running", "review", "approved", "delivering", "cancelling"]),
  attention: new Set(["failed", "blocked"]),
  done: new Set(["done", "cancelled", "closed"]),
};

export const initialState: AppState = {
  tasks: [], setup: {}, selectedId: null, filter: "all", query: "",
};

export type Action =
  | {type: "payload"; payload: TasksPayload}
  | {type: "select"; id: number | null}
  | {type: "filter"; filter: Filter}
  | {type: "query"; query: string};

export function reducer(state: AppState, action: Action): AppState {
  if (action.type === "payload") {
    const tasks = Array.isArray(action.payload.tasks) ? action.payload.tasks : [];
    const selectedId = tasks.some((task) => task.id === state.selectedId)
      ? state.selectedId : (tasks.find((task) => task.open) ?? tasks[0])?.id ?? null;
    return {...state, tasks, setup: action.payload.setup ?? {}, selectedId};
  }
  if (action.type === "select") return {...state, selectedId: action.id};
  if (action.type === "filter") return {...state, filter: action.filter};
  return {...state, query: action.query};
}

export function filterTasks(tasks: Task[], query: string, filter: Filter): Task[] {
  const needle = query.trim().toLocaleLowerCase();
  return tasks.filter((task) => {
    if (filter !== "all" && !STATUS_GROUPS[filter].has(task.status)) return false;
    if (!needle) return true;
    return [task.id, task.text, task.status, task.source, task.branch]
      .some((value) => String(value ?? "").toLocaleLowerCase().includes(needle));
  });
}

export function moveSelection(tasks: Task[], selectedId: number | null, direction: 1 | -1): number | null {
  if (!tasks.length) return null;
  const index = tasks.findIndex((task) => task.id === selectedId);
  const start = index < 0 ? (direction > 0 ? -1 : 0) : index;
  return tasks[(start + direction + tasks.length) % tasks.length]?.id ?? null;
}

export const DEFAULT_PREFS: Preferences = {theme: "light", density: "compact", sidebarCollapsed: false};

export function parsePreferences(raw: string | null): Preferences {
  if (!raw) return DEFAULT_PREFS;
  try {
    const value = JSON.parse(raw) as Partial<Preferences>;
    const theme: Theme = value.theme === "dark" ? "dark" : "light";
    const density: Density = value.density === "comfortable" ? "comfortable" : "compact";
    return {theme, density, sidebarCollapsed: value.sidebarCollapsed === true};
  } catch {
    return DEFAULT_PREFS;
  }
}

export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)
    || (target instanceof HTMLElement && target.isContentEditable);
}
