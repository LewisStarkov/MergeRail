// @vitest-environment jsdom

import {act, cleanup, fireEvent, render, screen, waitFor} from "@testing-library/preact";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {App} from "./App";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
}

const task = {
  id: 1,
  text: "Audit the release checklist",
  status: "new" as const,
  open: true,
  source: "web",
  attempts: 0,
  message_count: 51,
};

function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {"Content-Type": "application/json"},
  });
}

function setPageRuntime(id: string) {
  const meta = document.createElement("meta");
  meta.name = "mergerail-runtime-id";
  meta.content = id;
  document.head.append(meta);
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({tasks: [], setup: {}})));
  document.querySelector('meta[name="mergerail-runtime-id"]')?.remove();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("App integration contracts", () => {
  it("explains the reviewer role and keeps technical details collapsed", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(response(
      String(input) === "/api/tasks"
        ? {tasks: [{...task, status: "review", execution: {backend: "docker"}, live: {stage: "reviewing", roles: {fixer: {status: "complete"}}}}], setup: {}}
        : {messages: [], message_count: 0}
    ))));
    render(<App />);
    await screen.findByText("Ревьюер проверяет результат", {selector: "h3"});
    expect(screen.getByText("Технические детали · Docker и сохранённый результат").closest("details")?.open).toBe(false);
    fireEvent.click(screen.getByRole("button", {name: "Как это работает"}));
    expect(screen.getByRole("dialog").textContent).toContain("Код не меняет");
    fireEvent.keyDown(screen.getByRole("dialog"), {key: "n"});
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", {name: "Close"}));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders Docker recovery metadata from the task API", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/api/tasks") return Promise.resolve(response({
        tasks: [{...task, execution: {
          backend: "docker", validated: true, phase: "reconciled",
          base_sha: "a".repeat(40), result_sha: "b".repeat(40),
          recovery: {status: "unverified-artifacts-preserved", pending_artifacts: ["checkpoint.tar"]},
        }}], setup: {},
      }));
      return Promise.resolve(response({messages: [], message_count: 0}));
    }));

    render(<App />);
    await screen.findByText("Recovery: unverified-artifacts-preserved");
    expect(screen.getByText("Isolation validated · reconciled")).toBeTruthy();
    expect(screen.getByText("b".repeat(40))).toBeTruthy();
  });

  it("reloads when replacement SSE arrives after the initial REST request fails", async () => {
    const reload = vi.fn();
    setPageRuntime("original");
    const fetchMock = vi.fn().mockRejectedValue(new Error("server restarting"));
    vi.stubGlobal("fetch", fetchMock);

    render(<App reload={reload} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await act(async () => {});
    const source = FakeEventSource.instances[0];
    const replacement = {runtime: {id: "replacement", version: "2"}, tasks: [], setup: {}};

    act(() => source?.onmessage?.(
      new MessageEvent("message", {data: JSON.stringify(replacement)}),
    ));
    act(() => source?.onmessage?.(
      new MessageEvent("message", {data: JSON.stringify(replacement)}),
    ));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("accepts a first SSE payload from the runtime embedded in the page", async () => {
    const reload = vi.fn();
    setPageRuntime("current");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("temporarily unavailable")));

    render(<App reload={reload} />);
    await act(async () => {});
    const source = FakeEventSource.instances[0];
    act(() => source?.onmessage?.(new MessageEvent("message", {data: JSON.stringify({
      runtime: {id: "current", version: "1"}, tasks: [], setup: {},
    })})));

    expect(reload).not.toHaveBeenCalled();
  });

  it("reloads once when a refreshed payload reports a different runtime", async () => {
    const reload = vi.fn();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({runtime: {id: "first", version: "1"}, tasks: [], setup: {}}))
      .mockResolvedValueOnce(response({runtime: {id: "first", version: "1"}, tasks: [], setup: {}}))
      .mockResolvedValue(response({runtime: {id: "second", version: "2"}, tasks: [], setup: {}}));
    vi.stubGlobal("fetch", fetchMock);

    render(<App reload={reload} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(reload).not.toHaveBeenCalled();

    await act(async () => { window.dispatchEvent(new Event("online")); });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(reload).not.toHaveBeenCalled();

    await act(async () => { window.dispatchEvent(new Event("online")); });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));

    await act(async () => { window.dispatchEvent(new Event("online")); });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("uses SSE runtime ids and ignores payloads without one", async () => {
    const reload = vi.fn();
    render(<App reload={reload} />);
    await act(async () => {});
    const source = FakeEventSource.instances[0];

    const send = (payload: unknown) => act(() => source?.onmessage?.(
      new MessageEvent("message", {data: JSON.stringify(payload)}),
    ));
    send({runtime: {id: "first", version: "1"}, tasks: [], setup: {}});
    send({runtime: {id: "first", version: "1"}, tasks: [], setup: {}});
    send({tasks: [], setup: {}});
    expect(reload).not.toHaveBeenCalled();

    send({runtime: {id: "second", version: "2"}, tasks: [], setup: {}});
    send({runtime: {id: "third", version: "3"}, tasks: [], setup: {}});
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("stops reconnect polling when the event stream reopens", async () => {
    vi.useFakeTimers();
    render(<App />);
    await act(async () => {});
    const fetchMock = vi.mocked(fetch);
    const source = FakeEventSource.instances[0];
    expect(source?.url).toBe("/api/events");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => source?.onerror?.());
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    act(() => source?.onopen?.());
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("loads the next message page after the last visible id", async () => {
    const messages = Array.from({length: 50}, (_, index) => ({
      id: index + 1,
      role: "user",
      text: `Entry ${index + 1}`,
    }));
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/tasks") return Promise.resolve(response({tasks: [task], setup: {}}));
      if (url.includes("after=0")) {
        return Promise.resolve(response({messages, message_count: 51}));
      }
      if (url.includes("after=50")) {
        return Promise.resolve(response({messages: [{id: 51, role: "user", text: "Entry 51"}], message_count: 51}));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    const loadMore = await screen.findByRole("button", {name: "Load more entries"});
    fireEvent.click(loadMore);
    await screen.findByText("Entry 51");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/messages?after=50&limit=50",
      undefined,
    );
  });

  it("traps dialog work in an overlay and restores focus on Escape", async () => {
    render(<App />);
    const trigger = await screen.findByRole("button", {name: /New task/});
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", {name: "New work order"});
    const textbox = screen.getByRole("textbox", {name: "Requested outcome"});
    await waitFor(() => expect(document.activeElement).toBe(textbox));
    fireEvent.keyDown(dialog, {key: "Escape"});

    expect(screen.queryByRole("dialog", {name: "New work order"})).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("persists the desktop task queue state with accurate disclosure semantics", async () => {
    localStorage.setItem("mergerail-ui-prefs", JSON.stringify({
      theme: "light", density: "compact", sidebarCollapsed: true,
    }));
    render(<App />);

    const toggle = await screen.findByRole("button", {name: "Expand task queue"});
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.getAttribute("aria-controls")).toBe("task-queue");
    expect(document.querySelector(".operations-grid")?.classList.contains("queue-collapsed")).toBe(true);

    fireEvent.click(toggle);
    const expanded = screen.getByRole("button", {name: "Collapse task queue"});
    expect(expanded.getAttribute("aria-expanded")).toBe("true");
    expect(document.querySelector(".operations-grid")?.classList.contains("queue-collapsed")).toBe(false);
    await waitFor(() => expect(JSON.parse(localStorage.getItem("mergerail-ui-prefs") || "{}").sidebarCollapsed).toBe(false));
  });

  it("uses custom setup listboxes without rendering native selects", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      tasks: [],
      setup: {agents: {codex: {available: true}, offline: {available: false, reason: "not installed"}}},
    })));
    render(<App />);
    await act(async () => {});
    fireEvent.click(await screen.findByRole("button", {name: "Project setup"}));

    const dialog = screen.getByRole("dialog", {name: "Project setup"});
    expect(dialog.querySelector("select")).toBeNull();
    const agent = screen.getByRole("button", {name: "AI agent Auto (first available)"});
    fireEvent.click(agent);
    const listbox = screen.getByRole("listbox", {name: "AI agent"});
    expect(listbox).toBeTruthy();
    const unavailableAgent = await screen.findByRole("option", {name: "offline — not installed"});
    expect(unavailableAgent.getAttribute("aria-disabled")).toBe("true");
    fireEvent.keyDown(listbox, {key: "Escape"});
    expect(screen.getByRole("dialog", {name: "Project setup"})).toBeTruthy();
    expect(document.activeElement).toBe(agent);
  });

  it("hydrates an untouched setup form when the initial payload arrives", async () => {
    let resolveFetch!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>((resolve) => {resolveFetch = resolve;})));
    render(<App />);
    fireEvent.click(screen.getByRole("button", {name: "Project setup"}));
    expect(screen.getByRole("button", {name: "AI agent Auto (first available)"})).toBeTruthy();

    resolveFetch(response({
      tasks: [],
      setup: {
        initialized: true,
        agents: {codex: {available: true}},
        values: {agent: "codex", environment: "production", summary: "Protect the release", external_actions: "ask"},
      },
    }));

    expect(await screen.findByRole("button", {name: "AI agent codex"})).toBeTruthy();
    expect(screen.getByRole("button", {name: "Environment Production"})).toBeTruthy();
    expect((screen.getByRole("textbox", {name: "Current objective"}) as HTMLTextAreaElement).value).toBe("Protect the release");
    expect(screen.getByRole("button", {name: "Actions outside repository Ask first"})).toBeTruthy();
  });

  it("does not render a visible connection indicator", async () => {
    render(<App />);
    await act(async () => {});
    expect(screen.queryByText("live", {exact: true})).toBeNull();
    expect(document.querySelector(".connection")).toBeNull();
  });
});
