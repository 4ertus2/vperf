"""Entry point for ``python3 -m vperf``.

vperf has no runtime dependencies, so it runs straight from a checkout with no
install and no virtualenv: from the repository root run
``python3 -m vperf run -- ./yourapp``.  Use the ``vperf`` console script instead
when the package is installed (``pip install -e .``).
"""

import sys

from .cli import main

sys.exit(main())
