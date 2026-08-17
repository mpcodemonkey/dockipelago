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

The whole world is inspected, not just its ``__init__.py``. Custom worlds routinely split the
``World`` and ``WebWorld`` classes across modules, and an analysis of one file cannot see a
``tutorials`` list that lives in ``web.py`` - or the lack of one. Since an apworld carries its
entire package, relative imports are followed inside it: modules reachable from ``__init__`` are
scanned for ``World`` subclasses, and names are resolved across those modules.

Everything is read with :mod:`ast` rather than imported, since importing is what we are trying to
avoid doing to unverified third-party code. Parsing also settles the "is it commented out?" question
for free: commented code is not in the tree, so a ``tutorials`` block behind a ``#`` reads exactly
like one that was never written.

Only modules the world actually reaches are considered, and only problems the source proves are
reported. A name imported from outside the world, a base class in another package, a ``tutorials``
list built by a function - all of these stay quiet, because a false positive costs a game.
"""

import ast
from collections.abc import Iterator, Mapping
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

ROOT = ""  # the world package itself, i.e. <name>/__init__.py

_WORLD_BASE = "World"
_WEBWORLD_BASE = "WebWorld"
_TUTORIALS = "tutorials"
_WEB = "web"
_MAX_DEPTH = 10

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


def module_path(file_name: str) -> str | None:
    """Turn a path inside the world folder into a dotted module path.

    ``__init__.py`` is the package root and becomes ``""``; ``web.py`` becomes ``"web"``;
    ``sub/__init__.py`` becomes ``"sub"``. Anything that is not Python is not a module.
    """
    if not file_name.endswith(".py"):
        return None
    stem = file_name[: -len(".py")]
    parts = [part for part in stem.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def inspect_source(source: str, *, module_name: str = "") -> list[WebWorldFinding]:
    """Inspect a world consisting of a single ``__init__.py``."""
    return inspect_world({ROOT: source}, module_name=module_name)


def inspect_world(modules: Mapping[str, str], *, module_name: str = "") -> list[WebWorldFinding]:
    """Report WebWorld problems provable from a world's source.

    ``modules`` maps dotted module paths - as produced by :func:`module_path` - to their source.
    """
    label = module_name or "the world"
    package = _Package(modules)

    if ROOT not in modules:
        return []
    root = package.index(ROOT)
    if root is None:
        error = package.errors[ROOT]
        return [
            WebWorldFinding(
                code=UNPARSEABLE,
                detail=f"{label}/__init__.py is not valid Python: line {error.lineno}: {error.msg}",
                severity=SEVERITY_LOAD,
            )
        ]

    findings: list[WebWorldFinding] = []
    for path, name, node in package.world_classes():
        finding = _check_world(path, name, node, package)
        if finding is not None:
            findings.append(finding)
    return findings


def _check_world(path: str, name: str, node: ast.ClassDef, package: "_Package") -> WebWorldFinding | None:
    found = package.find_attribute(path, node, _WEB)

    if found is None or found.value is None:
        return _check_missing_web(path, name, node, package)

    value, owner = found.value, found.module
    if isinstance(value, ast.Call):
        root_name = _root_name_of(value.func)
        if root_name and package.is_unknown(owner, root_name):
            callee = _name_of(value.func) or root_name
            return WebWorldFinding(
                code=WEB_UNDEFINED,
                detail=f"{name} sets web = {callee}(), but {root_name} is never defined or imported",
                severity=SEVERITY_LOAD,
                world_class=name,
            )
        callee = _name_of(value.func) or ""
        webworld = package.class_def(owner, callee)
        if webworld is not None:
            return _check_tutorials(name, webworld, package)
        if callee == _WEBWORLD_BASE:
            # web = WebWorld() instantiates core's base directly, and that is the very class whose
            # bare "tutorials" annotation the WebHost's hasattr check exists to reject.
            return WebWorldFinding(
                code=WEB_NO_TUTORIALS,
                detail=(
                    f"{name} sets web = WebWorld(), which is core's base class and defines no "
                    f"'tutorials', {_DROPPED_BY_WEBHOST}; it needs a WebWorld subclass carrying a "
                    "Tutorial block"
                ),
                severity=SEVERITY_WEBHOST,
                world_class=name,
            )
        return None

    if isinstance(value, ast.Name):
        if package.class_def(owner, value.id) is not None and value.id not in package.instances(owner):
            return WebWorldFinding(
                code=WEB_NOT_INSTANTIATED,
                detail=(
                    f"{name} sets web = {value.id} instead of web = {value.id}(); core asserts "
                    "'WebWorld has to be instantiated.' and the world will not load"
                ),
                severity=SEVERITY_LOAD,
                world_class=name,
            )
        if package.is_unknown(owner, value.id):
            return WebWorldFinding(
                code=WEB_UNDEFINED,
                detail=f"{name} sets web = {value.id}, but {value.id} is never defined or imported",
                severity=SEVERITY_LOAD,
                world_class=name,
            )

    return None


def _check_missing_web(
    path: str, name: str, node: ast.ClassDef, package: "_Package"
) -> WebWorldFinding | None:
    if package.patches_attribute(path, name, _WEB) or package.inherits_from_outside(path, node):
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


def _check_tutorials(world_name: str, webworld: "_Class", package: "_Package") -> WebWorldFinding | None:
    """A WebWorld is only valid for the WebHost once it carries a ``tutorials`` list."""
    found = package.find_attribute(webworld.module, webworld.node, _TUTORIALS)
    web_name = webworld.name

    if found is None or found.value is None:
        # "self.tutorials = ..." in __init__ satisfies the WebHost too: its check is hasattr on the
        # instance held in World.web, not on the class.
        if package.sets_instance_attribute(webworld.module, webworld.node, _TUTORIALS):
            return None
        if package.patches_attribute(webworld.module, web_name, _TUTORIALS) or package.inherits_from_outside(
            webworld.module, webworld.node
        ):
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
    resolved = package.resolve(found.module, found.value)
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


# --------------------------------------------------------------------------------------
# Reading the world's modules
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Class:
    """A class definition, and the module it was found in."""

    module: str
    name: str
    node: ast.ClassDef


@dataclass(frozen=True)
class _Attribute:
    """A class attribute's value, and the module whose scope that value should be read in."""

    module: str
    value: ast.expr | None


