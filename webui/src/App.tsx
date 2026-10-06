import {type ComponentChildren, type JSX} from "preact";
import {useCallback, useEffect, useMemo, useReducer, useRef, useState} from "preact/hooks";
import {api, encodeFile, RequestError, validateFile} from "./api";
import {Icon} from "./icons";
import {shortcutFor} from "./keyboard";
import {LedgerSelect, type LedgerOption} from "./LedgerSelect";
import {filterTasks, initialState, isTypingTarget, moveSelection, parsePreferences, reducer} from "./state";
import type {Filter, Message, Preferences, Setup, Task, TasksPayload} from "./types";

const PREF_KEY = "mergerail-ui-prefs";
const STATUS_LABEL: Record<string, string> = {
  new: "Queued", running: "Running", review: "Review", approved: "Approved",
  delivering: "Delivering", cancelling: "Cancelling", done: "Done", failed: "Failed",
  blocked: "Blocked", cancelled: "Cancelled", closed: "Closed",
};

type DialogName = "new" | "setup" | "palette" | "help" | null;
type Toast = {id: number; message: string; kind: "info" | "danger" | "success"};

function reloadPage() {
  window.location.reload();
}

function pageRuntimeId() {
  return document.querySelector<HTMLMetaElement>('meta[name="mergerail-runtime-id"]')?.content || null;
}

