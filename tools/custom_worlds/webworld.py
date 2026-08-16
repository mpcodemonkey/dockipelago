"""Catching worlds whose ``WebWorld`` wiring is broken, without running their code.

A world can satisfy every manifest check and still be useless here, because the manifest says
nothing about the code. Problems come in two severities, both confirmed against this checkout by
loading a world with the mistake in it:

``load`` - Archipelago refuses to load the world at all
    ``web = MyGameWeb`` assigns the class instead of an instance, and
    ``AutoWorldRegister.__new__`` asserts ``isinstance(dct["web"], WebWorld)``, so importing raises
    ``AssertionError: WebWorld has to be instantiated.``. Instantiating a name that does not exist
    raises ``NameError``. Source that does not parse raises ``SyntaxError``, which is worse than it
    sounds: it aborts ``import worlds`` outright rather than being caught per-world.

``webhost`` - the world loads, but the WebHost drops it
    ``WebHost.py`` filters its world list down with ``hasattr(world.web, "tutorials")`` and logs
    ``Following worlds not loaded as they are invalid for WebHost: {...}``. A world that fails it is
    removed from both ``AutoWorldRegister.world_types`` and the network data package, so it does not
    appear on the site at all. Two shapes fail: a world that never sets ``web`` (inheriting
    ``World.web = WebWorld()``, whose ``tutorials`` is a bare annotation with no value), and a
    ``WebWorld`` subclass that never sets ``tutorials``.

The source is read with :mod:`ast` rather than imported, since importing is what we are trying to
avoid doing to unverified third-party code. Parsing also settles the "is it commented out?" question
for free: commented code is not in the tree, so a ``tutorials`` block behind a ``#`` reads exactly
like one that was never written.

Only ``<name>/__init__.py`` is inspected. A world that defines its ``WebWorld`` in another module is
deliberately left alone, because confirming it would mean resolving imports across the archive. The
rule throughout is to report a problem only when the source proves one, and stay quiet whenever the
answer depends on something this module cannot see.
"""

import ast
from dataclasses import dataclass

#: Archipelago will not load the world at all.
SEVERITY_LOAD = "load"
#: The world loads, but ``WebHost.py`` drops it from the site and the data package.
SEVERITY_WEBHOST = "webhost"

#: The world assigns the WebWorld class rather than an instance.
WEB_NOT_INSTANTIATED = "web-not-instantiated"
#: The world instantiates a name that does not exist, so importing it raises NameError.
WEB_UNDEFINED = "web-undefined"
#: The file is not valid Python.
UNPARSEABLE = "unparseable"
#: The world never sets ``web``, so it inherits a base WebWorld with no ``tutorials``.
WEB_MISSING = "web-missing"
#: The world's WebWorld never sets ``tutorials``.
WEB_NO_TUTORIALS = "web-no-tutorials"
#: ``tutorials`` is set to something the WebHost cannot iterate over.
TUTORIALS_NOT_A_LIST = "tutorials-not-a-list"

_WORLD_BASE = "World"
_WEBWORLD_BASE = "WebWorld"
_TUTORIALS = "tutorials"
_WEB = "web"
_MAX_BASE_DEPTH = 10

_DROPPED_BY_WEBHOST = (
    "so WebHost.py drops it with 'Following worlds not loaded as they are invalid for WebHost' "
    "and the game never appears on the site"
)


@dataclass(frozen=True)
class WebWorldFinding:
    """Something wrong with a world's WebWorld wiring."""

    code: str
    detail: str
    severity: str
    world_class: str = ""

    @property
    def blocks_loading(self) -> bool:
        """True when Archipelago refuses to load the world, as opposed to the WebHost dropping it."""
        return self.severity == SEVERITY_LOAD

    def __str__(self) -> str:
        return self.detail


def inspect_source(source: str, *, module_name: str = "") -> list[WebWorldFinding]:
    """Report WebWorld problems provable from the text of a world's ``__init__.py``."""
    label = module_name or "the world"
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [
            WebWorldFinding(
                code=UNPARSEABLE,
                detail=f"{label}/__init__.py is not valid Python: line {error.lineno}: {error.msg}",
                severity=SEVERITY_LOAD,
            )
        ]

    module = _ModuleIndex(tree)
    findings: list[WebWorldFinding] = []
    for name, node in module.classes.items():
        if not module.is_world(name):
            continue
        finding = _check_world(name, node, module)
        if finding is not None:
            findings.append(finding)
    return findings


