#!/usr/bin/env python3
"""Run the savings pipeline (see carsavings/run.py and the README).

    python pipeline.py --csv examples/smoke.csv --workers 2
"""

import sys

from carsavings.run import main

if __name__ == "__main__":
    sys.exit(main())