export function App({reload = reloadPage}: {reload?: () => void} = {}) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [dialog, setDialog] = useState<DialogName>(null);
  const [mobileDetail, setMobileDetail] = useState(false);
  const [prefs, setPrefs] = useState<Preferences>(() => parsePreferences(safeGet(PREF_KEY)));
  const [toasts, setToasts] = useState<Toast[]>([]);
  const searchRef = useRef<HTMLInputElement>(null);
  const threadInputRef = useRef<HTMLTextAreaElement>(null);
  const runtimeIdRef = useRef<string | null>(pageRuntimeId());
  const reloadRequestedRef = useRef(false);
  const visible = useMemo(() => filterTasks(state.tasks, state.query, state.filter), [state.tasks, state.query, state.filter]);
  const selected = state.tasks.find((task) => task.id === state.selectedId);

  const notify = useCallback((message: string, kind: Toast["kind"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((items) => [...items.slice(-3), {id, message, kind}]);
    window.setTimeout(() => setToasts((items) => items.filter((item) => item.id !== id)), 4800);
  }, []);

  const receivePayload = useCallback((payload: TasksPayload) => {
    if (reloadRequestedRef.current) return;
    const runtimeId = payload.runtime?.id;
    if (runtimeId) {
      if (runtimeIdRef.current === null) runtimeIdRef.current = runtimeId;
      else if (runtimeIdRef.current !== runtimeId) {
        reloadRequestedRef.current = true;
        reload();
        return;
      }
    }
    dispatch({type: "payload", payload});
  }, [reload]);

  const refresh = useCallback(async (quiet = true) => {
    try {
      receivePayload(await api.tasks());
    } catch (error) {
      if ((error as RequestError).kind === "auth") return;
      if (!quiet) notify(errorMessage(error), "danger");
    }
  }, [notify, receivePayload]);

  useEffect(() => {
    void refresh();
    let poll = 0;
    let polling = false;
    let source: EventSource | undefined;
    const schedule = () => {
      if (!polling) return;
      window.clearTimeout(poll);
      poll = window.setTimeout(async () => {
        if (!polling) return;
        await refresh();
        schedule();
      }, 15_000);
    };
    const startPolling = () => {
      if (polling) return;
      polling = true;
      schedule();
    };
    const stopPolling = () => {
      polling = false;
      window.clearTimeout(poll);
    };
    try {
      source = new EventSource("/api/events");
      source.onopen = stopPolling;
      source.onmessage = (event) => {
        try { receivePayload(JSON.parse(event.data) as TasksPayload); }
        catch { notify("Live update could not be read", "danger"); }
      };
      source.onerror = () => {
        void refresh();
        startPolling();
      };
    } catch {
      startPolling();
    }
    const online = () => void refresh();
    window.addEventListener("online", online);
    return () => {
      source?.close();
      stopPolling();
      window.removeEventListener("online", online);
    };
  }, [notify, receivePayload, refresh]);

  useEffect(() => {
    document.documentElement.dataset.theme = prefs.theme;
    document.documentElement.dataset.density = prefs.density;
    try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch {}
  }, [prefs]);

  const toggleTheme = useCallback(() => setPrefs((value) => ({...value, theme: value.theme === "dark" ? "light" : "dark"})), []);
  const toggleSidebar = useCallback(() => setPrefs((value) => ({...value, sidebarCollapsed: !value.sidebarCollapsed})), []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || dialog || isTypingTarget(event.target)) return;
      const shortcut = shortcutFor(event);
      if (!shortcut) return;
      event.preventDefault();
      if (shortcut === "search") searchRef.current?.focus();
      if (shortcut === "new") setDialog("new");
      if (shortcut === "palette") setDialog("palette");
      if (shortcut === "help") setDialog("help");
      if (shortcut === "escape") setMobileDetail(false);
      if (shortcut === "queue") setMobileDetail(false);
      if (shortcut === "task" && state.selectedId !== null) setMobileDetail(true);
      if (shortcut === "comment") threadInputRef.current?.focus();
      if (shortcut === "next" || shortcut === "previous") {
        dispatch({type: "select", id: moveSelection(visible, state.selectedId, shortcut === "next" ? 1 : -1)});
      }
      if (shortcut === "open" && state.selectedId !== null) setMobileDetail(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dialog, state.selectedId, visible]);

  const selectTask = (id: number) => {
    dispatch({type: "select", id});
    setMobileDetail(true);
  };

  const execute = (name: Exclude<DialogName, null> | "theme" | "density") => {
    setDialog(null);
    if (name === "theme") toggleTheme();
    else if (name === "density") setPrefs((value) => ({...value, density: value.density === "compact" ? "comfortable" : "compact"}));
    else setDialog(name);
  };

  return <>
    <a class="skip-link" href="#workspace">Skip to task workspace</a>
    <div class="app-shell">
      <TopBar prefs={prefs} onTheme={toggleTheme} onSidebar={toggleSidebar} onPalette={() => setDialog("palette")} onSetup={() => setDialog("setup")} />
      <div class={`operations-grid ${mobileDetail ? "show-detail" : "show-queue"} ${prefs.sidebarCollapsed ? "queue-collapsed" : ""}`}>
        <Queue id="task-queue" tasks={visible} allTasks={state.tasks} selectedId={state.selectedId} query={state.query} filter={state.filter}
          searchRef={searchRef} onQuery={(query) => dispatch({type: "query", query})}
          onFilter={(filter) => dispatch({type: "filter", filter})} onSelect={selectTask} onNew={() => setDialog("new")} />
        <Workspace task={selected} onBack={() => setMobileDetail(false)} refresh={refresh} notify={notify} inputRef={threadInputRef} />
      </div>
      <MobileBar selected={Boolean(selected)} detail={mobileDetail} onQueue={() => setMobileDetail(false)} onDetail={() => setMobileDetail(true)} onNew={() => setDialog("new")} />
    </div>
    <ToastRegion items={toasts} dismiss={(id) => setToasts((items) => items.filter((item) => item.id !== id))} />
    {dialog === "new" && <NewTaskDialog close={() => setDialog(null)} complete={async (message) => {setDialog(null); notify(message, "success"); await refresh(false);}} notify={notify} />}
    {dialog === "setup" && <SetupDialog setup={state.setup} close={() => setDialog(null)} complete={async () => {
      setDialog(null);
      notify("Project setup saved", "success");
      await refresh(false);
    }} />}
    {dialog === "palette" && <CommandPalette close={() => setDialog(null)} execute={execute} />}
    {dialog === "help" && <HelpDialog close={() => setDialog(null)} />}
  </>;
}

