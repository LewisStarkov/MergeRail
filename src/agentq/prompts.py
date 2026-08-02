"""What the two agents are told.

These prompts say nothing about any particular project, and that is deliberate
rather than a compromise. The rules of a codebase already live in its
``CLAUDE.md`` / ``AGENTS.md`` / ``CONTRIBUTING.md``, which Claude Code reads on
its own; a config field asking you to restate them would only be a worse copy
that goes stale. What is left here is the part no repository writes down: that
the worktree was just reset, that the checks are somebody else's job, and that
the reply is going to a person's phone.

The reviewer is adversarial on purpose. A reviewer told to "check the change"
approves; a reviewer told to find the reason it should not ship either finds one
or says plainly that there isn't.
"""

from __future__ import annotations

import re

from .tasks import Task

#: The reviewer's answer. Last match wins — the instructions themselves quote
#: both words, and an agent that repeats them mid-reasoning must not decide it.
VERDICT = re.compile(r"VERDICT:\s*(APPROVE|REJECT)", re.IGNORECASE)


def verdict_of(text: str) -> bool | None:
    """``True`` approved, ``False`` rejected, ``None`` if it never said."""
    matches: list[str] = VERDICT.findall(text or "")
    if not matches:
        return None
    return matches[-1].upper() == "APPROVE"


def _conventions(files: list[str]) -> str:
    if not files:
        return "This repository states no conventions of its own; follow the code you are editing."
    listed = ", ".join(f"`{name}`" for name in files)
    return (
        f"This repository states its rules in {listed} — read what applies before you change "
        "anything. Those rules win over your own preferences."
    )


def fix(task: Task, branch: str, base: str, conventions: list[str], context: str) -> str:
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
    return f"""NEW TASK — #{task.id}. It has nothing to do with whatever you were working on
before this message. The worktree has been reset to `{base}` and put on a fresh branch
`{branch}`; anything you remember editing is already landed and gone from your diff.
What you remember about *how this codebase is laid out* is still true and still useful —
that is why you are the same session. Do not re-explore what you already know.

You are alone in a git worktree of this repository.

The author wrote:
---
{task.text or "(no text — see the attached file)"}
---
{attachment}{extra}
Do the work:

1. Read the code you are about to change — but only that. {_conventions(conventions)}
2. Make the change. Match the conventions of the module you are editing. Keep it minimal:
   no scaffolding nobody asked for, no drive-by refactors of code the task did not mention.
3. Add or update tests when the change is testable.
4. **Do not run the project's full check sweep.** The runner does that the moment you stop,
   and hands you the output if anything fails. Run the one test file you touched if you want
   a fast signal; that is all.
5. Commit everything to `{branch}` with a message that says what changed and why.

Do not push. Do not switch branches. Do not touch `{base}`.

When you are done, reply with a short summary of what you changed and why — it is going
straight to a person's phone, so write it for a human, not for a diff viewer.
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


def review(task: Task, branch: str, base: str, conventions: list[str], checks: str) -> str:
    attachment = f"\nThe author attached: {task.file} — read it.\n" if task.file else ""
    return f"""NEW REVIEW — task #{task.id}, unrelated to anything you reviewed before. You may
read and run anything; you may not edit any file. Be adversarial: your job is to find the
reason this should not ship, and to say so plainly if there isn't one.

The author asked for:
---
{task.text or "(no text — see the attached file)"}
---
{attachment}
The change under review is `{base}..{branch}` — start with `git diff {base}...HEAD` and
`git log {base}..HEAD`.

The runner already ran the checks, on this exact commit:
---
{checks}
---
Take that as given. Re-running them is your tokens spent on an answer you already have;
spend them on the code instead.

Check, in this order:

1. **Does it do what was asked?** Nothing less, and — just as disqualifying — nothing more.
   An unrequested refactor riding along is a reject.
2. **Is it correct?** Read the changed code in context, not just the diff hunks. Trace the
   paths that reach it. {_conventions(conventions)}
3. **Are the tests real?** A suite that passes because an assertion was loosened, a test
   deleted, or an `except` widened is worse than a failing one.
4. **Does it break something that worked?** Trace what else calls the code it changed.

Then write your verdict. The **last line of your reply** must be exactly one of:

VERDICT: APPROVE
VERDICT: REJECT

If you reject, the lines above that line must say precisely what to change — they are fed
straight back to the author as their next instruction.
"""


__all__ = ["VERDICT", "fix", "review", "revise", "verdict_of"]
