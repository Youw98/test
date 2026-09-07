"""Locating files that move when the application is frozen into an executable.

Running from a checkout, everything is where the source says it is. Inside a
PyInstaller bundle nothing is: read-only resources are unpacked to a temporary
directory that changes every launch, and the working directory is wherever the user
happened to double-click from — which on Windows may well be somewhere unwritable.

Three questions, answered once here so no other module has to know it is frozen:

* Where are the bundled resources? -- :func:`static_dir`, :func:`default_config_path`
* Where may we write? -- :func:`data_home`
* Are we frozen at all? -- :func:`is_frozen`
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "KasFlex"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_root() -> Path:
    """Directory holding bundled read-only resources.

    Frozen, this is PyInstaller's unpack directory (``sys._MEIPASS``), which is
    temporary and differs between launches. Never write into it.
    """
    if is_frozen():
        return Path(sys._MEIPASS)  # noqa: SLF001 - the documented PyInstaller API
    return Path(__file__).resolve().parent


def static_dir() -> Path:
    """The interface's HTML, CSS and JavaScript."""
    if is_frozen():
        return resource_root() / "kasflex" / "ui" / "static"
    return Path(__file__).resolve().parent / "ui" / "static"


def default_config_path() -> Path:
    """The scenario the application opens with.

    A relative path works from a checkout and fails from an executable launched by
    double-click, where the working directory is arbitrary. Preference order:

    1. ``KASFLEX_CONFIG`` in the environment, for anyone scripting it.
    2. A ``configs/`` directory beside the executable or the current directory, so
       a user can drop in their own scenario without rebuilding anything.
    3. The copy bundled inside the application.
    """
    override = os.environ.get("KASFLEX_CONFIG")
    if override:
        return Path(override).expanduser()

    name = "scenario_westland_winter.yaml"
    candidates = [Path.cwd() / "configs" / name]
    if is_frozen():
        candidates.insert(0, Path(sys.executable).resolve().parent / "configs" / name)
        candidates.append(resource_root() / "configs" / name)
    else:
        candidates.append(Path(__file__).resolve().parents[2] / "configs" / name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]  # report the bundled path in the error the caller raises


def data_home() -> Path:
    """Directory for everything the application writes: results, cache, traces.

    From a checkout this is the current directory, so a developer's ``results/``
    lands where they expect. Frozen, it is a folder in the user's home: writing
    beside the executable fails outright under Program Files, and writing to the
    working directory scatters audit logs wherever someone happened to launch it.

    ``KASFLEX_HOME`` overrides both.
    """
    override = os.environ.get("KASFLEX_HOME")
    if override:
        path = Path(override).expanduser()
    elif is_frozen():
        path = Path.home() / APP_NAME
    else:
        return Path.cwd()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_output(path: str | Path) -> Path:
    """Place a relative output path under :func:`data_home`.

    Absolute paths are honoured as given: someone who names a location means it.
    """
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    resolved = data_home() / candidate
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved
