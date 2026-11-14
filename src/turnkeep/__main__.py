"""Product entry point: `python -m turnkeep <command>`. Delegates to cli.main."""
import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
