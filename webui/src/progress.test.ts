import {describe, expect, it} from "vitest";
import {taskProgress} from "./progress";
import type {Task} from "./types";

const task: Task = {id: 1, text: "Improve delivery", status: "running", open: true};

describe("task progress evidence", () => {
  it("does not treat a saved checkpoint as approval", () => {
    const progress = taskProgress({...task, status: "failed", execution: {phase: "checkpoint-verified", result_sha: "a".repeat(40)}, live: {stage: "reviewing"}});
    expect(progress.attention).toBe(true);
    expect(progress.steps.some((step) => step.state === "complete")).toBe(false);
    expect(progress.actor).toBe("MergeRail · система");
  });
  it("shows general checks separately from the developer's test report", () => {
    const progress = taskProgress({...task, live: {stage: "running checks", roles: {fixer: {status: "complete"}}}});
    expect(progress.steps.map((step) => step.state)).toEqual(["complete", "current", "pending", "pending", "optional"]);
  });
  it("returns to changes when a new fixer round begins despite stale review status", () => {
    const progress = taskProgress({...task, status: "review", live: {stage: "reviewing", roles: {fixer: {status: "round 2"}, reviewer: {status: "complete"}}}});
    expect(progress.actor).toBe("Разработчик · Fixer");
    expect(progress.steps[2]?.state).toBe("pending");
  });
  it("distinguishes recovery from confirmed completion", () => {
    const progress = taskProgress({...task, execution: {phase: "resuming-checkpoint", result_sha: "a".repeat(40)}});
    expect(progress.title).toContain("Восстанавливается");
    expect(progress.steps[2]?.state).toBe("pending");
  });
  it("does not infer checks, review, delivery or publication for a completed answer", () => {
    const progress = taskProgress({...task, status: "done"});
    expect(progress.steps.some((step) => step.state === "complete")).toBe(false);
    expect(progress.title).toBe("Работа завершена");
  });
  it.each(["queued", "running", "failed", "blocked", "superseded"])("does not infer publication from deployment %s", (status) => {
    const progress = taskProgress({...task, status: "done", delivery: {status: "succeeded"}, deployment: {status}});
    expect(progress.steps[4]?.state).not.toBe("complete");
    expect(progress.steps.slice(0, 4).every((step) => step.state === "complete")).toBe(true);
  });
  it("uses the real DevBot succeeded status for confirmed publication", () => {
    const progress = taskProgress({...task, status: "done", deployment: {status: "succeeded"}});
    expect(progress.steps[4]?.state).toBe("complete");
    expect(progress.title).toContain("подтвердил");
  });
});
