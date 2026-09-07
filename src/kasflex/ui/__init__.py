"""Browser interface (R27-R31).

A local, single-page interface so the scenario can be adjusted and run without
editing YAML over SSH, and so a plan can be approved, edited or rejected by a
person rather than by a flag.
"""

from kasflex.ui.server import ADJUSTABLE, UiServer, serve

__all__ = ["ADJUSTABLE", "UiServer", "serve"]
