"""One process-wide registry. Agent modules register themselves on import,
the same way cadence's `@flow.step` decorators do - importing the workflow
module is what wires it up, nothing else has to.
"""

from ..registry import Registry

registry = Registry()

from . import self_maintain  # noqa: E402,F401  (import for registration side-effects)

__all__ = ["registry"]
