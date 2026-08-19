"""Giving a world the WebWorld wiring it is missing, instead of turning it away.

Three of the things :mod:`.webworld` refuses a world for are not really faults in the game logic at
all - they are paperwork the WebHost insists on:

``docs-missing``
    no ``docs/`` folder for ``copy_tutorials_files_to_static()`` to list.
``web-no-tutorials``
    a ``WebWorld`` that never declares a ``tutorials`` list.
``web-missing``
    no ``web`` attribute at all, so the world inherits the base ``WebWorld`` and gets dropped.

Every one of those is something we can simply write. The world generates perfectly well; it is being
turned away over a setup guide it never shipped. So ``--repair`` writes the missing pieces and lets
the world in.

This edits third-party source, which is worth being deliberate about, so two rules apply throughout:

*Only ever add.* Edits are line insertions located with :mod:`ast`, never rewrites of existing text,
so a world's own code is returned byte for byte and only new lines appear between it. Nothing is
reformatted and nothing is deleted.

*Never trust the result.* A repair is a proposal. The caller re-verifies the repaired world from
scratch, and a world that still fails is refused exactly as it would have been - see
:func:`repair_apworld`, which hands back a new file rather than touching the original.
"""

import ast
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .webworld import (
    DOCS_MISSING,
    WEB_MISSING,
    WEB_NO_TUTORIALS,
    WebWorldFinding,
    module_path,
)

logger = logging.getLogger(__name__)

#: Findings ``--repair`` knows how to write its way out of.
REPAIRABLE = frozenset({DOCS_MISSING, WEB_NO_TUTORIALS, WEB_MISSING})

#: The class a world gets when it has no WebWorld of its own.
GENERATED_WEB_CLASS = "CWWeb"

#: The setup guide the generated tutorial points at, and the file created to back it.
SETUP_DOC = "setup_en.md"
DOCS_FOLDER = "docs"

_TUTORIAL_AUTHORS = ("ubufugu",)

_IMPORTS = (
    ("worlds.AutoWorld", "WebWorld"),
    ("BaseClasses", "Tutorial"),
)


def _tutorial_block(indent: str) -> str:
    """The generic tutorial, indented to sit inside a class body."""
    authors = ", ".join(f'"{author}"' for author in _TUTORIAL_AUTHORS)
    return (
        f"{indent}setup_en = Tutorial(\n"
        f'{indent}    "Multiworld Setup Guide",\n'
        f'{indent}    "A guide to playing this game with Archipelago.",\n'
        f'{indent}    "English",\n'
        f'{indent}    "{SETUP_DOC}",\n'
        f'{indent}    "setup/en",\n'
        f"{indent}    [{authors}]\n"
        f"{indent})\n"
        f"\n"
        f"{indent}tutorials = [setup_en]\n"
    )


@dataclass
class RepairPlan:
    """What a repair would change, before any of it is written."""

    #: Module path -> new source.
    modules: dict[str, str] = field(default_factory=dict)
    #: Archive path -> contents, for files the world does not have yet.
    files: dict[str, bytes] = field(default_factory=dict)
    #: Codes repaired, for the run report and the lockfile.
    repaired: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.modules or self.files)


def repairable(findings: list[WebWorldFinding]) -> bool:
    """Whether every finding that would refuse this world is one a repair can answer.

    A world with anything else wrong is left alone: repairing half of it would install something
    still broken, which is worse than a clear refusal.
    """
    blocking = [finding for finding in findings if finding.code not in _IGNORABLE]
    return bool(blocking) and all(finding.code in REPAIRABLE for finding in blocking)


#: Findings that never refuse a world, so they do not stand in the way of repairing one.
_IGNORABLE = frozenset({"bad-escape"})


def plan_repair(
    modules: dict[str, str], files: set[str], findings: list[WebWorldFinding]
) -> RepairPlan:
    """Work out the edits that would answer ``findings``, without applying any of them."""
    plan = RepairPlan()
    edited = dict(modules)

    for finding in findings:
        if finding.code == WEB_NO_TUTORIALS:
            source = edited.get(finding.module)
            if source is None:
                continue
            updated = _add_tutorials(source, finding.target_class)
            if updated is not None:
                edited[finding.module] = updated
                plan.repaired.append(WEB_NO_TUTORIALS)
        elif finding.code == WEB_MISSING:
            source = edited.get(finding.module)
            if source is None:
                continue
            updated = _add_web_world(source, finding.target_class)
            if updated is not None:
                edited[finding.module] = updated
                plan.repaired.append(WEB_MISSING)

    plan.modules = {path: source for path, source in edited.items() if source != modules.get(path)}

    # A tutorial naming setup_en.md is only half an answer if the file is not there, so the docs are
    # written whenever anything was repaired - not only when docs/ was the finding.
    wants_docs = plan.repaired or any(finding.code == DOCS_MISSING for finding in findings)
    if wants_docs and f"{DOCS_FOLDER}/{SETUP_DOC}" not in files:
        plan.files[f"{DOCS_FOLDER}/{SETUP_DOC}"] = b""
        # Recorded whenever the file is written, not only when docs/ was the reported finding: a
        # world repaired for its WebWorld never reached the docs check, and the lockfile should
        # still say the guide came from here rather than from the world.
        plan.repaired.append(DOCS_MISSING)

    return plan


