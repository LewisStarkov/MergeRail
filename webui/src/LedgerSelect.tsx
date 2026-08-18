import {type JSX} from "preact";
import {useEffect, useId, useRef, useState} from "preact/hooks";
import {Icon} from "./icons";

export interface LedgerOption {
  value: string;
  label: string;
  disabled?: boolean;
}

interface LedgerSelectProps {
  label: string;
  value: string;
  options: LedgerOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
}

export function LedgerSelect({label, value, options, onChange, disabled = false}: LedgerSelectProps) {
  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const listbox = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(() => selectedIndex(options, value));
  const current = options.find((option) => option.value === value) ?? options.find((option) => !option.disabled);

  useEffect(() => {
    if (!open) setActive(selectedIndex(options, value));
  }, [open, options, value]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  useEffect(() => {
    if (open) listbox.current?.focus();
  }, [open]);

  const openAt = (index: number) => {
    if (disabled || !options.length) return;
    setActive(enabledIndex(options, index, 1));
    setOpen(true);
  };

  const closeAndFocus = () => {
    setOpen(false);
    trigger.current?.focus();
  };

  const choose = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    closeAndFocus();
  };

  const onTriggerKeyDown: JSX.KeyboardEventHandler<HTMLButtonElement> = (event) => {
    if (event.key === "ArrowDown") { event.preventDefault(); openAt(enabledIndex(options, selectedIndex(options, value) + 1, 1)); }
    else if (event.key === "ArrowUp") { event.preventDefault(); openAt(enabledIndex(options, selectedIndex(options, value) - 1, -1)); }
    else if (event.key === "Home") { event.preventDefault(); openAt(enabledIndex(options, 0, 1)); }
    else if (event.key === "End") { event.preventDefault(); openAt(enabledIndex(options, options.length - 1, -1)); }
    else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openAt(selectedIndex(options, value)); }
  };

  const onListKeyDown: JSX.KeyboardEventHandler<HTMLDivElement> = (event) => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeAndFocus(); }
    else if (event.key === "ArrowDown") { event.preventDefault(); setActive((index) => enabledIndex(options, index + 1, 1)); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setActive((index) => enabledIndex(options, index - 1, -1)); }
    else if (event.key === "Home") { event.preventDefault(); setActive(enabledIndex(options, 0, 1)); }
    else if (event.key === "End") { event.preventDefault(); setActive(enabledIndex(options, options.length - 1, -1)); }
    else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(active); }
    else if (event.key === "Tab") setOpen(false);
  };

  const labelId = `${id}-label`;
  const valueId = `${id}-value`;
  const listId = `${id}-listbox`;
  return <div class="selector-field" ref={root}>
    <span id={labelId} class="selector-label">{label}</span>
    <button ref={trigger} type="button" class="selector-trigger" aria-labelledby={`${labelId} ${valueId}`}
      aria-haspopup="listbox" aria-expanded={open} aria-controls={listId} disabled={disabled}
      onClick={() => open ? closeAndFocus() : openAt(selectedIndex(options, value))} onKeyDown={onTriggerKeyDown}>
      <span id={valueId}>{current?.label || "Select"}</span><Icon name="chevron" />
    </button>
    {open && <div ref={listbox} id={listId} class="selector-listbox" role="listbox" tabIndex={-1}
      aria-labelledby={labelId} aria-activedescendant={`${id}-option-${active}`} onKeyDown={onListKeyDown}>
      {options.map((option, index) => <div id={`${id}-option-${index}`} key={option.value} role="option"
        class={index === active ? "is-active" : ""} aria-selected={option.value === value}
        aria-disabled={option.disabled || undefined} onPointerMove={() => {if (!option.disabled) setActive(index);}}
        onClick={() => choose(index)}>
        <span>{option.label}</span>{option.value === value && <Icon name="check" />}
      </div>)}
    </div>}
  </div>;
}

function selectedIndex(options: LedgerOption[], value: string): number {
  const index = options.findIndex((option) => option.value === value && !option.disabled);
  return index >= 0 ? index : enabledIndex(options, 0, 1);
}

function enabledIndex(options: LedgerOption[], start: number, direction: 1 | -1): number {
  if (!options.length) return -1;
  for (let offset = 0; offset < options.length; offset += 1) {
    const index = (start + offset * direction + options.length) % options.length;
    if (!options[index]?.disabled) return index;
  }
  return -1;
}
