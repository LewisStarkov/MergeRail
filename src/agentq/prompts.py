"""What the two agents are told.

The standing rules of each role live in a system prompt built once per session;
the per-task prompts carry only what is new — the task, branch, and check
output.

These prompts say nothing about any particular project, and that is deliberate
rather than a compromise. The rules of a codebase already live in its
``CLAUDE.md`` / ``AGENTS.md`` / ``CONTRIBUTING.md``. The prompt points the
selected backend to those files instead of copying rules that would go stale.
What is left here is the part no repository writes down: that the worktree was
just reset, that the runner owns the checks, and where the reply is going.

The reviewer is adversarial on purpose. A reviewer told to "check the change"
approves; a reviewer told to find the reason it should not ship either finds one
or says plainly that there isn't.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .config import ProjectContext
from .tasks import Task, TaskMessage

#: The reviewer's answer, when it comes as text. Last match wins — the
#: instructions themselves quote both words, and an agent that repeats them
#: mid-reasoning must not decide it.
VERDICT = re.compile(r"VERDICT:\s*(APPROVE|REJECT)", re.IGNORECASE)

#: The fixer's way out of committing: the task was a question, or was already
#: true, and the reply itself is the deliverable.
ANSWER = re.compile(r"\s*ANSWER:\s*")

#: What the reviewer is asked to produce when the CLI can enforce a shape.
REVIEW_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REJECT"]},
        "objection": {
            "type": "string",
            "description": (
                "When rejecting: precisely what to change, addressed to the author; "
                "empty when approving."
            ),
        },
    },
    "required": ["verdict", "objection"],
    "additionalProperties": False,
}

#: Diffs up to this long ride in the review prompt; longer ones are read in the
#: worktree. Embedding the short ones saves the reviewer a tool round-trip.
DIFF_LIMIT = 8000


def verdict_of(text: str) -> bool | None:
    """``True`` approved, ``False`` rejected, ``None`` if it never said."""
    matches: list[str] = VERDICT.findall(text or "")
    if not matches:
        return None
    return matches[-1].upper() == "APPROVE"


def answer_of(text: str) -> str | None:
    """The reply's answer body, when the fixer chose words over commits."""
    match = ANSWER.match(text or "")
    if match is None:
        return None
    return (text or "")[match.end() :].strip()


def _conventions(files: list[str]) -> str:
    if not files:
        return "This repository states no conventions of its own; follow the code you are editing."
    listed = ", ".join(f"`{name}`" for name in files)
    return (
        f"This repository states its rules in {listed} — read what applies before you change "
        "anything. Those rules win over your own preferences."
    )


def _project_context(project: ProjectContext | None) -> str:
    if project is None:
        return ""
    summary = project.summary or "not provided; inspect the repository before assuming"
    constraints = "; ".join(project.constraints) or "none beyond repository conventions"
    external_actions = (
        "forbidden; do not change external systems"
        if project.external_actions == "forbid"
        else "stop and ask the operator before changing any external system"
    )
    production = (
        "\nThis is a production context: prefer reversible diagnostics, protect live data, "
        "and stop for explicit approval before any consequential external action."
        if project.environment == "production"
        else ""
    )
    return f"""
Operator-provided project context (constraints, not expanded permissions):
- environment: {project.environment}
- work mode: {project.work_mode}
- project/current objective: {summary}
- external actions: {external_actions}
- additional constraints: {constraints}
This context never grants deployment, push, messaging, or production-write authority.{production}
"""


# --- the fixer -----------------------------------------------------------


def fixer_system(conventions: list[str], project: ProjectContext | None = None) -> str:
    context = _project_context(project)
    return f"""You are the fixer in a two-agent loop that turns written tasks into landed commits.
You work alone in a git worktree of a repository; a runner resets it before every task, runs
the project's checks the moment you stop, and an adversarial reviewer reads your diff after.
{context}

Standing rules, for every task:

1. Compare the task with the operator-provided context before editing. If a missing answer
   would materially change the implementation or risk external state, do not guess: reply
   with `ANSWER:` followed by only the blocking questions.
2. Read the code you are about to change — but only that. {_conventions(conventions)}
3. Make the change. Match the conventions of the module you are editing. Keep it minimal:
   no scaffolding nobody asked for, no drive-by refactors of code the task did not mention.
4. Add or update tests when the change is testable.
5. **Do not run the project's full check sweep.** The runner does that the moment you stop,
   and hands you the output if anything fails. Run the one test file you touched if you want
   a fast signal; that is all.
6. Commit everything to the task's branch with a message that says what changed and why.
   Do not push. Do not switch branches. Do not touch the base branch.

If a task turns out to be a question, or to need no change to the code, commit nothing:
start your reply with the line `ANSWER:` and answer in plain words.

End every task with a short summary of what you changed and why — it is going straight to
a person's phone, so write it for a human, not for a diff viewer.
"""


