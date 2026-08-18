export type Shortcut = "search" | "new" | "next" | "previous" | "open" | "comment" | "queue" | "task" | "palette" | "help" | "escape" | null;

export function shortcutFor(event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey">): Shortcut {
  if (event.key === "Escape") return "escape";
  if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") return "palette";
  if (event.metaKey || event.ctrlKey || event.altKey) return null;
  if (event.key === "/") return "search";
  if (event.key === "?") return "help";
  const key = event.key.toLocaleLowerCase();
  if (key === "n") return "new";
  if (key === "j" || event.key === "ArrowDown") return "next";
  if (key === "k" || event.key === "ArrowUp") return "previous";
  if (event.key === "Enter") return "open";
  if (key === "c") return "comment";
  if (key === "q") return "queue";
  if (key === "t") return "task";
  return null;
}
