import {describe, expect, it} from "vitest";
import {shortcutFor} from "./keyboard";

const key = (value: string, overrides = {}) => shortcutFor({key: value, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...overrides});

describe("keyboard shortcuts", () => {
  it("maps queue navigation and direct actions", () => {
    expect(key("/")).toBe("search");
    expect(key("j")).toBe("next");
    expect(key("ArrowUp")).toBe("previous");
    expect(key("N")).toBe("new");
    expect(key("Q")).toBe("queue");
    expect(key("T")).toBe("task");
    expect(key("Enter")).toBe("open");
  });

  it("reserves command modifiers for the palette", () => {
    expect(key("k", {metaKey: true})).toBe("palette");
    expect(key("k", {ctrlKey: true})).toBe("palette");
    expect(key("j", {metaKey: true})).toBeNull();
  });
});