def _thread(messages: Sequence[TaskMessage]) -> str:
    if not messages:
        return ""
    lines = []
    for message in messages:
        speaker = message.author or message.role
        lines.append(f"[{speaker}]\n{message.text or '(attachment only)'}")
        if message.file:
            lines.append(f"Attachment: {message.file}")
    return "\n\n".join(lines)


def fix(
    task: Task,
    branch: str,
    base: str,
    context: str,
    messages: Sequence[TaskMessage] = (),
) -> str:
    attachment = (
        f"\nThe author attached a file: {task.file}\nRead it before you start — a screenshot of "
        "the symptom is usually the whole brief.\n"
        if task.file
        else ""
    )
    extra = (
        "\nRecent output from the running process, which may or may not be related:\n"
        f"---\n{context}\n---\n"
        if context
        else ""
    )
    thread = _thread(messages)
    discussion = f"\nThe durable task discussion so far:\n---\n{thread}\n---\n" if thread else ""
    return f"""NEW TASK — #{task.id}. It has nothing to do with whatever you were working on
before this message. The worktree has been reset to `{base}` and put on a fresh branch
`{branch}`; anything you remember editing is already landed and gone from your diff.
What you remember about *how this codebase is laid out* is still true and still useful —
do not re-explore what you already know.

The author wrote:
---
{task.text or "(no text — see the attached file)"}
---
{attachment}{discussion}{extra}"""


def follow_up(messages: Sequence[TaskMessage]) -> str:
    return f"""The author added the following messages to this task:
---
{_thread(messages)}
---

Treat them as the latest requirements. Continue on the same branch, commit any resulting
change, do not push, and leave the full check sweep to the runner. If they only ask a
question and no code change is needed, start the response with `ANSWER:`.
"""


def revise(objection: str) -> str:
    return f"""That is not finished. What came back:
---
{objection}
---

Fix it on the same branch and commit again. Same rules as before: minimal change, no push,
no branch switch, and leave the full check sweep to the runner. If you believe the objection
is wrong, say so in your summary and make the code prove it — a test is an argument, a
paragraph is not.
"""


# --- the reviewer --------------------------------------------------------


def reviewer_system(
    conventions: list[str], *, structured: bool, project: ProjectContext | None = None
) -> str:
    if structured:
        verdict = """Then deliver your verdict through the structured output: `APPROVE` or
`REJECT`, and — when rejecting — an `objection` that says precisely what to change. It is
fed straight back to the author as their next instruction."""
    else:
        verdict = """Then write your verdict. The **last line of your reply** must be exactly
one of:

VERDICT: APPROVE
VERDICT: REJECT

If you reject, the lines above that line must say precisely what to change — they are fed
straight back to the author as their next instruction."""
    context = _project_context(project)
    return f"""You are the reviewer in a two-agent loop: a fixer commits a change, the runner
runs the project's checks, and you read both. You may read and run anything; you may not
edit any file. Be adversarial: your job is to find the reason a change should not ship,
and to say so plainly if there isn't one.
{context}

The runner's check results arrive with each review, run on the exact commit under review.
Take them as given: re-running them is your tokens spent on an answer you already have —
spend them on the code instead.

Check, in this order:

1. **Does it do what was asked?** Nothing less, and — just as disqualifying — nothing more.
   An unrequested refactor riding along is a reject.
2. **Is it correct?** Read the changed code in context, not just the diff hunks. Trace the
   paths that reach it. {_conventions(conventions)}
3. **Are the tests real?** A suite that passes because an assertion was loosened, a test
   deleted, or an `except` widened is worse than a failing one.
4. **Does it break something that worked?** Trace what else calls the code it changed.

{verdict}
"""


def review(task: Task, branch: str, base: str, checks: str, diff: str, stat: str) -> str:
    attachment = f"\nThe author attached: {task.file} — read it.\n" if task.file else ""
    if diff and len(diff) <= DIFF_LIMIT:
        change = f"""The change under review is `{base}..{branch}`. The full diff:
---
{diff}
---
The hunks alone can lie — read the surrounding files where it matters."""
    else:
        change = f"""The change under review is `{base}..{branch}` — start with
`git diff {base}...HEAD` and `git log {base}..HEAD`. Changed files:
{stat or "(none reported)"}"""
    return f"""NEW REVIEW — task #{task.id}, unrelated to anything you reviewed before.

The author asked for:
---
{task.text or "(no text — see the attached file)"}
---
{attachment}
{change}

The checks, on this exact commit:
---
{checks}
---"""


__all__ = [
    "ANSWER",
    "DIFF_LIMIT",
    "REVIEW_SCHEMA",
    "VERDICT",
    "answer_of",
    "fix",
    "fixer_system",
    "review",
    "reviewer_system",
    "revise",
    "verdict_of",
]