def _check_world(name: str, node: ast.ClassDef, module: "_ModuleIndex") -> WebWorldFinding | None:
    found, value = module.find_attribute(name, node, _WEB)

    if not found or value is None:
        return _check_missing_web(name, module)

    if isinstance(value, ast.Call):
        root = _root_name_of(value.func)
        if root and module.is_unknown(root):
            callee = _name_of(value.func) or root
            return WebWorldFinding(
                code=WEB_UNDEFINED,
                detail=f"{name} sets web = {callee}(), but {root} is never defined or imported",
                severity=SEVERITY_LOAD,
                world_class=name,
            )
        # The WebWorld is only checkable when its class lives in this file.
        web_name = _name_of(value.func)
        webworld = module.classes.get(web_name) if web_name else None
        if webworld is not None and web_name is not None:
            return _check_tutorials(name, web_name, webworld, module)
        return None

    if isinstance(value, ast.Name) and value.id in module.classes:
        return WebWorldFinding(
            code=WEB_NOT_INSTANTIATED,
            detail=(
                f"{name} sets web = {value.id} instead of web = {value.id}(); core asserts "
                "'WebWorld has to be instantiated.' and the world will not load"
            ),
            severity=SEVERITY_LOAD,
            world_class=name,
        )

    if isinstance(value, ast.Name) and module.is_unknown(value.id):
        return WebWorldFinding(
            code=WEB_UNDEFINED,
            detail=f"{name} sets web = {value.id}, but {value.id} is never defined or imported",
            severity=SEVERITY_LOAD,
            world_class=name,
        )

    return None


def _check_missing_web(name: str, module: "_ModuleIndex") -> WebWorldFinding | None:
    if module.patches_attribute(name, _WEB) or module.inherits_from_outside(name):
        return None
    return WebWorldFinding(
        code=WEB_MISSING,
        detail=(
            f"{name} never sets 'web', so it inherits the base WebWorld, which has no 'tutorials', "
            f"{_DROPPED_BY_WEBHOST}"
        ),
        severity=SEVERITY_WEBHOST,
        world_class=name,
    )


def _check_tutorials(
    world_name: str, web_name: str, webworld: ast.ClassDef, module: "_ModuleIndex"
) -> WebWorldFinding | None:
    """A WebWorld is only valid for the WebHost once it carries a ``tutorials`` list."""
    found, value = module.find_attribute(web_name, webworld, _TUTORIALS)

    if not found or value is None:
        if module.patches_attribute(web_name, _TUTORIALS) or module.inherits_from_outside(web_name):
            return None
        return WebWorldFinding(
            code=WEB_NO_TUTORIALS,
            detail=(
                f"{web_name} defines no 'tutorials', {_DROPPED_BY_WEBHOST}; it needs a Tutorial "
                "block, e.g. 'tutorials = [Tutorial(tutorial_name=\"Setup Guide\", ...)]'"
            ),
            severity=SEVERITY_WEBHOST,
            world_class=world_name,
        )

    # Anything else is only worth flagging when it is provably not a list. A call is usually a
    # factory returning one, so only a bare Tutorial(...) - the missing-brackets mistake - counts.
    resolved = module.resolve(value)
    if _is_lone_tutorial(resolved) or isinstance(resolved, ast.Constant):
        return WebWorldFinding(
            code=TUTORIALS_NOT_A_LIST,
            detail=(
                f"{web_name} sets 'tutorials' to a single value rather than a list; the WebHost "
                "iterates it, so it needs to be a list such as 'tutorials = [setup]'"
            ),
            severity=SEVERITY_WEBHOST,
            world_class=world_name,
        )
    return None


def _is_lone_tutorial(node: ast.expr) -> bool:
    """``tutorials = Tutorial(...)`` - the same thing as the list form, minus the brackets."""
    if not isinstance(node, ast.Call):
        return False
    callee = _name_of(node.func) or ""
    return callee == "Tutorial" or callee.endswith("Tutorial")


