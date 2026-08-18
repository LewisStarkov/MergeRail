// @vitest-environment jsdom

import {cleanup, fireEvent, render, screen} from "@testing-library/preact";
import {useState} from "preact/hooks";
import {afterEach, describe, expect, it} from "vitest";
import {LedgerSelect, type LedgerOption} from "./LedgerSelect";

const options: LedgerOption[] = [
  {value: "auto", label: "Auto"},
  {value: "offline", label: "Offline agent", disabled: true},
  {value: "codex", label: "Codex"},
  {value: "claude", label: "Claude"},
];

function Harness() {
  const [value, setValue] = useState("auto");
  return <LedgerSelect label="AI agent" value={value} options={options} onChange={setValue} />;
}

afterEach(cleanup);

describe("LedgerSelect", () => {
  it("skips disabled options with arrows and selects with Enter", () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", {name: "AI agent Auto"});
    trigger.focus();
    fireEvent.keyDown(trigger, {key: "ArrowDown"});

    const listbox = screen.getByRole("listbox", {name: "AI agent"});
    expect(screen.getByRole("option", {name: "Offline agent"}).getAttribute("aria-disabled")).toBe("true");
    fireEvent.keyDown(listbox, {key: "Enter"});

    expect(document.activeElement).toBe(screen.getByRole("button", {name: "AI agent Codex"}));
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("supports Home, End, Space and restores focus on Escape", () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", {name: "AI agent Auto"});
    fireEvent.keyDown(trigger, {key: " "});
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, {key: "End"});
    fireEvent.keyDown(listbox, {key: "Enter"});
    expect(document.activeElement).toBe(screen.getByRole("button", {name: "AI agent Claude"}));

    fireEvent.keyDown(screen.getByRole("button", {name: "AI agent Claude"}), {key: "ArrowUp"});
    fireEvent.keyDown(screen.getByRole("listbox"), {key: "Enter"});
    expect(document.activeElement).toBe(screen.getByRole("button", {name: "AI agent Codex"}));

    fireEvent.keyDown(screen.getByRole("button", {name: "AI agent Codex"}), {key: "Home"});
    fireEvent.keyDown(screen.getByRole("listbox"), {key: "Escape"});
    expect(document.activeElement).toBe(screen.getByRole("button", {name: "AI agent Codex"}));
  });

  it("selects enabled options by click and ignores disabled ones", () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", {name: "AI agent Auto"}));
    fireEvent.click(screen.getByRole("option", {name: "Offline agent"}));
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.click(screen.getByRole("option", {name: "Codex"}));
    expect(screen.getByRole("button", {name: "AI agent Codex"})).toBeTruthy();
  });
});
