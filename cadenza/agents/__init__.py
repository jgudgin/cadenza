"""One process-wide registry. Agent modules register themselves on import,
the same way cadence's `@flow.step` decorators do - importing the workflow
module is what wires it up, nothing else has to.
"""

from dotenv import load_dotenv

# Must run before any agent submodule is imported below: some (e.g.
# self_maintain.maintain) read config out of the environment at import
# time, so .env needs to already be loaded into os.environ before that
# happens - not just before this package's own callers get control back.
load_dotenv()

from ..registry import Registry

registry = Registry()

from . import self_maintain  # noqa: E402,F401  (import for registration side-effects)

__all__ = ["registry"]
