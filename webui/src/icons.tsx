import type {JSX} from "preact";

const paths: Record<string, JSX.Element> = {
  add: <path d="M12 5v14M5 12h14"/>,
  adjust: <><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></>,
  archive: <><path d="M4 8h16v12H4zM3 4h18v4H3zM9 12h6"/></>,
  arrow: <><path d="M5 12h14M13 6l6 6-6 6"/></>,
  attach: <path d="m8 12 5.8-5.8a3 3 0 0 1 4.2 4.2l-7.4 7.4a5 5 0 0 1-7.1-7.1l7-7"/>,
  back: <path d="m15 18-6-6 6-6"/>,
  check: <path d="m5 12 4 4L19 6"/>,
  chevron: <path d="m7 9 5 5 5-5"/>,
  close: <path d="M6 6l12 12M18 6 6 18"/>,
  command: <><rect x="4" y="4" width="6" height="6" rx="2"/><rect x="14" y="4" width="6" height="6" rx="2"/><rect x="4" y="14" width="6" height="6" rx="2"/><rect x="14" y="14" width="6" height="6" rx="2"/><path d="M10 7v10a3 3 0 0 0 3 3h1M14 7v10a3 3 0 0 1-3 3h-1"/></>,
  comment: <path d="M4 5h16v12H9l-5 4z"/>,
  file: <><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5"/></>,
  help: <><circle cx="12" cy="12" r="9"/><path d="M9.8 9a2.3 2.3 0 1 1 3.2 2.1c-.7.3-1 1-1 1.9M12 17h.01"/></>,
  moon: <path d="M19 15.5A8 8 0 0 1 8.5 5 8 8 0 1 0 19 15.5Z"/>,
  retry: <><path d="M20 7v5h-5"/><path d="M19 12a7 7 0 1 0-2 5"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  sidebar: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></>,
  stop: <><circle cx="12" cy="12" r="9"/><path d="M9 9h6v6H9z"/></>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></>,
  trash: <><path d="M4 7h16M9 3h6l1 4M7 7l1 14h8l1-14M10 11v6M14 11v6"/></>,
  warning: <><path d="M12 3 2.5 20h19z"/><path d="M12 9v5M12 17h.01"/></>,
};

export function Icon({name, size = 16}: {name: string; size?: number}) {
  return <svg class="icon" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{paths[name] ?? <circle cx="12" cy="12" r="4"/>}</svg>;
}
