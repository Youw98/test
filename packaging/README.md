# Building the desktop application

The goal: someone who has never installed Python double-clicks one file and gets
the KasFlex interface in their browser.

## Getting one without building it

Download it from the [releases page](https://github.com/youw98/test/releases) —
`KasFlex.exe` for Windows, `KasFlex` for macOS or Linux. That is the intended route
for anyone who just wants to use the thing.

## Building it yourself

**PyInstaller does not cross-compile.** A Windows `.exe` must be built on Windows, a
macOS binary on macOS. This is why `.github/workflows/release.yml` exists: it builds
all three on CI, smoke-tests each one, and attaches them to a tagged release.

Two ways to start it: push a `v*` tag, or run **Actions -> Build applications ->
Run workflow** and type the version (`v0.1.0`) into `release_tag`. The second exists
because pushing a tag needs direct git access to the remote, which a machine behind
a restrictive proxy does not always have; the release job then creates the tag on
the commit it built. Running it with `release_tag` empty builds and smoke-tests the
binaries without publishing anything, which is the useful thing to do before
releasing.

On the platform you are targeting:

```bash
pip install -e . pyinstaller
pyinstaller packaging/kasflex.spec --noconfirm
```

The result is a single self-contained file in `dist/`, about 25 MB.

## What the spec has to get right

Two things PyInstaller cannot work out on its own, both of which produce a build
that succeeds and then fails in use:

**Data files.** The interface's HTML, CSS and JavaScript and the default scenario
are not importable modules, so the dependency analysis never sees them. Left out,
the application starts and serves a blank page.

**Runtime-selected imports.** Planners, greenhouse models and data sources are
chosen by name from configuration, so nothing imports them statically. Left out,
the build is clean and then fails the moment someone picks a planner.

`tests/test_resources.py` asserts the spec still declares both.

## What changes when frozen

`kasflex.resources` answers three questions so no other module has to know whether
it is packaged:

| | From a checkout | Frozen |
|---|---|---|
| Interface files | `src/kasflex/ui/static` | PyInstaller's unpack directory |
| Default scenario | `configs/` in the repository | bundled, or `configs/` beside the app |
| Where writes go | the working directory | `~/KasFlex` |

The last row matters most. A packaged application launched by double-click has an
arbitrary working directory, which on Windows may be somewhere unwritable — so
results, the cache and the audit log go to a folder in the user's home instead.
`KASFLEX_HOME` overrides it, `KASFLEX_CONFIG` overrides the scenario.

Started with no arguments a frozen build opens the interface, because that is what
someone who double-clicked an icon wanted. Given arguments it is the same CLI as a
`pip install`, so the packaged build cannot drift from the documented commands.

The console window is kept deliberately. It prints the address being served and any
error; hiding it turns every failure into "the icon does nothing".

## Signing

The applications are unsigned, so Windows SmartScreen warns on first launch and
macOS Gatekeeper blocks it until the quarantine attribute is cleared. Signing needs
a paid certificate per platform. If this project ever ships to growers rather than
researchers, that becomes necessary; for now the release notes explain the warning.