class _ModuleIndex:
    """Everything about a module's top level that the checks need to look up by name."""

    def __init__(self, tree: ast.Module) -> None:
        self.classes: dict[str, ast.ClassDef] = {}
        self.imported: set[str] = set()
        self.assigned: dict[str, ast.expr] = {}
        self.functions: set[str] = set()
        #: Attributes given to a class from outside its body, e.g. "MyWorld.web = MyWeb()".
        self.patched: dict[str, set[str]] = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self.classes.setdefault(node.name, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.add(node.name)
            elif isinstance(node, ast.Import):
                self.imported.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.imported.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self._record_target(target, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self._record_target(node.target, node.value)

    def _record_target(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.assigned[target.id] = value
        elif isinstance(target, ast.Attribute):
            owner = _name_of(target.value)
            if owner:
                self.patched.setdefault(owner, set()).add(target.attr)

    def is_world(self, name: str) -> bool:
        return self._descends_from(name, _WORLD_BASE)

    def is_webworld(self, name: str) -> bool:
        return self._descends_from(name, _WEBWORLD_BASE)

    def is_unknown(self, name: str) -> bool:
        """True when a name is used but defined nowhere this module can see."""
        return not (
            name in self.classes or name in self.imported or name in self.assigned or name in self.functions
        )

    def patches_attribute(self, class_name: str, attribute: str) -> bool:
        return attribute in self.patched.get(class_name, ())

    def resolve(self, value: ast.expr) -> ast.expr:
        """Follow a bare name back to what it was assigned at module level, if anything."""
        if isinstance(value, ast.Name) and value.id in self.assigned:
            return self.assigned[value.id]
        return value

    def inherits_from_outside(self, name: str, depth: int = 0) -> bool:
        """True when any base class is not defined in this file, so it could supply the attribute."""
        node = self.classes.get(name)
        if node is None or depth > _MAX_BASE_DEPTH:
            return True
        for base in node.bases:
            base_name = _name_of(base)
            if base_name is None:
                return True  # something dynamic; assume it might provide the attribute
            if base_name in (_WORLD_BASE, _WEBWORLD_BASE):
                continue  # core's bases, which define neither web nor tutorials usefully
            if base_name not in self.classes:
                return True
            if self.inherits_from_outside(base_name, depth + 1):
                return True
        return False

    def find_attribute(
        self, name: str, node: ast.ClassDef, attribute: str, depth: int = 0
    ) -> tuple[bool, ast.expr | None]:
        """Look for a class attribute here, then in the base classes defined in this file."""
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id == attribute:
                        return True, statement.value
            elif isinstance(statement, ast.AnnAssign):
                if isinstance(statement.target, ast.Name) and statement.target.id == attribute:
                    # A bare "tutorials: List[Tutorial]" annotation declares nothing at runtime,
                    # which is exactly why the base WebWorld fails the WebHost's hasattr check.
                    return statement.value is not None, statement.value

        if depth <= _MAX_BASE_DEPTH:
            for base in node.bases:
                base_name = _name_of(base)
                base_node = self.classes.get(base_name) if base_name else None
                if base_node is not None:
                    found, value = self.find_attribute(base_name or "", base_node, attribute, depth + 1)
                    if found:
                        return found, value
        return False, None

    def _descends_from(self, name: str, ancestor: str, depth: int = 0) -> bool:
        node = self.classes.get(name)
        if node is None or depth > _MAX_BASE_DEPTH:
            return False
        for base in node.bases:
            base_name = _name_of(base)
            if base_name == ancestor:
                return True
            if base_name and base_name in self.classes and self._descends_from(base_name, ancestor, depth + 1):
                return True
        return False


def _name_of(node: ast.expr) -> str | None:
    """The trailing name of ``Foo`` or ``pkg.mod.Foo``, or None for anything more dynamic."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _root_name_of(node: ast.expr) -> str | None:
    """The leading name of ``Foo`` or ``pkg.mod.Foo`` - the one that has to be in scope."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None
