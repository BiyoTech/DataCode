"""Task execution plane (design 04 §3).

Work that must not run in this process is submitted here. The submitter picks the
sandbox from the task's kind, a backend provides the vehicle, and the audit
module records what happened.

Callers use :func:`src.tasks.submitter.get_task_submitter` and name a task kind —
never a level, a Job or a process.
"""
