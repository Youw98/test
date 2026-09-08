"""Resource resolution, which changes meaning the moment the app is frozen.

Running from a checkout every path works by accident: the source tree is right
there and the working directory is the repository. In a packaged build neither
holds, and the failures are quiet — a missing stylesheet renders an unstyled page,
an unwritable audit log loses the human decisions the project exists to record.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kasflex import resources


def test_not_frozen_when_running_from_source():
    assert resources.is_frozen() is False


def test_static_directory_holds_the_interface():
    static = resources.static_dir()
    assert static.is_dir()
    for name in ("index.html", "style.css", "app.js"):
        assert (static / name).is_file(), f"{name} missing from {static}"


def test_default_config_exists_and_loads():
    from kasflex.config import ScenarioConfig

    path = resources.default_config_path()
    assert path.is_file(), f"default scenario not found at {path}"
    assert ScenarioConfig.from_yaml(path).name


def test_wheel_config_is_bundled_without_duplicate_static_force_include():
    """Keep the wheel installable and its default scenario self-contained."""
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '"configs" = "kasflex/configs"' in pyproject
    assert '"src/kasflex/ui/static" = "kasflex/ui/static"' not in pyproject


def test_config_can_be_overridden_by_environment(monkeypatch, tmp_path):
    target = tmp_path / "mine.yaml"
    target.write_text("name: x\ndate: '2023-01-01'\n")
    monkeypatch.setenv("KASFLEX_CONFIG", str(target))
    assert resources.default_config_path() == target


def test_data_home_is_the_working_directory_from_a_checkout(monkeypatch):
    monkeypatch.delenv("KASFLEX_HOME", raising=False)
    assert resources.data_home() == Path.cwd()


def test_data_home_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("KASFLEX_HOME", str(tmp_path / "elsewhere"))
    home = resources.data_home()
    assert home == tmp_path / "elsewhere"
    assert home.is_dir(), "data_home must create the directory it names"


def test_relative_outputs_land_under_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("KASFLEX_HOME", str(tmp_path))
    resolved = resources.resolve_output("results/runs.jsonl")
    assert resolved == tmp_path / "results" / "runs.jsonl"
    assert resolved.parent.is_dir(), "the parent directory must be created"


def test_absolute_outputs_are_left_alone(monkeypatch, tmp_path):
    """Someone who names a location means it."""
    monkeypatch.setenv("KASFLEX_HOME", str(tmp_path / "ignored"))
    target = tmp_path / "somewhere" / "out.jsonl"
    assert resources.resolve_output(target) == target


@pytest.mark.parametrize("name", ["index.html", "style.css", "app.js"])
def test_the_packaging_spec_ships_the_interface(name):
    """A spec that forgets the static files builds cleanly and serves a blank page."""
    spec = Path(__file__).resolve().parents[1] / "packaging" / "kasflex.spec"
    assert spec.is_file(), "the PyInstaller spec is missing"
    text = spec.read_text()
    assert 'ui" / "static"' in text, "the spec does not bundle the interface"
    assert '"configs"' in text, "the spec does not bundle the default scenario"


def test_the_spec_declares_the_runtime_selected_imports():
    """Planners and models are chosen by name, so nothing imports them statically."""
    spec = (Path(__file__).resolve().parents[1] / "packaging" / "kasflex.spec").read_text()
    for module in ("kasflex.controllers", "kasflex.forecast", "kasflex.adapters", "kasflex.data"):
        assert module in spec, (
            f"{module} is selected by name at runtime and must be a hidden import, "
            f"or the packaged build fails the moment someone picks one"
        )
