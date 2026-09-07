# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the KasFlex desktop build.

Produces a single self-contained application that opens the interface when
launched with no arguments, and still exposes the whole command line when given
some. Build it on the platform you are targeting:

    pyinstaller packaging/kasflex.spec --noconfirm

PyInstaller does not cross-compile. A Windows .exe must be built on Windows; see
.github/workflows/release.yml, which builds all three platforms on CI.

Two things this spec exists to get right:

* **Data files.** The interface's HTML, CSS and JavaScript and the default scenario
  are not importable modules, so PyInstaller would not find them. They are listed
  explicitly and resolved at runtime by kasflex.resources.
* **Hidden imports.** Planners, greenhouse models and data sources are selected by
  name at runtime, so nothing imports them statically and the dependency analysis
  cannot see them. Left out, the application builds cleanly and then fails the
  moment someone picks a planner.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src" / "kasflex"

datas = [
    (str(SRC / "ui" / "static"), "kasflex/ui/static"),
    (str(ROOT / "configs"), "configs"),
]

# Imported by name at runtime, never statically. See the module docstring.
hiddenimports = [
    *collect_submodules("kasflex.controllers"),
    *collect_submodules("kasflex.forecast"),
    *collect_submodules("kasflex.adapters"),
    *collect_submodules("kasflex.data"),
    "kasflex.ui.server",
]

# The optional extras are deliberately excluded. power-grid-model is a phase-2
# adapter that pulls a large binary wheel, and the language-model client needs a
# network and a key -- neither belongs in a double-clickable build whose selling
# point is that it runs offline. Both remain available from a pip install.
excludes = [
    "power_grid_model",
    "power_grid_model_ds",
    "anthropic",
    "matplotlib",
    "tkinter",
    "pytest",
    "IPython",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="KasFlex",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # A console window is kept on purpose. The application prints the address it
    # is serving on and any error it hits; hiding that turns every failure into
    # "the icon does nothing", which is the least diagnosable bug there is.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
