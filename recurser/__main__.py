"""``python -m recurser``."""
import signal
import sys

if "serve" in sys.argv[1:]:
    # As the first process of a container, Python ignores a stop request until a handler exists.  The server
    # takes it here, before the command line is even imported, so that the moment in which it is lost is as
    # short as the interpreter allows.  Other commands keep the usual behaviour: a stop request ends them
    # with a failure status.
    def _leave(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _leave)

from .cli import main  # noqa: E402

sys.exit(main())
