"""Guards against documentation drifting away from the code.

Every one of these caught something real. The module map listed modules that had
been superseded and omitted three that existed; the requirement tally said
"24 done" when the table itself counted 32; the plan's header still described the
project two stages behind. None of that is visible from inside the code, and all of
it is what a reviewer reads first.

These are deliberately cheap and mechanical. They cannot tell you whether the prose
is *true*, only whether the things it names still exist and its arithmetic is right.
That is enough to stop the specific failure of a document quietly aging.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC = ROOT / "src" / "kasflex"


def read(path: Path) -> str:
    # Be explicit on Windows, where the locale codec is not necessarily UTF-8 and
    # would silently mojibake the status markers this module deliberately checks.
    return path.read_text(encoding="utf-8")


# --- the module map must name modules that exist ---------------------------


def test_architecture_module_map_matches_the_package():
    listed = re.findall(r"^\| `kasflex\.([\w.*]+)` \|", read(DOCS / "ARCHITECTURE.md"), re.M)
    assert listed, "the module map is missing entirely"

    missing = []
    for module in listed:
        base = module.rstrip(".*").replace(".", "/")
        if not (SRC / base).exists() and not (SRC / f"{base}.py").exists():
            missing.append(module)
    assert not missing, (
        f"docs/ARCHITECTURE.md names modules that do not exist: {missing}"
    )


def test_every_top_level_module_is_described():
    """A new module that nobody documented is the usual way the map goes stale."""
    described = set(
        re.findall(r"^\| `kasflex\.([\w.*]+)` \|", read(DOCS / "ARCHITECTURE.md"), re.M)
    )
    roots = {name.rstrip(".*").split(".")[0] for name in described}

    actual = {
        p.stem if p.is_file() else p.name
        for p in SRC.iterdir()
        if (p.suffix == ".py" and p.stem not in {"__init__", "cli", "config"})
        or (p.is_dir() and not p.name.startswith("__"))
    }
    undocumented = actual - roots
    assert not undocumented, (
        f"these modules exist but the architecture module map does not mention "
        f"them: {sorted(undocumented)}"
    )


# --- the CLI must be documented --------------------------------------------


def test_every_cli_command_appears_in_the_usage_guide():
    from kasflex.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])

    source = read(SRC / "cli.py")
    commands = set(re.findall(r'sub\.add_parser\(\s*"(\w+)"', source))
    assert commands, "no subcommands found in cli.py"

    usage = read(DOCS / "USAGE.md")
    undocumented = {c for c in commands if not re.search(rf"kasflex {c}\b", usage)}
    assert not undocumented, (
        f"these commands exist but docs/USAGE.md never shows them: {sorted(undocumented)}"
    )


# --- the plan's own arithmetic ---------------------------------------------


def test_requirement_tally_matches_the_table():
    """The tally said 24 done while the table listed 32. Nobody recounts by hand."""
    plan = read(DOCS / "MVP_PLAN.md")
    rows = [line for line in plan.splitlines() if re.match(r"^\| (R\d+|—) \|", line)]
    assert len(rows) > 30, f"only found {len(rows)} requirement rows; has the table moved?"

    counted = {
        "done": sum("| ✅" in r for r in rows),
        "part": sum("| 🔶" in r for r in rows),
        "todo": sum("| ⬜" in r for r in rows),
    }
    assert sum(counted.values()) == len(rows), (
        f"every requirement row needs exactly one status marker; "
        f"{len(rows) - sum(counted.values())} row(s) have none"
    )

    stated = re.search(r"✅ (\d+) · 🔶 (\d+) · ⬜ (\d+)", plan)
    assert stated, "the requirement tally line is missing from docs/MVP_PLAN.md"
    assert [int(g) for g in stated.groups()] == [
        counted["done"], counted["part"], counted["todo"]
    ], (
        f"the tally says {stated.group(0)} but the table counts "
        f"✅ {counted['done']} · 🔶 {counted['part']} · ⬜ {counted['todo']}"
    )


def test_the_docs_do_not_hard_code_a_test_count():
    """A count like "221 tests" is stale the day after it is written.

    Worse, it is not even well defined: the number collected depends on which
    optional extras are installed, so the same commit legitimately reports 215 or
    221. Policing a moving number is machinery in service of nothing — the sentence
    reads the same either way. So the rule is simply not to claim one.
    """
    offenders: list[str] = []
    for path in [ROOT / "README.md", *sorted(DOCS.glob("*.md"))]:
        for match in re.finditer(r"\b\d+ tests\b", read(path)):
            offenders.append(f"{path.name}: {match.group(0)!r}")
    assert not offenders, (
        "these documents hard-code a test count, which goes stale immediately and "
        "varies with the installed extras: " + ", ".join(offenders) +
        ". Describe the suite instead of counting it."
    )


# --- claims that must never quietly disappear ------------------------------


@pytest.mark.parametrize(
    "path, needle",
    [
        ("README.md", "Simulation only"),
        ("README.md", "validated for operational use"),
        ("README.md", "apparatus, not findings"),
        ("docs/MVP_PLAN.md", "apparatus"),
        ("docs/USAGE.md", "not validated"),
    ],
)
def test_the_honesty_notices_survive(path, needle):
    """R31, and the rule that no surrogate figure may be published as a result.

    These sentences are load-bearing: they are what stops a plausible number from
    being read as a finding. An edit that removes one should fail the suite, not
    pass quietly.
    """
    # Normalise whitespace: these sentences are wrapped in the source, so a
    # literal search would fail on a reflow rather than on a real removal.
    text = " ".join(read(ROOT / path).split()).lower()
    assert needle.lower() in text, f"{path} no longer contains {needle!r}"
