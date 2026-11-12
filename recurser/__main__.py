"""``python -m recurser``."""
import signal
import sys


def _stopped_early(signum, frame):
    # As the first process of a container, Python ignores a stop request until a handler exists, and loading
    # the command line takes a moment.  A command stopped that early ends at once, with the status a stop
    # request normally leaves.  Once the command is known, `serve` puts its own handler in place (it ends
    # with success) and every other command gets the usual behaviour back.
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, _stopped_early)

from .cli import main  # noqa: E402

sys.exit(main())
