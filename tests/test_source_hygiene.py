"""Source-level invariants that no runtime test would notice.

Everything here failed silently at least once. A shadowed function does not
raise, a duplicate attribute does not warn -- the feature just quietly stops
working, and the test suite goes on passing because the wrong answer is still
a well-formed answer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "stockroom"
_MODULES = sorted(_SRC.rglob("*.py"))


@pytest.mark.parametrize("path", _MODULES, ids=lambda p: p.name)
def test_no_module_defines_the_same_name_twice(path):
    """The second definition wins, and the first silently stops existing.

    This is not hypothetical: `service.list_units` once meant "the storage
    cabinets, for the filter dropdown" and was then redefined to mean "the
    individual physical units of an item". Python took the second, the filter
    dropdown went empty, and nothing failed -- an empty dropdown is a
    perfectly valid dropdown. Hence `list_storage_units`, and hence this.
    """
    tree = ast.parse(path.read_text())
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # A @typing.overload or @property/@x.setter pair is a deliberate
        # redefinition; only bare ones are the bug.
        if node.decorator_list:
            continue
        if node.name in seen:
            duplicates.append(
                f"{node.name} (lines {seen[node.name]} and {node.lineno})"
            )
        seen[node.name] = node.lineno

    assert not duplicates, (
        f"{path.name} defines the same top-level name more than once: "
        + "; ".join(duplicates)
    )


def test_the_two_kinds_of_unit_have_separate_functions():
    """"Unit" means two things in this domain, so the names have to differ.

    `item.unit` is a storage cabinet. The `unit` table is one individual
    physical object. Both are legitimate and both are called a unit out loud,
    which is exactly why the functions cannot both be `list_units`.
    """
    from stockroom import service

    assert callable(service.list_storage_units)
    assert callable(service.list_units)
    assert service.list_storage_units is not service.list_units


_TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "stockroom" / "templates"


@pytest.mark.parametrize(
    "template", sorted(p.name for p in _TEMPLATES.glob("*.html")),
)
def test_every_template_compiles(template):
    """Catch a syntax error in a template nobody happened to open.

    `test_web.py` renders the common pages, but not every branch of every
    page -- a broken `{% if %}` inside a block that only appears for staff,
    or only when a list is non-empty, can sit there until someone hits it in
    the stockroom. Compiling is cheap and covers all of them.

    Uses the application's own Jinja environment, because the templates rely
    on filters registered in web/app.py (`datetime`, `hold_class`, ...) and a
    bare Environment would report those as errors.
    """
    from stockroom.web import app as app_module  # registers the filters
    from stockroom.web.deps import templates

    assert app_module  # imported for its side effect on templates.env
    templates.env.get_template(template)