@dataclass(frozen=True)
class _Import:
    """A name brought into a module from elsewhere."""

    module: str | None  # the module inside this world it came from, or None if from outside
    name: str  # the original name there


class _ModuleIndex:
    """Everything about one module's top level that the checks need to look up by name."""

    def __init__(self, path: str, tree: ast.Module, package: "_Package") -> None:
        self.path = path
        self.classes: dict[str, ast.ClassDef] = {}
        self.imports: dict[str, _Import] = {}
        self.star_imports: list[str | None] = []
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
                for alias in node.names:
                    self.imports[alias.asname or alias.name.split(".")[0]] = _Import(None, alias.name)
            elif isinstance(node, ast.ImportFrom):
                self._record_import_from(node, package)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self._record_target(target, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self._record_target(node.target, node.value)

    def _record_import_from(self, node: ast.ImportFrom, package: "_Package") -> None:
        origin = package.resolve_module(self.path, node.level, node.module)
        for alias in node.names:
            if alias.name == "*":
                self.star_imports.append(origin)
                continue
            local = alias.asname or alias.name
            # "from . import web" names a submodule, not a member of the package.
            submodule = package.join(origin, alias.name) if origin is not None else None
            if submodule is not None and package.has(submodule):
                self.imports[local] = _Import(submodule, "")
            else:
                self.imports[local] = _Import(origin, alias.name)

    def _record_target(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.assigned[target.id] = value
        elif isinstance(target, ast.Attribute):
            owner = _name_of(target.value)
            if owner:
                self.patched.setdefault(owner, set()).add(target.attr)


class _Package:
    """The modules of one world, parsed on demand and resolved against each other."""

    def __init__(self, sources: Mapping[str, str]) -> None:
        self.sources = sources
        self.errors: dict[str, SyntaxError] = {}
        self._indexes: dict[str, _ModuleIndex | None] = {}

    # -- module plumbing ---------------------------------------------------------------

    def has(self, path: str) -> bool:
        return path in self.sources

    def index(self, path: str) -> _ModuleIndex | None:
        """Parse a module, caching both successes and failures. None means it did not parse."""
        if path in self._indexes:
            return self._indexes[path]
        self._indexes[path] = None
        source = self.sources.get(path)
        if source is None:
            return None
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            self.errors[path] = error
            return None
        index = _ModuleIndex(path, tree, self)
        self._indexes[path] = index
        return index

    @staticmethod
    def join(package: str, name: str) -> str:
        return f"{package}.{name}" if package else name

    def resolve_module(self, current: str, level: int, module: str | None) -> str | None:
        """Where an import in ``current`` points, as a module path inside this world.

        None means outside the world, which is as far as the analysis can see.
        """
        if level == 0:
            # Absolute: only "worlds.<name>.x" reaches back into a world, and the world's own name
            # is not known here, so treat every absolute import as external.
            return None
        parts = current.split(".") if current else []
        if self.is_package(current):
            container = parts
        else:
            container = parts[:-1]
        climb = level - 1
        if climb > len(container):
            return None
        base = container[: len(container) - climb] if climb else container
        target = list(base) + (module.split(".") if module else [])
        return ".".join(target)

    def is_package(self, path: str) -> bool:
        """A module is a package when the archive holds it as ``<path>/__init__.py``."""
        return path == ROOT or any(other.startswith(f"{path}.") for other in self.sources)

    def reachable(self) -> list[str]:
        """Module paths reachable from ``__init__`` by following this world's own imports.

        A module the world never imports cannot register a class, so anything found there would be
        dead code rather than a problem worth reporting.
        """
        seen = [ROOT]
        queue = [ROOT]
        while queue:
            current = queue.pop(0)
            index = self.index(current)
            if index is None:
                continue
            targets = list(self.star_targets(index))
            targets.extend(entry.module for entry in index.imports.values() if entry.module is not None)
            for target in targets:
                if self.has(target) and target not in seen:
                    seen.append(target)
                    queue.append(target)
        return seen

    @staticmethod
    def star_targets(index: _ModuleIndex) -> Iterator[str]:
        for origin in index.star_imports:
            if origin is not None:
                yield origin

    def world_classes(self) -> Iterator[tuple[str, str, ast.ClassDef]]:
        """Every ``World`` subclass in the modules this world reaches."""
        for path in self.reachable():
            index = self.index(path)
            if index is None:
                continue
            for name, node in index.classes.items():
                if self.descends_from(path, node, _WORLD_BASE):
                    yield path, name, node

    # -- name resolution ---------------------------------------------------------------

    def class_def(self, module: str, name: str, depth: int = 0) -> _Class | None:
        """The class ``name`` refers to in ``module``, following imports within this world."""
        if not name or depth > _MAX_DEPTH:
            return None
        index = self.index(module)
        if index is None:
            return None
        node = index.classes.get(name)
        if node is not None:
            return _Class(module, name, node)

        entry = index.imports.get(name)
        if entry is not None and entry.module is not None and entry.name:
            return self.class_def(entry.module, entry.name, depth + 1)

        for origin in self.star_targets(index):
            found = self.class_def(origin, name, depth + 1)
            if found is not None:
                return found
        return None

    def instances(self, module: str) -> set[str]:
        """Module-level names bound to a call, which are instances rather than classes."""
        index = self.index(module)
        if index is None:
            return set()
        return {name for name, value in index.assigned.items() if isinstance(value, ast.Call)}

    def is_unknown(self, module: str, name: str) -> bool:
        """True when a name is used but defined nowhere the analysis can see."""
        index = self.index(module)
        if index is None:
            return False
        if name in index.classes or name in index.imports or name in index.assigned or name in index.functions:
            return False
        for origin in index.star_imports:
            # A star import could be supplying the name. Only a source this analysis can read, and
            # which turns out not to define it, leaves the name genuinely unaccounted for.
            if origin is None or not self.is_unknown(origin, name):
                return False
        return True

    def sets_instance_attribute(self, module: str, node: ast.ClassDef, attribute: str, depth: int = 0) -> bool:
        """Whether any method of the class assigns ``self.<attribute>``.

        The WebHost asks ``hasattr(world.web, "tutorials")`` of the *instance*, so a tutorials list
        built in ``__init__`` counts just as much as one declared in the class body.
        """
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(statement):
                targets: list[ast.expr] = []
                if isinstance(inner, ast.Assign):
                    targets = list(inner.targets)
                elif isinstance(inner, (ast.AnnAssign, ast.AugAssign)):
                    targets = [inner.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == attribute
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        return True

        if depth <= _MAX_DEPTH:
            for _name, resolved in self.bases(module, node):
                if resolved is not None and self.sets_instance_attribute(
                    resolved.module, resolved.node, attribute, depth + 1
                ):
                    return True
        return False

    def patches_attribute(self, module: str, class_name: str, attribute: str) -> bool:
        """Whether any reachable module assigns ``<class>.<attribute>`` from outside the class body."""
        for path in {module, *self.reachable()}:
            index = self.index(path)
            if index is not None and attribute in index.patched.get(class_name, ()):
                return True
        return False

    def resolve(self, module: str, value: ast.expr) -> ast.expr:
        """Follow a bare name back to what it was assigned at module level, if anything."""
        if isinstance(value, ast.Name):
            index = self.index(module)
            if index is not None and value.id in index.assigned:
                return index.assigned[value.id]
        return value

    # -- class structure ---------------------------------------------------------------

    def bases(self, module: str, node: ast.ClassDef) -> Iterator[tuple[str | None, _Class | None]]:
        """Each base class as ``(name, resolved)``; resolved is None when it is not visible here."""
        for base in node.bases:
            name = _name_of(base)
            yield name, (self.class_def(module, name) if name else None)

    def descends_from(self, module: str, node: ast.ClassDef, ancestor: str, depth: int = 0) -> bool:
        if depth > _MAX_DEPTH:
            return False
        for name, resolved in self.bases(module, node):
            if name == ancestor:
                return True
            if resolved is not None and self.descends_from(resolved.module, resolved.node, ancestor, depth + 1):
                return True
        return False

    def inherits_from_outside(self, module: str, node: ast.ClassDef, depth: int = 0) -> bool:
        """True when a base class is not visible here, so it could be supplying the attribute."""
        if depth > _MAX_DEPTH:
            return True
        for name, resolved in self.bases(module, node):
            if name is None:
                return True  # something dynamic; assume it might provide the attribute
            if name in (_WORLD_BASE, _WEBWORLD_BASE):
                continue  # core's bases, which define neither web nor tutorials usefully
            if resolved is None:
                return True
            if self.inherits_from_outside(resolved.module, resolved.node, depth + 1):
                return True
        return False

    def find_attribute(
        self, module: str, node: ast.ClassDef, attribute: str, depth: int = 0
    ) -> _Attribute | None:
        """Look for a class attribute here, then in the base classes this world defines."""
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id == attribute:
                        return _Attribute(module, statement.value)
            elif isinstance(statement, ast.AnnAssign):
                if isinstance(statement.target, ast.Name) and statement.target.id == attribute:
                    # A bare "tutorials: List[Tutorial]" annotation declares nothing at runtime,
                    # which is exactly why the base WebWorld fails the WebHost's hasattr check.
                    return _Attribute(module, statement.value) if statement.value is not None else None

        if depth <= _MAX_DEPTH:
            for _name, resolved in self.bases(module, node):
                if resolved is not None:
                    found = self.find_attribute(resolved.module, resolved.node, attribute, depth + 1)
                    if found is not None:
                        return found
        return None


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
