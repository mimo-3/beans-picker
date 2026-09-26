"""`python -m cua_jev`: the same as the `cua-jev` command."""

import sys

from cua_jev.cli import main

if __name__ == "__main__":
    sys.exit(main())
