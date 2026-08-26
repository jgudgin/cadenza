"""What an agent is allowed to say about how its task went.

Borrowed directly from cadence's handler vocabulary, because it is the right
shape for any at-least-once task runner, not just a linear pipeline: an
agent either succeeds (return a dict), or it fails in one of three distinct
ways that the orchestrator needs to treat differently.
"""

from __future__ import annotations


class Retry(Exception):
    """Something temporary went wrong - a network blip, a rate limit, a
    flaky tool. The task goes back to 'pending' with backoff and will be
    claimed again later."""


class Permanent(Exception):
    """This task can never succeed as specified - malformed input, a
    contradiction in the request. Retrying will not help. The task (and
    anything that depends on it) is marked failed."""


class Drop(Exception):
    """Not a failure - the planner decided, after the fact, that this task
    is no longer needed (e.g. superseded by a replan). Does not show up as
    an error in the trace."""
