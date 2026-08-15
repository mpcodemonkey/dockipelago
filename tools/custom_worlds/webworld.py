"""Catching worlds whose ``WebWorld`` wiring is broken, without running their code.

Two mistakes are common in community worlds and neither is caught by the manifest checks, because
both are perfectly valid Python that only goes wrong once Archipelago imports the world:

``web = MyGameWeb``
    Assigning the class instead of an instance. ``AutoWorldRegister.__new__`` asserts
    ``isinstance(dct["web"], WebWorld)``, so the world raises
    ``AssertionError: WebWorld has to be instantiated.`` at import and never registers at all.

No ``web`` attribute
    The world registers fine and generation works, but it inherits ``World.web = WebWorld()``, and
    the base ``WebWorld`` declares ``tutorials`` as a bare annotation with no value. The WebHost's
    ``/tutorial/`` page loops over *every* registered world reading ``world_type.web.tutorials``, so
    one world like this raises ``AttributeError`` and takes out that page for the whole site.

The source is read with :mod:`ast` rather than imported, since importing is what we are trying to
avoid doing to unverified third-party code.

Only ``<name>/__init__.py`` is inspected. A world that defines its ``WebWorld`` in another module is
deliberately left alone: ``web = MyGameWeb()`` where ``MyGameWeb`` is imported is reported as fine,
because confirming it would mean resolving imports across the archive. The rule throughout is to
report a problem only when the source proves one, and stay quiet whenever the answer depends on
something this module cannot see.
"""

import ast
from dataclasses import dataclass

#: The world assigns the WebWorld class rather than an instance. Core refuses to load it.
WEB_NOT_INSTANTIATED = "web-not-instantiated"
#: The world instantiates a name that does not exist, so importing it raises NameError.
WEB_UNDEFINED = "web-undefined"
#: The world never sets ``web``, which breaks the WebHost's tutorial page for every game.
WEB_MISSING = "web-missing"
#: The file is not valid Python.
UNPARSEABLE = "unparseable"

_WORLD_BASE = "World"
_WEBWORLD_BASE = "WebWorld"
_MAX_BASE_DEPTH = 10


@dataclass(frozen=True)
class WebWorldFinding:
    """Something wrong with a world's WebWorld wiring."""

    code: str
    detail: str
    #: True when Archipelago refuses to load the world at all, as opposed to loading it broken.
    fatal: bool
    world_class: str = ""

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
                fatal=True,
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
    found, value = module.find_web(name, node)

    if not found or value is None:
        if module.assigns_web_attribute(name):
            return None  # set after the class body, e.g. "MyWorld.web = MyWeb()"
        if not module.inherits_from_outside(name):
            return WebWorldFinding(
                code=WEB_MISSING,
                detail=(
                    f"{name} never sets 'web', so it falls back to the base WebWorld, whose "
                    "'tutorials' attribute does not exist; this breaks the WebHost tutorial page "
                    "for every game on the site"
                ),
                fatal=False,
                world_class=name,
            )
        return None  # a base class we cannot see may well set it

    if isinstance(value, ast.Call):
        # For "web = web_module.MyGameWeb()" the name that has to exist is web_module, not the
        # attribute hanging off it, so resolution follows the chain back to its root.
        root = _root_name_of(value.func)
        if root and module.is_unknown(root):
            callee = _name_of(value.func) or root
            return WebWorldFinding(
                code=WEB_UNDEFINED,
                detail=f"{name} sets web = {callee}(), but {root} is never defined or imported",
                fatal=True,
                world_class=name,
            )
        return None

    if isinstance(value, ast.Name) and value.id in module.classes:
        return WebWorldFinding(
            code=WEB_NOT_INSTANTIATED,
            detail=(
                f"{name} sets web = {value.id} instead of web = {value.id}(); core asserts "
                "'WebWorld has to be instantiated.' and the world will not load"
            ),
            fatal=True,
            world_class=name,
        )

    if isinstance(value, ast.Name) and module.is_unknown(value.id):
        return WebWorldFinding(
            code=WEB_UNDEFINED,
            detail=f"{name} sets web = {value.id}, but {value.id} is never defined or imported",
            fatal=True,
            world_class=name,
        )

    return None


class _ModuleIndex:
    """Everything about a module's top level that the checks need to look up by name."""

    def __init__(self, tree: ast.Module) -> None:
        self.classes: dict[str, ast.ClassDef] = {}
        self.imported: set[str] = set()
        self.assigned: dict[str, ast.expr] = {}
        self.functions: set[str] = set()
        #: Classes given a ``web`` attribute from outside their body, e.g. "MyWorld.web = MyWeb()".
        self.patched_web: set[str] = set()

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
                    if isinstance(target, ast.Name):
                        self.assigned[target.id] = node.value
                    elif isinstance(target, ast.Attribute) and target.attr == "web":
                        owner = _name_of(target.value)
                        if owner:
                            self.patched_web.add(owner)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self.assigned[node.target.id] = node.value

    def is_world(self, name: str) -> bool:
        return self._descends_from(name, _WORLD_BASE)

    def is_webworld(self, name: str) -> bool:
        return self._descends_from(name, _WEBWORLD_BASE)

    def is_unknown(self, name: str) -> bool:
        """True when a name is used but defined nowhere this module can see."""
        return not (
            name in self.classes or name in self.imported or name in self.assigned or name in self.functions
        )

    def assigns_web_attribute(self, name: str) -> bool:
        return name in self.patched_web

    def inherits_from_outside(self, name: str, depth: int = 0) -> bool:
        """True when any base class is not defined in this file, so it could supply ``web``."""
        node = self.classes.get(name)
        if node is None or depth > _MAX_BASE_DEPTH:
            return True
        for base in node.bases:
            base_name = _name_of(base)
            if base_name is None:
                return True  # something dynamic; assume it might provide web
            if base_name in (_WORLD_BASE, _WEBWORLD_BASE):
                continue  # core's bases, which we know do not usefully set web
            if base_name not in self.classes:
                return True
            if self.inherits_from_outside(base_name, depth + 1):
                return True
        return False

    def find_web(self, name: str, node: ast.ClassDef, depth: int = 0) -> tuple[bool, ast.expr | None]:
        """Look for a ``web`` assignment in this class, then in its local base classes."""
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id == "web":
                        return True, statement.value
            elif isinstance(statement, ast.AnnAssign):
                if isinstance(statement.target, ast.Name) and statement.target.id == "web":
                    # A bare "web: WebWorld" annotation declares nothing, so treat it as unset.
                    return statement.value is not None, statement.value

        if depth <= _MAX_BASE_DEPTH:
            for base in node.bases:
                base_name = _name_of(base)
                base_node = self.classes.get(base_name) if base_name else None
                if base_node is not None:
                    found, value = self.find_web(base_name or "", base_node, depth + 1)
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
