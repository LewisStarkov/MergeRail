"""MergeRail — write the task down; an agent does it, a reviewer checks it, it lands.

The short version::

    uvx mergerail --telegram

The embedded version, when you want the runner to own your app process too::

    from mergerail import Runner

    Runner(front="telegram").run()

Anything else is a front: implement :class:`~mergerail.fronts.base.Front` and pass
an instance instead of a name. Live streaming is an optional third hook.
"""

from __future__ import annotations

from .agent import AgentOptions, AgentReply, ClaudeAgent
from .config import Config
from .detect import Check, detect_checks
from .fronts import FolderFront, Front, StreamEvent, TelegramFront, WebFront, make_front
from .runner import Runner
from .supervisor import Supervisor
from .tasks import Status, Task, TaskStore

__version__ = "0.1.6"

__all__ = [
    "AgentOptions",
    "AgentReply",
    "Check",
    "ClaudeAgent",
    "Config",
    "FolderFront",
    "Front",
    "Runner",
    "Status",
    "StreamEvent",
    "Supervisor",
    "Task",
    "TaskStore",
    "TelegramFront",
    "WebFront",
    "__version__",
    "detect_checks",
    "make_front",
]
