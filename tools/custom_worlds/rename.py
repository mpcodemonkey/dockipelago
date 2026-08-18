"""Installing custom worlds under a prefixed module name, and fixing up the source that assumed it.

Core keys nothing on a world's folder name - it registers by ``game`` - but the folder name is still
a global: ``worlds/`` is one package, so two worlds cannot share one. Installing every custom world
as ``worlds/cw_<name>/`` keeps them in their own namespace, where they can never shadow a world that
ships with Archipelago.

Renaming a package is not free. Most worlds reach their own modules with relative imports
(``from .Rom import ...``), which do not care what the package is called. A minority spell it out::

    from worlds.dark_souls_3.Bosses import all_bosses

11 of the 82 worlds bundled with Archipelago do this somewhere, so it is a normal thing to write, not
an oddity - and every one of those lines is a ``ModuleNotFoundError`` once the folder moves. So the
imports are rewritten as the world is installed.

Only real import statements are rewritten, located with :mod:`ast` rather than by matching text, so a
docstring that happens to mention ``worlds.something`` is left alone. That cuts both ways: a world
that builds its own module path as a *string* is not something this can safely rewrite, so those are
reported instead of guessed at.

The rename map covers every world being installed, not just the one being written, because custom
worlds occasionally import each other. Core's own names are never in the map, so a world reaching
into ``worlds.alttp`` still reaches core's alttp.
"""

import ast
import re

#: Prefix for the module name every custom world is installed under.
DEFAULT_MODULE_PREFIX = "cw_"

#: Where a world names a module path in a string, which cannot be rewritten safely.
_STRING_REFERENCE = "string-module-path"


def prefixed(module_name: str, prefix: str) -> str:
    """The folder a world is installed into, which is its own name behind ``prefix``."""
    if not prefix or module_name.startswith(prefix):
        return module_name
    return f"{prefix}{module_name}"


def valid_prefix(prefix: str) -> str:
    """Reject a prefix that cannot appear in an import statement, explaining why.

    Returns the empty string when the prefix is usable. A hyphen is the tempting choice here and the
    one that has to be refused: ``worlds/cw-alttp`` imports fine through ``importlib``, but
    ``from worlds.cw-alttp.Items import x`` is a SyntaxError, so the very imports this module exists
    to fix could not be written at all.
    """
    if not prefix:
        return ""
    if not f"{prefix}x".isidentifier():
        return (
            f"module prefix {prefix!r} cannot start a Python identifier, so a world's own "
            f"'from worlds.{prefix}<name> import ...' lines could not be rewritten to match "
            "and the world would fail to load. Use letters, digits and underscores"
        )
    return ""


def rewrite_imports(source: str, renames: dict[str, str]) -> str:
    """Point a world's absolute imports of renamed worlds at their new module names.

    ``renames`` maps an old module name to its new one. Returns the source unchanged when there is
    nothing to do, including when it does not parse - a module we cannot read is one
    :mod:`.webworld` has already refused the world over.
    """
    if not renames:
        return source
    try:
        tree = ast.parse(source.lstrip("﻿"))
    except SyntaxError:
        return source

    offsets = _line_offsets(source)
    edits: list[tuple[int, int, str]] = []
    # "import worlds.mygame.Extra" binds the name "worlds", and every use of it afterwards spells
    # the old module out again as attribute access. Those uses are only rewritten in a module that
    # imports the package this way, which is what makes "worlds" certainly the package and not some
    # local variable that happens to share the name.
    qualified = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module or _target(node.module, renames) is None:
                continue
        elif isinstance(node, ast.Import):
            if not any(_target(alias.name, renames) is not None for alias in node.names):
                continue
            qualified = qualified or any(alias.asname is None for alias in node.names)
        else:
            continue
        edit = _rewrite_statement(source, offsets, node, renames)
        if edit is not None:
            edits.append(edit)

    if qualified:
        edits.extend(_rewrite_attribute_uses(source, offsets, tree, renames))

    if not edits:
        return source
    result = source
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def string_references(source: str, names: dict[str, str]) -> list[str]:
    """String literals naming a module that is being renamed, which cannot be rewritten for them.

    A path assembled as text - ``"worlds.mygame.data"`` handed to ``importlib``, or a resource path
    like ``"ap:worlds/ladx/assets/..."`` - keeps working only by luck. Rewriting every string that
    matches would corrupt docstrings that merely mention the world, so these are reported and left.

    Prose is filtered out by requiring the whole literal to be whitespace-free. A module path or a
    resource path never contains a space; a docstring that mentions the world in passing almost
    always does. That keeps this list short enough to be worth reading.
    """
    try:
        tree = ast.parse(source.lstrip("﻿"))
    except SyntaxError:
        return []
    pattern = re.compile(r"\bworlds[./](" + "|".join(re.escape(name) for name in names) + r")\b")
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        if text.split() == [text] and pattern.search(text):
            found.append(text[:80])
    return found


def _target(module: str, renames: dict[str, str]) -> str | None:
    """The new dotted path for ``worlds.<old>`` or ``worlds.<old>.sub``, or None to leave it alone."""
    if not module.startswith("worlds."):
        return None
    rest = module[len("worlds."):]
    head, _, tail = rest.partition(".")
    new = renames.get(head)
    if new is None:
        return None
    return f"worlds.{new}.{tail}" if tail else f"worlds.{new}"


def _rewrite_statement(
    source: str, offsets: list[int], node: ast.stmt, renames: dict[str, str]
) -> tuple[int, int, str] | None:
    """Replace the module paths inside one import statement, leaving the rest of it untouched.

    Scoped to the statement's own source span, so ``worlds.<name>`` can only be a module path here -
    which is what makes a plain textual substitution safe.
    """
    if node.end_lineno is None or node.end_col_offset is None:
        return None
    start = offsets[node.lineno - 1] + node.col_offset
    end = offsets[node.end_lineno - 1] + node.end_col_offset
    statement = source[start:end]

    rewritten = statement
    for old, new in renames.items():
        rewritten = re.sub(rf"\bworlds\.{re.escape(old)}\b", f"worlds.{new}", rewritten)
    return None if rewritten == statement else (start, end, rewritten)


def _rewrite_attribute_uses(
    source: str, offsets: list[int], tree: ast.Module, renames: dict[str, str]
) -> list[tuple[int, int, str]]:
    """Repoint ``worlds.<old>`` where it is read as an attribute chain rather than imported.

    Only the ``worlds.<old>`` head of the chain is replaced, so ``worlds.mygame.Extra.VALUE`` keeps
    everything after the module name exactly as it was.
    """
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        if node.value.id != "worlds" or node.attr not in renames:
            continue
        if node.end_lineno is None or node.end_col_offset is None:
            continue
        start = offsets[node.value.lineno - 1] + node.value.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        edits.append((start, end, f"worlds.{renames[node.attr]}"))
    return edits


def _line_offsets(source: str) -> list[int]:
    """The offset each line starts at, so an ast (lineno, col) becomes an index into the source."""
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets
