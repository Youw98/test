"""Entry point for the packaged application.

Thin on purpose: everything it does is call the same CLI a pip install exposes, so
the packaged build cannot drift from the documented commands. The only difference
in behaviour lives in kasflex.cli.main, which opens the interface when a frozen
build is started with no arguments.
"""

from __future__ import annotations

import sys


def main() -> int:
    # multiprocessing is not used today, but a frozen build that later grows a
    # worker pool will spawn copies of itself without this and the symptom -- the
    # application starting over and over -- is baffling if you have not seen it.
    import multiprocessing

    multiprocessing.freeze_support()

    from kasflex.cli import main as cli_main

    try:
        return cli_main()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        # A packaged application must never die silently: on Windows the console
        # closes with the process and takes the traceback with it.
        import traceback

        traceback.print_exc()
        print(f"\nKasFlex stopped: {exc}", file=sys.stderr)
        try:
            input("\nPress Enter to close…")
        except (EOFError, KeyboardInterrupt):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