def repair_apworld(source: Path, destination: Path, findings: list[WebWorldFinding]) -> list[str]:
    """Write a repaired copy of an apworld, and report which findings it answered.

    The original is never modified: a repair that turns out not to work should cost nothing, and the
    caller decides what to keep after re-verifying the copy.
    """
    prefix = f"{source.stem}/"
    with zipfile.ZipFile(source) as archive:
        members = [info for info in archive.infolist() if info.filename.startswith(prefix)]
        modules: dict[str, str] = {}
        files: set[str] = set()
        for info in members:
            relative = info.filename[len(prefix):]
            if not relative:
                continue
            files.add(relative.rstrip("/"))
            dotted = module_path(relative)
            if dotted is not None and not info.is_dir():
                modules[dotted] = archive.read(info).decode("utf-8", errors="replace")

        plan = plan_repair(modules, files, findings)
        if not plan:
            return []

        rewritten = {_module_file(path, files): source_text for path, source_text in plan.modules.items()}
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out:
            for info in members:
                relative = info.filename[len(prefix):]
                if not relative or info.is_dir():
                    continue
                if relative in rewritten:
                    out.writestr(f"{prefix}{relative}", rewritten[relative].encode("utf-8"))
                else:
                    out.writestr(f"{prefix}{relative}", archive.read(info))
            for relative, content in plan.files.items():
                out.writestr(f"{prefix}{relative}", content)

    return plan.repaired


def _module_file(dotted: str, files: set[str]) -> str:
    """The archive path a dotted module came from - ``web`` is ``web.py`` or ``web/__init__.py``."""
    flat = f"{dotted.replace('.', '/')}.py" if dotted else "__init__.py"
    if flat in files:
        return flat
    package = f"{dotted.replace('.', '/')}/__init__.py" if dotted else "__init__.py"
    return package if package in files else flat


# -- the edits ---------------------------------------------------------------------------


def _add_tutorials(source: str, class_name: str) -> str | None:
    """Insert the generic tutorial block at the top of a class body."""
    node = _class_named(source, class_name)
    if node is None:
        return None
    line, indent = _body_insertion_point(node, source)
    edited = _insert_lines(source, line, _tutorial_block(indent) + "\n")
    return _with_imports(edited)


def _add_web_world(source: str, world_class: str) -> str | None:
    """Give a world a WebWorld: define one above it, and point ``web`` at an instance of it.

    The assignment goes in as the first statement of the class body, after the docstring where there
    is one, so it reads the way a world would have written it.
    """
    node = _class_named(source, world_class)
    if node is None:
        return None

    line, indent = _body_insertion_point(node, source)
    edited = _insert_lines(source, line, f"{indent}web = {GENERATED_WEB_CLASS}()\n")

    # Re-parse: the class moved down by however many lines the assignment added.
    moved = _class_named(edited, world_class)
    if moved is None:
        return None
    definition = (
        f"class {GENERATED_WEB_CLASS}(WebWorld):\n"
        f'    """Generated so the WebHost will serve this world."""\n\n'
        f"{_tutorial_block('    ')}"
        f"\n\n"
    )
    edited = _insert_lines(edited, _statement_start(moved), definition)
    return _with_imports(edited)


def _with_imports(source: str) -> str:
    """Make sure ``WebWorld`` and ``Tutorial`` are importable in this module."""
    missing = [(module, name) for module, name in _IMPORTS if not _binds(source, name)]
    if not missing:
        return source
    lines = "".join(f"from {module} import {name}\n" for module, name in missing)
    return _insert_lines(source, _import_insertion_point(source), lines)


def _binds(source: str, name: str) -> bool:
    """Whether a module-level name already exists, however it got there."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True  # cannot tell, so do not add an import that might duplicate one
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if any((alias.asname or alias.name) == name for alias in node.names):
                return True
            if any(alias.name == "*" for alias in node.names):
                return True  # a star import may well be supplying it
        elif isinstance(node, ast.Import):
            if any((alias.asname or alias.name.split(".")[0]) == name for alias in node.names):
                return True
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return True
    return False


# -- locating where to write ------------------------------------------------------------


def _class_named(source: str, name: str) -> ast.ClassDef | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _statement_start(node: ast.stmt) -> int:
    """The first line a statement occupies, decorators included.

    ``lineno`` on a decorated definition points at the ``def`` or ``class``, so inserting there
    would land between a decorator and the thing it decorates.
    """
    decorators: list[ast.expr] = getattr(node, "decorator_list", [])
    return min([node.lineno, *(decorator.lineno for decorator in decorators)])


def _body_insertion_point(node: ast.ClassDef, source: str) -> tuple[int, str]:
    """Where the first statement of a class body should go, and at what indent.

    After the docstring if there is one, which is what makes the result read like the world wrote it
    rather than like something was stapled on top.
    """
    first = node.body[0]
    docstring = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )
    if docstring and len(node.body) > 1:
        following = node.body[1]
        return _statement_start(following), " " * following.col_offset
    if docstring:
        return (first.end_lineno or first.lineno) + 1, " " * first.col_offset
    return _statement_start(first), " " * first.col_offset


def _import_insertion_point(source: str) -> int:
    """Below the module's existing imports, or below its docstring when it has none."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 1
    line = 1
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            line = (node.end_lineno or node.lineno) + 1
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and line == 1:
            line = (node.end_lineno or node.lineno) + 1
        elif not isinstance(node, (ast.Import, ast.ImportFrom)):
            break
    return line


def _insert_lines(source: str, line: int, text: str) -> str:
    """Insert ``text`` before 1-based ``line``, leaving every existing line untouched."""
    lines = source.splitlines(keepends=True)
    index = max(0, min(len(lines), line - 1))
    if lines and index and not lines[index - 1].endswith("\n"):
        lines[index - 1] += "\n"
    return "".join(lines[:index]) + text + "".join(lines[index:])