function TopBar({prefs, onTheme, onSidebar, onPalette, onSetup}: {
  prefs: Preferences; onTheme: () => void; onSidebar: () => void; onPalette: () => void; onSetup: () => void;
}) {
  return <header class="topbar">
    <div class="topbar-left">
      <div class="wordmark" aria-label="MergeRail operations">
        <span class="wordmark-merge">Merge</span><span class="wordmark-rail">Rail</span>
        <span class="wordmark-context">operations</span>
      </div>
      <button class="icon-button sidebar-toggle" onClick={onSidebar} aria-controls="task-queue"
        aria-expanded={!prefs.sidebarCollapsed} aria-label={prefs.sidebarCollapsed ? "Expand task queue" : "Collapse task queue"}
        title={prefs.sidebarCollapsed ? "Expand task queue" : "Collapse task queue"}><Icon name="sidebar" /></button>
    </div>
    <div class="top-actions">
      <button class="quiet-button command-trigger" onClick={onPalette}><Icon name="command" /> Commands <kbd>⌘K</kbd></button>
      <button class="icon-button" onClick={onTheme} aria-label={`Use ${prefs.theme === "dark" ? "light" : "dark"} theme`} title="Toggle theme"><Icon name={prefs.theme === "dark" ? "sun" : "moon"} /></button>
      <button class="icon-button" onClick={onSetup} aria-label="Project setup" title="Project setup"><Icon name="adjust" /></button>
    </div>
  </header>;
}

function Queue({id, tasks, allTasks, selectedId, query, filter, searchRef, onQuery, onFilter, onSelect, onNew}: {
  id: string; tasks: Task[]; allTasks: Task[]; selectedId: number | null; query: string; filter: Filter;
  searchRef: {current: HTMLInputElement | null}; onQuery: (value: string) => void;
  onFilter: (value: Filter) => void; onSelect: (id: number) => void; onNew: () => void;
}) {
  const open = allTasks.filter((task) => task.open).length;
  return <aside id={id} class="queue" aria-label="Task queue">
    <div class="pane-heading">
      <div><span class="eyebrow">Queue ledger</span><h1>Work orders</h1></div>
      <button class="primary-button" onClick={onNew}><Icon name="add" /> New task <kbd>N</kbd></button>
    </div>
    <div class="queue-tools">
      <label class="search-field"><Icon name="search" /><span class="sr-only">Search tasks</span>
        <input ref={searchRef} type="search" value={query} onInput={(event) => onQuery(event.currentTarget.value)} placeholder="Search id, branch, or text" />
        <kbd>/</kbd>
      </label>
      <div class="filters" aria-label="Filter by task status">
        {(["all", "active", "attention", "done"] as Filter[]).map((value) =>
          <button key={value} aria-pressed={filter === value} onClick={() => onFilter(value)}>{value}</button>)}
      </div>
    </div>
    <div class="ledger-head" aria-hidden="true"><span>Order</span><span>State</span></div>
    <div class="task-list" role="listbox" aria-label="Tasks">
      {tasks.map((task) => <TaskRow key={task.id} task={task} selected={task.id === selectedId} onSelect={onSelect} />)}
      {!tasks.length && <div class="queue-empty"><Icon name={allTasks.length ? "search" : "file"} size={20} /><strong>{allTasks.length ? "No matching work orders" : "No work orders queued"}</strong><span>{allTasks.length ? "Change the search or status filter." : "Create a task to begin the ledger."}</span></div>}
    </div>
    <footer class="queue-footer"><span>{tasks.length} shown</span><span>{open} open</span><span>{allTasks.length} total</span></footer>
  </aside>;
}

function TaskRow({task, selected, onSelect}: {task: Task; selected: boolean; onSelect: (id: number) => void}) {
  return <button type="button" role="option" aria-selected={selected} class="task-row" onClick={() => onSelect(task.id)}>
    <span class="task-number">MR-{String(task.id).padStart(4, "0")}</span>
    <span class="task-copy"><strong>{firstLine(task.text) || "Attachment-only task"}</strong><small>{task.branch || task.source || "web"}{task.message_count ? ` · ${task.message_count} notes` : ""}</small></span>
    <Status value={task.status} />
  </button>;
}

function Workspace({task, onBack, refresh, notify, inputRef}: {
  task?: Task; onBack: () => void; refresh: (quiet?: boolean) => Promise<void>; notify: (message: string, kind?: Toast["kind"]) => void;
  inputRef: {current: HTMLTextAreaElement | null};
}) {
  return <main id="workspace" class="workspace" tabIndex={-1}>
    {!task ? <EmptyWorkspace /> : <TaskWorkspace key={task.id} task={task} onBack={onBack} refresh={refresh} notify={notify} inputRef={inputRef} />}
  </main>;
}

