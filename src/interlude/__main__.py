"""`python -m interlude` shortcut → identical to the `interlude` console script.

We forward to interlude.proxy.main rather than introduce a separate dispatcher
so there's only one entrypoint to maintain. Module-form invocation stays useful
for diagnostics (`python -m interlude --no-ui`) without having the entry-point
shim on PATH.
"""

from interlude.proxy import main

if __name__ == "__main__":
    main()
