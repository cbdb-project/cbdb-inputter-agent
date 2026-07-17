"""Lets `python -m cbdb_agent ...` work - without this, Python refuses to run a
package directly (`No module named cbdb_agent.__main__`) even though cli.py's
own `if __name__ == "__main__":` guard only fires for `python -m cbdb_agent.cli`.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