function EmptyWorkspace() {
  return <div class="workspace-empty"><span class="index-number">00</span><div><span class="eyebrow">No selection</span><h2>Select a work order</h2><p>The activity ledger, delivery state, and controls will appear here.</p></div></div>;
}

function TaskWorkspace({task, onBack, refresh, notify, inputRef}: {
  task: Task; onBack: () => void; refresh: (quiet?: boolean) => Promise<void>;
  notify: (message: string, kind?: Toast["kind"]) => void; inputRef: {current: HTMLTextAreaElement | null};
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [messageCount, setMessageCount] = useState(task.message_count ?? 0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [acting, setActing] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await api.messages(task.id);
      setMessages(payload.messages ?? []);
      setMessageCount(payload.message_count ?? payload.messages.length);
      setError("");
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setLoading(false); }
  }, [task.id]);

  const loadMore = async () => {
    if (loading || !messages.length) return;
    setLoading(true);
    try {
      const payload = await api.messages(task.id, messages[messages.length - 1]?.id ?? 0);
      setMessages((current) => [...current, ...(payload.messages ?? [])]);
      setMessageCount(payload.message_count ?? messageCount);
      setError("");
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setLoading(false); }
  };

  useEffect(() => { void load(); }, [load, task.message_revision, task.last_message_at]);

  const act = async (verb: string, label: string) => {
    if (verb === "delete" && !window.confirm(`Delete work order MR-${String(task.id).padStart(4, "0")}?`)) return;
    setActing(verb);
    try { await api.act(task.id, verb); notify(`${label} completed`, "success"); await refresh(false); }
    catch (caught) { notify(errorMessage(caught), "danger"); }
    finally { setActing(""); }
  };

  return <>
    <header class="workspace-header">
      <button class="mobile-back" onClick={onBack}><Icon name="back" /> Queue</button>
      <div class="record-title"><span class="eyebrow">Work order · MR-{String(task.id).padStart(4, "0")}</span><h2>{firstLine(task.text) || "Attachment-only task"}</h2></div>
      <Status value={task.status} />
    </header>
    <div class="workspace-meta">
      <Fact label="Source" value={task.source || "web"} />
      <Fact label="Branch" value={task.branch || "Not created"} />
      <Fact label="Attempt" value={String(task.attempts ?? 0).padStart(2, "0")} />
      <Fact label="Cost" value={task.cost_usd ? `$${task.cost_usd.toFixed(2)}` : "—"} />
    </div>
    <div class="workspace-scroll">
      {task.execution?.backend === "docker" && <section class="record-section"><SectionHeading index="00" title="Docker execution" />
        <div class="record-content">
          <p>{task.execution.validated ? "Isolation validated" : "Isolation pending"}{task.execution.phase && ` · ${task.execution.phase}`}</p>
          {task.execution.memory_mib && <p>RAM: {task.execution.memory_mib} MiB · Workspace: {task.execution.workspace_limit_mib} MiB</p>}
          {task.execution.base_sha && <p>Base commit: <code>{task.execution.base_sha}</code></p>}
          {task.execution.result_sha && <p>Saved result: <code>{task.execution.result_sha}</code></p>}
          {task.execution.recovery?.status && <p>Recovery: {task.execution.recovery.status}</p>}
        </div>
      </section>}
      <section class="record-section description"><SectionHeading index="01" title="Request" />
        <div class="record-content"><p class="request-text">{task.text || "Attachment-only task"}</p>{task.file && <Attachment file={task.file} />}</div>
      </section>
      {task.delivery && Boolean(task.delivery.status !== "none" || task.delivery.summary || task.delivery.errors?.length) && <section class="record-section"><SectionHeading index="02" title="Delivery" />
        <div class="record-content"><div class="delivery-line"><Status value={task.delivery.status || "pending"} />
          {(task.delivery.stage || task.delivery.outcome || task.delivery.summary) && <p>{[task.delivery.stage, task.delivery.outcome, task.delivery.summary].filter(Boolean).join(" · ")}</p>}
          {task.delivery.errors?.map((item, index) => <pre key={`${item.code}-${index}`}>{item.stage ? `${item.stage}: ` : ""}{item.message || item.code}</pre>)}
        </div></div>
      </section>}
      {task.deployment && <section class="record-section"><SectionHeading index="D" title="DevBot deployment" />
        <div class="record-content"><p>{task.deployment.status}</p>
          <code>{task.deployment.sha}</code>
          {task.deployment.error && <pre>{task.deployment.error}</pre>}
          {task.deployment.url && /^https:\/\//.test(task.deployment.url) && <p><a href={task.deployment.url} target="_blank" rel="noopener noreferrer">Open DevBot</a></p>}
        </div>
      </section>}
      <section class="record-section activity-section"><SectionHeading index="03" title="Activity ledger" meta={`${messageCount} entries`} />
        <div class="record-content">
          <ActivityEntry message={{id: 0, role: "user", author: task.author || task.source || "web", text: task.text, file: task.file || undefined, created_at: task.created_at, status: "original"}} />
          {messages.map((message) => <ActivityEntry key={message.id} message={message} />)}
          {task.live && <LiveActivity task={task} />}
          {loading && <div class="ledger-note">Loading ledger…</div>}
          {!loading && messages.length < messageCount && <button class="ledger-load" onClick={() => void loadMore()}>Load more entries</button>}
          {error && <div class="inline-error" role="alert">{error} <button onClick={() => void load()}>Retry</button></div>}
          <MessageComposer taskId={task.id} inputRef={inputRef} onSent={async () => {await load(); await refresh();}} notify={notify} />
        </div>
      </section>
      {(task.note || task.url) && <section class="record-section"><SectionHeading index="04" title="Outcome" /><div class="record-content outcome"><Icon name="check" /><span>{task.note}{task.url && /^https?:\/\//.test(task.url) && <><br /><a href={task.url} target="_blank" rel="noopener noreferrer">Open delivery link</a></>}</span></div></section>}
    </div>
    <footer class="action-strip">
      <span>Available actions</span>
      <div>{actionsFor(task).map(([verb, label, icon]) => <button key={verb} disabled={Boolean(acting)} class={verb === "delete" ? "danger-button" : "quiet-button"} onClick={() => void act(verb, label)}><Icon name={acting === verb ? "retry" : icon} />{label}</button>)}</div>
    </footer>
  </>;
}

function SectionHeading({index, title, meta}: {index: string; title: string; meta?: string}) {
  return <header class="section-heading"><span>{index}</span><h3>{title}</h3>{meta && <small>{meta}</small>}</header>;
}

function Fact({label, value}: {label: string; value: string}) {
  return <div><span>{label}</span><strong title={value}>{value}</strong></div>;
}

function Status({value}: {value: string}) {
  return <span class={`status-chip status-${value}`}><span aria-hidden="true" />{STATUS_LABEL[value] ?? value}</span>;
}

function ActivityEntry({message}: {message: Message}) {
  const name = message.author || (message.role === "assistant" ? "agent" : message.role) || "system";
  return <article class={`activity-entry role-${message.role || "system"}`}>
    <div class="activity-rule"><span /></div>
    <div class="activity-body">
      <header><strong>{name}</strong>{message.mode && <span>{message.mode}</span>}{message.status && <span>{message.status}</span>}<time>{formatTime(message.created_at)}</time></header>
      <p>{message.text}</p>{message.file && <Attachment file={message.file} />}
    </div>
  </article>;
}

function LiveActivity({task}: {task: Task}) {
  const roles = Object.entries(task.live?.roles ?? {});
  if (!roles.length) return null;
  return <details class="live-activity" open><summary><span class="live-pulse" />Agent activity · {task.live?.stage || "working"}</summary>
    {roles.map(([role, value]) => <div class="live-role" key={role}><header><strong>{role}</strong><span>{value.status || "active"}</span></header>{value.text && <pre>{value.text}</pre>}{value.tools?.slice(-6).map((tool) => <code key={tool}>{tool}</code>)}</div>)}
  </details>;
}

function Attachment({file}: {file: string}) {
  return <span class="attachment"><Icon name="attach" />{file.split(/[\\/]/).pop()}</span>;
}

function MessageComposer({taskId, inputRef, onSent, notify}: {
  taskId: number; inputRef: {current: HTMLTextAreaElement | null}; onSent: () => Promise<void>; notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const picker = useRef<HTMLInputElement>(null);

  const submit = async (mode: "comment" | "instruction") => {
    if (!text.trim() || sending) return;
    setSending(true); setError("");
    try {
      const body: Record<string, unknown> = {text: text.trim(), mode, idempotency_key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`};
      if (file) body.file = await encodeFile(file);
      const response = await api.sendMessage(taskId, body);
      if (response.warning) notify(response.warning, "danger");
      setText(""); setFile(null); if (picker.current) picker.current.value = "";
      notify(mode === "comment" ? "Comment recorded" : "Instruction sent", "success");
      await onSent();
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setSending(false); }
  };

  const chooseFile: JSX.GenericEventHandler<HTMLInputElement> = (event) => {
    const next = event.currentTarget.files?.[0] ?? null;
    const problem = validateFile(next);
    setError(problem); setFile(problem ? null : next);
    if (problem) event.currentTarget.value = "";
  };

  return <form class="message-composer" onSubmit={(event) => {event.preventDefault(); void submit("instruction");}}>
    <label htmlFor={`message-${taskId}`}>Add an instruction or record a comment</label>
    <textarea id={`message-${taskId}`} ref={inputRef} value={text} onInput={(event) => setText(event.currentTarget.value)} placeholder="Add operational context…" disabled={sending} onKeyDown={(event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {event.preventDefault(); void submit("instruction");}
    }} />
    {error && <div class="inline-error" role="alert">{error}</div>}
    <div class="composer-actions"><input ref={picker} type="file" hidden onChange={chooseFile} /><button type="button" class="quiet-button" onClick={() => picker.current?.click()}><Icon name="attach" />{file?.name || "Attach"}</button>
      <span />
      <button type="button" class="quiet-button" disabled={sending || !text.trim()} onClick={() => void submit("comment")}><Icon name="comment" />Comment</button>
      <button type="submit" class="primary-button" disabled={sending || !text.trim()}>{sending ? "Sending…" : "Send instruction"}<Icon name="arrow" /></button>
    </div>
  </form>;
}

function NewTaskDialog({close, complete, notify}: {close: () => void; complete: (message: string) => Promise<void>; notify: (message: string, kind?: Toast["kind"]) => void}) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const submit = async () => {
    if ((!text.trim() && !file) || sending) return;
    setSending(true); setError("");
    try {
      const body: Record<string, unknown> = {text: text.trim()};
      if (file) body.file = await encodeFile(file);
      const response = await api.createTask(body);
      if (response.warning) notify(response.warning, "danger");
      await complete(`Work order MR-${String(response.id).padStart(4, "0")} queued`);
    } catch (caught) { setError(errorMessage(caught)); setSending(false); }
  };
  return <Dialog title="New work order" close={close} className="form-dialog">
    <form onSubmit={(event) => {event.preventDefault(); void submit();}}>
      <label>Requested outcome<textarea autoFocus value={text} onInput={(event) => setText(event.currentTarget.value)} placeholder="Describe the result, constraints, and acceptance criteria…" /></label>
      <input ref={picker} type="file" hidden onChange={(event) => {const next = event.currentTarget.files?.[0] ?? null; const issue = validateFile(next); setError(issue); setFile(issue ? null : next);}} />
      <button type="button" class="file-drop" onClick={() => picker.current?.click()}><Icon name="attach" size={20} /><span><strong>{file?.name || "Attach context"}</strong><small>Documents or images · 20 MB maximum</small></span></button>
      {error && <div class="inline-error" role="alert">{error}</div>}
      <div class="dialog-actions"><button type="button" class="quiet-button" onClick={close}>Cancel</button><button type="submit" class="primary-button" disabled={sending || (!text.trim() && !file)}>{sending ? "Queueing…" : "Queue work order"}<Icon name="arrow" /></button></div>
    </form>
  </Dialog>;
}

function SetupDialog({setup, close, complete}: {setup: Setup; close: () => void; complete: () => Promise<void>}) {
  const values = setup.values ?? {};
  const [agent, setAgent] = useState(values.agent || "auto");
  const [environment, setEnvironment] = useState(values.environment || "local");
  const [summary, setSummary] = useState(values.summary || "");
  const [external, setExternal] = useState(values.external_actions || "forbid");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (dirty) return;
    setAgent(values.agent || "auto");
    setEnvironment(values.environment || "local");
    setSummary(values.summary || "");
    setExternal(values.external_actions || "forbid");
  }, [dirty, values.agent, values.environment, values.summary, values.external_actions]);
  const agentOptions: LedgerOption[] = [
    {value: "auto", label: "Auto (first available)"},
    ...Object.entries(setup.agents ?? {}).map(([name, info]) => ({
      value: name,
      label: `${name}${!info.available && info.reason ? ` — ${info.reason}` : ""}`,
      disabled: !info.available,
    })),
  ];
  const environmentOptions: LedgerOption[] = [
    {value: "local", label: "Local"}, {value: "staging", label: "Staging"},
    {value: "production", label: "Production"},
  ];
  const externalOptions: LedgerOption[] = [
    {value: "forbid", label: "Never"}, {value: "ask", label: "Ask first"},
  ];
  const submit = async () => {
    setSending(true); setError("");
    try { await api.setup({agent, environment, summary, external_actions: external}); await complete(); }
    catch (caught) { setError(errorMessage(caught)); }
    finally { setSending(false); }
  };
  return <Dialog title="Project setup" close={close} className="setup-dialog">
    <div class="setup-state"><span class={`setup-indicator ${setup.initialized ? "ready" : "required"}`} />
      <div><strong>{setup.initialized ? "Workspace configured" : "Configuration required"}</strong><small>{setup.progress?.message || "Applies to all newly queued work."}</small></div>
    </div>
    <form onSubmit={(event) => {event.preventDefault(); void submit();}}>
      <div class="field-grid"><LedgerSelect label="AI agent" value={agent} options={agentOptions} onChange={(value) => {setDirty(true); setAgent(value);}} />
        <LedgerSelect label="Environment" value={environment} options={environmentOptions} onChange={(value) => {setDirty(true); setEnvironment(value);}} /></div>
      <label>Current objective<textarea value={summary} onInput={(event) => {setDirty(true); setSummary(event.currentTarget.value);}} placeholder="Project context and current objective" /></label>
      <LedgerSelect label="Actions outside repository" value={external} options={externalOptions} onChange={(value) => {setDirty(true); setExternal(value);}} />
      {setup.progress?.history?.length ? <details class="setup-history"><summary>Setup activity ({setup.progress.history.length})</summary><ol>{setup.progress.history.map((item, index) => <li key={`${item.stage}-${index}`}><strong>{item.stage}</strong>{item.message}</li>)}</ol></details> : null}
      {error && <div class="inline-error" role="alert">{error}</div>}
      <div class="dialog-actions"><button type="button" class="quiet-button" onClick={close}>Close</button><button type="submit" class="primary-button" disabled={sending || setup.busy || setup.available === false}>{sending ? "Saving…" : "Save setup"}<Icon name="check" /></button></div>
    </form>
  </Dialog>;
}

function CommandPalette({close, execute}: {close: () => void; execute: (name: "new" | "setup" | "help" | "theme" | "density") => void}) {
  const [query, setQuery] = useState("");
  const commands = [
    ["new", "New work order", "N", "add"], ["setup", "Project setup", "", "adjust"],
    ["theme", "Toggle color theme", "", "moon"], ["density", "Toggle row density", "", "archive"],
    ["help", "Keyboard shortcuts", "?", "help"],
  ] as const;
  const visible = commands.filter((item) => item[1].toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <Dialog title="Command palette" close={close} className="palette-dialog">
    <label class="palette-search"><Icon name="search" /><span class="sr-only">Find a command</span><input autoFocus value={query} onInput={(event) => setQuery(event.currentTarget.value)} placeholder="Type a command…" /></label>
    <div class="command-list">{visible.map(([name, label, key, icon]) => <button key={name} onClick={() => execute(name)}><Icon name={icon} /><span>{label}</span>{key && <kbd>{key}</kbd>}</button>)}</div>
  </Dialog>;
}

function HelpDialog({close}: {close: () => void}) {
  const rows = [["/", "Focus task search"], ["N", "New work order"], ["J / ↓", "Next work order"], ["K / ↑", "Previous work order"], ["Enter / T", "Open selected work order"], ["C", "Focus activity composer"], ["Q", "Return to queue"], ["⌘ / Ctrl + K", "Command palette"], ["?", "Shortcut reference"], ["Esc", "Close a dialog"]];
  return <Dialog title="Keyboard shortcuts" close={close} className="help-dialog"><dl>{rows.map(([key, value]) => <div key={key}><dt><kbd>{key}</kbd></dt><dd>{value}</dd></div>)}</dl></Dialog>;
}

function Dialog({title, close, className = "", children}: {title: string; close: () => void; className?: string; children: ComponentChildren}) {
  const panel = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<Element | null>(null);
  useEffect(() => {
    previousFocus.current = document.activeElement;
    const node = panel.current;
    ((node?.querySelector("[autofocus]") as HTMLElement | null) ?? node)?.focus();
    return () => { if (previousFocus.current instanceof HTMLElement) previousFocus.current.focus(); };
  }, []);
  const keyDown: JSX.KeyboardEventHandler<HTMLDivElement> = (event) => {
    if (event.key === "Escape") {event.preventDefault(); close(); return;}
    if (event.key !== "Tab" || !panel.current) return;
    const nodes = [...panel.current.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])")].filter((node) => !node.hidden);
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last?.focus();}
    else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first?.focus();}
  };
  return <div class="dialog-layer" role="presentation" onMouseDown={(event) => {if (event.target === event.currentTarget) close();}}>
    <div ref={panel} class={`dialog-panel ${className}`} role="dialog" aria-modal="true" aria-labelledby="dialog-title" tabIndex={-1} onKeyDown={keyDown}>
      <header class="dialog-header"><div><span class="eyebrow">MergeRail operations</span><h2 id="dialog-title">{title}</h2></div><button class="icon-button" onClick={close} aria-label="Close"><Icon name="close" /></button></header>{children}
    </div>
  </div>;
}

function MobileBar({selected, detail, onQueue, onDetail, onNew}: {selected: boolean; detail: boolean; onQueue: () => void; onDetail: () => void; onNew: () => void}) {
  return <nav class="mobile-bar" aria-label="Primary views"><button aria-current={!detail} onClick={onQueue}><Icon name="archive" />Queue</button><button aria-current={detail} disabled={!selected} onClick={onDetail}><Icon name="file" />Work order</button><button onClick={onNew}><Icon name="add" />New</button></nav>;
}

function ToastRegion({items, dismiss}: {items: Toast[]; dismiss: (id: number) => void}) {
  return <div class="toast-region" aria-live="polite">{items.map((item) => <div key={item.id} class={`toast toast-${item.kind}`} role={item.kind === "danger" ? "alert" : "status"}><Icon name={item.kind === "danger" ? "warning" : "check"} /><span>{item.message}</span><button onClick={() => dismiss(item.id)} aria-label="Dismiss"><Icon name="close" /></button></div>)}</div>;
}

function actionsFor(task: Task): Array<[string, string, string]> {
  const actions: Array<[string, string, string]> = [];
  if (!task.open && task.delivery?.status === "blocked") actions.push(["retry-delivery", "Retry delivery", "retry"]);
  if (task.deployment?.status === "failed") actions.push(["retry-deploy", "Retry deploy", "retry"]);
  if (!task.open) actions.push(["retry-task", "Retry task", "retry"]);
  if (["running", "review"].includes(task.status)) actions.push(["cancel", "Cancel", "stop"]);
  if (task.open && task.status === "new") actions.push(["close", "Close", "archive"]);
  if (!["running", "review", "approved", "delivering", "cancelling"].includes(task.status)) actions.push(["delete", "Delete", "trash"]);
  return actions;
}

function safeGet(key: string): string | null { try { return localStorage.getItem(key); } catch { return null; } }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : "Request failed"; }
function firstLine(value: string): string { return value.trim().split(/\r?\n/, 1)[0] ?? ""; }
function formatTime(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
}
