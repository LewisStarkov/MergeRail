import {describe, expect, it} from "vitest";
import {filterTasks, initialState, moveSelection, parsePreferences, reducer} from "./state";
import type {Task} from "./types";

const tasks = [
  {id: 1, text: "Ship billing report", status: "running", open: true, branch: "mr/1-billing"},
  {id: 2, text: "Repair login", status: "failed", open: false, source: "web"},
  {id: 3, text: "Update handbook", status: "done", open: false},
] satisfies Task[];

describe("task state", () => {
  it("keeps a valid selection and falls back to the first open task", () => {
    const loaded = reducer(initialState, {type: "payload", payload: {tasks}});
    expect(loaded.selectedId).toBe(1);
    expect(reducer({...loaded, selectedId: 2}, {type: "payload", payload: {tasks}}).selectedId).toBe(2);
    expect(reducer({...loaded, selectedId: 99}, {type: "payload", payload: {tasks}}).selectedId).toBe(1);
  });

  it("filters by operational group and searchable fields", () => {
    expect(filterTasks(tasks, "", "attention").map((task) => task.id)).toEqual([2]);
    expect(filterTasks(tasks, "BILLING", "all").map((task) => task.id)).toEqual([1]);
    expect(filterTasks(tasks, "web", "all").map((task) => task.id)).toEqual([2]);
  });

  it("moves and wraps queue selection", () => {
    expect(moveSelection(tasks, 1, 1)).toBe(2);
    expect(moveSelection(tasks, 1, -1)).toBe(3);
    expect(moveSelection([], null, 1)).toBeNull();
  });
});

describe("preferences", () => {
  it("uses compact defaults and validates persisted values", () => {
    expect(parsePreferences(null)).toEqual({theme: "light", density: "compact", sidebarCollapsed: false});
    expect(parsePreferences('{"theme":"dark","density":"comfortable","sidebarCollapsed":true}'))
      .toEqual({theme: "dark", density: "comfortable", sidebarCollapsed: true});
    expect(parsePreferences('{"theme":"sepia","density":"tiny","sidebarCollapsed":"yes"}'))
      .toEqual({theme: "light", density: "compact", sidebarCollapsed: false});
    expect(parsePreferences("broken")).toEqual({theme: "light", density: "compact", sidebarCollapsed: false});
  });
});
