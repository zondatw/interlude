"""Interlude — capture AI coding agent <-> API traffic for prompt-architecture analysis.

This package exposes three entry points (declared in pyproject.toml):

    interlude          → interlude.proxy:main   (bundled launcher: proxy + UI)
    interlude-report   → interlude.report:main  (web UI alone, reads logs)
    interlude-analyze  → interlude.analyze:main (text report)

Each module is also runnable as `python -m interlude.<name>` for diagnostics.
"""

__version__ = "0.1.0"
