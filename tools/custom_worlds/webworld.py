"""Catching worlds whose ``WebWorld`` wiring is broken, without running their code.

A world can satisfy every manifest check and still be useless here, because the manifest says
nothing about the code. Problems come in two severities, both confirmed against this checkout by
loading a world with the mistake in it:

``load`` - Archipelago refuses to load the world at all
    ``web = MyGameWeb`` assigns the class instead of an instance, and
    ``AutoWorldRegister.__new__`` asserts ``isinstance(dct["web"], WebWorld)``, so importing raises
    ``AssertionError: WebWorld has to be instantiated.``. Instantiating a name that does not exist
    raises ``NameError``. Source that does not parse raises ``SyntaxError``. All three are caught
    per-world by ``WorldSource.load`` and recorded in ``failed_world_loads``, so the world is simply
    absent rather than taking anything else down with it.

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
import warnings
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass

_INVALID_ESCAPE = "invalid escape sequence"

#: Archipelago will not load the world at all.
SEVERITY_LOAD = "load"
#: The world loads, but ``WebHost.py`` drops it from the site and the data package.
SEVERITY_WEBHOST = "webhost"
#: Worth saying out loud, but never a reason to refuse a world: it serves, with something degraded.
SEVERITY_NOTE = "note"

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
#: ``options_presets`` maps preset names to scalars rather than to option dictionaries.
PRESETS_NOT_NESTED = "presets-not-nested"
#: The world has tutorials but ships no ``docs/`` folder for the WebHost to copy.
DOCS_MISSING = "docs-missing"
#: The ``settings`` annotation is one core's own resolver cannot turn back into a class.
SETTINGS_UNRESOLVABLE = "settings-unresolvable"
#: A string literal contains an escape Python does not recognise, such as ``"setup\\en"``.
BAD_ESCAPE = "bad-escape"
#: An option sets ``visibility`` to a plain int rather than to a ``Visibility`` flag.
VISIBILITY_NOT_A_FLAG = "visibility-not-a-flag"

ROOT = ""  # the world package itself, i.e. <name>/__init__.py

_WORLD_BASE = "World"
_WEBWORLD_BASE = "WebWorld"
_TUTORIALS = "tutorials"
_PRESETS = "options_presets"
_WEB = "web"
_HIDDEN = "hidden"
_SETTINGS = "settings"
_DOCS = "docs"
_PATCH_ENDING = "patch_file_ending"
_OPTIONS_DATACLASS = "options_dataclass"
_VISIBILITY = "visibility"
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
    #: Where a repair would have to go: the module, and the class inside it to change.
    module: str = ""
    target_class: str = ""

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


def inspect_world(
    modules: Mapping[str, str],
    *,
    module_name: str = "",
    files: Collection[str] | None = None,
    syntax_authoritative: bool = True,
) -> list[WebWorldFinding]:
    """Report WebWorld problems provable from a world's source.

    ``modules`` maps dotted module paths - as produced by :func:`module_path` - to their source.
    ``files`` is every path inside the world folder, Python or not, which is what the ``docs/``
    check needs. It is optional: a caller that cannot enumerate the package has nothing to say
    about a missing folder, and silence beats a guess.

    ``syntax_authoritative`` says whether this interpreter is new enough to judge the world's syntax.
    Source is parsed by whatever Python is running the crawler, and a world may legitimately use
    syntax that Python does not have: 3.12 accepts ``type Alias = int | str`` where 3.11 raises. Run
    older than the image, an unparseable module is reported without being treated as proof.
    """
    label = module_name or "the world"
    package = _Package(modules)

    if ROOT not in modules:
        return []
    root = package.index(ROOT)
    if root is None:
        error = package.errors[ROOT]
        return [
            _unparseable(f"{label}/__init__.py", error, syntax_authoritative, imported=False)
        ]

    findings: list[WebWorldFinding] = []
    for path in package.reachable():
        if package.index(path) is None and path in package.errors:
            # The world imports this module, so Python will parse it too - and raise where we did.
            error = package.errors[path]
            findings.append(
                _unparseable(f"{label}/{_file_name(path)}", error, syntax_authoritative, imported=True)
            )

    findings.extend(_check_escapes(label, package))

    has_docs = _has_docs(files)
    for path, name, node in package.world_classes():
        finding = _check_world(path, name, node, package)
        if finding is not None:
            findings.append(finding)
        if has_docs is False:
            docs = _check_docs(label, path, name, node, package, finding)
            if docs is not None:
                findings.append(docs)
        settings = _check_settings(path, name, node, package)
        if settings is not None:
            findings.append(settings)
        findings.extend(_check_option_visibility(name, path, node, package))
    return findings


def patch_endings(modules: Mapping[str, str]) -> set[str]:
    """Every ``patch_file_ending`` a world's patch containers claim.

    ``AutoPatchRegister`` keys these globally and raises ``ImproperlyConfiguredAutoPatchError`` the
    moment a second class claims one that is taken, so the loser fails to import. The loser is
    whichever loads later, which is alphabetical - a custom world can and does knock out one of
    Archipelago's own this way.

    Read from every module in the package, reachable or not: a patch container registers itself as
    soon as its class statement executes, and worlds keep them in files like ``Rom.py`` that
    ``__init__`` imports for their side effects.

    Registration is keyed on ``"game" in dct`` - the class's *own* body, not anything inherited - so
    that is the condition mirrored here. It matters: core's factorio, kh1 and kh2 all name ``.zip``
    on classes that inherit ``game`` rather than setting it, so they never register and never clash.
    ``.zip`` is skipped regardless, since core raises on it before it reaches the registry.
    """
    endings: set[str] = set()
    package = _Package(modules)
    for path in modules:
        index = package.index(path)
        if index is None:
            continue
        for node in index.classes.values():
            if _own_attribute(node, "game") is None:
                continue
            value = _own_attribute(node, _PATCH_ENDING)
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value != ".zip":
                endings.add(value.value)
    return endings


def _own_attribute(node: ast.ClassDef, attribute: str) -> ast.expr | None:
    """A value assigned in this class's own body, which is all that reaches a metaclass's ``dct``."""
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == attribute:
                    return statement.value
        elif isinstance(statement, ast.AnnAssign):
            if isinstance(statement.target, ast.Name) and statement.target.id == attribute:
                return statement.value
    return None


def world_game(modules: Mapping[str, str]) -> str:
    """The ``game`` a world registers itself under, or "" when the source does not say plainly.

    ``AutoWorldRegister`` keys on ``dct["game"]`` - the class's own body - and raises RuntimeError if
    that name is taken, so this mirrors the same rule. A game name built at runtime returns "",
    since a guess here would cost a world.
    """
    package = _Package(modules)
    for path, _name, node in package.world_classes():
        value = _own_attribute(node, "game")
        if value is None:
            continue
        resolved = package.value_of(path, value).value
        if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
            return resolved.value
    return ""


def _unparseable(
    where: str, error: SyntaxError, authoritative: bool, *, imported: bool
) -> WebWorldFinding:
    """A module that would not parse, graded by whether this interpreter can be trusted to say so.

    A world that really is broken here still loads harmlessly badly: ``WorldSource.load`` catches the
    SyntaxError, records it in ``failed_world_loads`` and carries on, so the other worlds and the
    site are unaffected. That makes reporting-without-refusing the safe side to err on when this
    Python is older than the image's - losing a working world costs more than shipping a broken one.
    """
    detail = f"{where} is not valid Python: line {error.lineno}: {error.msg}"
    if imported:
        detail += "; the world imports it"
    if authoritative:
        return WebWorldFinding(code=UNPARSEABLE, detail=f"{detail}, so it cannot load", severity=SEVERITY_LOAD)
    return WebWorldFinding(
        code=UNPARSEABLE,
        detail=(
            f"{detail}. This Python is older than the one the image runs, so that may only mean the "
            "world uses syntax it does not have yet - installed anyway rather than refused. Run the "
            "crawler on the image's Python to get a firm answer"
        ),
        severity=SEVERITY_NOTE,
    )


def _has_docs(files: Collection[str] | None) -> bool | None:
    """Whether the world ships a ``docs/`` folder, or None when the caller could not say."""
    if files is None:
        return None
    return any(name == f"{_DOCS}/" or name.startswith(f"{_DOCS}/") for name in files)


def _check_docs(
    label: str,
    path: str,
    name: str,
    node: ast.ClassDef,
    package: "_Package",
    web_finding: WebWorldFinding | None,
) -> WebWorldFinding | None:
    """A world the WebHost will publish tutorials for must ship the folder it copies them from.

    ``copy_tutorials_files_to_static()`` runs at start-up, after the options templates, and for every
    non-hidden world with a ``web.tutorials`` it does a bare ``os.listdir(<world>/docs)``. There is
    no try/except: one world missing the folder raises FileNotFoundError and the WebHost never
    finishes starting.

    This only bites worlds installed as folders. The zip branch above it walks the archive's entries
    and copies the ones under ``docs/``, so an ``.apworld`` without any simply contributes nothing -
    which is why this is checked against the install mode rather than reported unconditionally.
    """
    if web_finding is not None and web_finding.code in _NO_TUTORIALS_REACHED:
        return None  # the world has no usable tutorials list, which is already the finding
    if _is_hidden(path, node, package):
        return None
    if package.find_attribute(path, node, _WEB) is None:
        return None
    return WebWorldFinding(
        code=DOCS_MISSING,
        detail=(
            f"{name} has tutorials but {label} ships no 'docs/' folder. WebHost.py copies each "
            "world's docs to its static folder at start-up with a bare os.listdir(), so the "
            "missing folder raises FileNotFoundError and the site never starts"
        ),
        severity=SEVERITY_WEBHOST,
        world_class=name,
        module=path,
        target_class=name,
    )


#: Findings that mean the world never reaches the tutorial copy at all, so docs cannot be the issue.
_NO_TUTORIALS_REACHED = frozenset(
    {WEB_NOT_INSTANTIATED, WEB_UNDEFINED, WEB_MISSING, WEB_NO_TUTORIALS, TUTORIALS_NOT_A_LIST}
)


def _is_hidden(path: str, node: ast.ClassDef, package: "_Package") -> bool:
    """``hidden = True`` keeps a world off the site, and out of the tutorial copy with it."""
    found = package.find_attribute(path, node, _HIDDEN)
    if found is None or found.value is None:
        return False
    value = package.value_of(found.module, found.value).value
    return isinstance(value, ast.Constant) and value.value is True


def _check_settings(
    path: str, name: str, node: ast.ClassDef, package: "_Package"
) -> WebWorldFinding | None:
    """``settings`` has to be annotated in the one shape core's resolver can read back.

    Only when the annotation reaches core as a *string*, which is the branch this is about.
    ``settings.py`` reads ``world.__annotations__["settings"]`` and, for a string, does not evaluate
    it. It takes the text, strips a single layer of brackets, and hands the remainder to ``getattr``
    on the world's module::

        cls_name = cls_or_name
        if "[" in cls_name:
            cls_name = cls_name.split("[", 1)[1].rsplit("]", 1)[0]
        cls = getattr(__import__(world_mod, fromlist=[cls_name]), cls_name)

    One layer. ``ClassVar[MySettings]`` works; ``ClassVar[type[MySettings]]`` leaves
    ``type[MySettings]``, which is not a name any module has, and the lookup raises AttributeError
    when the settings file is next written. Rather than guess at the shapes, this runs core's two
    lines and asks whether what falls out could be a name at all.

    This costs more than the one world. ``Settings.dump`` walks *every* registered world's settings
    to load them before writing ``host.yaml``, so the AttributeError propagates out of the loop and
    the host's settings file is never written at all - the same shape of damage as a world that
    breaks the option templates, which is why it is refused rather than merely noted.

    Without ``from __future__ import annotations`` the annotation arrives as a real object instead
    and core resolves it with ``typing.get_args``, which handles nesting and dotted names perfectly
    well - core's own sc2 writes ``ClassVar[settings.Starcraft2Settings]`` and is fine. Reporting
    that would be a false positive, so the string branch is a precondition, not a detail.
    """
    annotation = package.find_annotation(path, node, _SETTINGS)
    if annotation is None:
        return None
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        written = annotation.value  # settings: "ClassVar[...]" is a string whatever the module does
    elif package.string_annotations(path):
        written = ast.unparse(annotation)
    else:
        return None
    resolved = _core_settings_name(written)
    if resolved.isidentifier():
        return None
    return WebWorldFinding(
        code=SETTINGS_UNRESOLVABLE,
        detail=(
            f"{name} annotates settings as '{written}', which core reduces to '{resolved}' - not a "
            "name it can look up, so reading or saving this world's settings raises AttributeError. "
            "core strips one layer of brackets only, so it needs 'ClassVar[MySettings]' rather than "
            "'ClassVar[type[MySettings]]'"
        ),
        severity=SEVERITY_WEBHOST,
        world_class=name,
    )


def _check_option_visibility(
    world_name: str, path: str, node: ast.ClassDef, package: "_Package"
) -> list[WebWorldFinding]:
    """``visibility`` has to be a ``Visibility`` flag, not the integer that flag is made of.

    ``Visibility`` is an ``IntFlag``, and ``get_option_groups`` asks ``visibility_level in
    option.visibility``. Membership works on the flag type and not on a bare ``int``::

        TypeError: argument of type 'int' is not iterable

    That propagates through ``generate_yaml_templates`` and out of ``create_options_files()``, so
    like a flat ``options_presets`` it stops the WebHost booting rather than dropping one game.

    Only the options core actually iterates are checked - the ones annotated on the world's
    ``options_dataclass`` - which is the same set ``get_option_groups`` walks. An option class the
    world defines but never attaches is never asked for its visibility, so it is not a problem.
    """
    found = package.find_attribute(path, node, _OPTIONS_DATACLASS)
    if found is None or found.value is None:
        return []
    dataclass = package.class_from(found.module, found.value)
    if dataclass is None:
        return []

    findings = []
    for option_name, option in _annotated_options(dataclass, package):
        visibility = package.find_attribute(option.module, option.node, _VISIBILITY)
        if visibility is None or visibility.value is None:
            continue
        value = package.value_of(visibility.module, visibility.value).value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, int):
            continue
        if isinstance(value.value, bool):
            continue  # True/False is a different mistake, and not one that raises here
        findings.append(
            WebWorldFinding(
                code=VISIBILITY_NOT_A_FLAG,
                detail=(
                    f"{option.name}, the option behind '{option_name}', sets visibility to "
                    f"{value.value!r} rather than to a Visibility flag. Visibility is an IntFlag and "
                    "the WebHost asks 'visibility_level in option.visibility', which raises "
                    "TypeError on a plain int - so create_options_files() fails and the server does "
                    "not start at all. It needs Visibility.none, Visibility.template, or the flags "
                    "combined with '|'"
                ),
                severity=SEVERITY_WEBHOST,
                world_class=world_name,
            )
        )
    return findings


def _annotated_options(dataclass: "_Class", package: "_Package") -> Iterator[tuple[str, "_Class"]]:
    """The option classes an options dataclass declares, following bases inside this world."""
    seen: set[str] = set()
    for current in package.own_and_bases(dataclass):
        for statement in current.node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            name = statement.target.id
            if name in seen:
                continue
            seen.add(name)
            option = package.class_from(current.module, statement.annotation)
            if option is not None:
                yield name, option


def _core_settings_name(annotation: str) -> str:
    """``settings.Settings.__getattribute__``'s own reduction, kept identical to it on purpose."""
    if "[" in annotation:
        return annotation.split("[", 1)[1].rsplit("]", 1)[0]
    return annotation


def _check_escapes(label: str, package: "_Package") -> list[WebWorldFinding]:
    """Report string literals carrying escapes Python does not recognise.

    ``"setup\\en"`` is meant to be ``"setup/en"``. Python keeps the backslash and warns, so the world
    still loads - with a SyntaxWarning on every start-up and a tutorial link pointing nowhere.
    """
    findings = []
    for path in package.reachable():
        for lineno, message in package.escapes.get(path, []):
            findings.append(
                WebWorldFinding(
                    code=BAD_ESCAPE,
                    detail=(
                        f"{label}/{_file_name(path)} line {lineno}: {message}. Python warns and "
                        "keeps the backslash, so the string is not what it looks like - a tutorial "
                        r"link written 'setup\en' needs to be 'setup/en'"
                    ),
                    severity=SEVERITY_NOTE,
                )
            )
    return findings


def _file_name(path: str) -> str:
    """The file a dotted module path came from, for messages that point at something openable."""
    return f"{path.replace('.', '/')}.py" if path else "__init__.py"


def _check_world(path: str, name: str, node: ast.ClassDef, package: "_Package") -> WebWorldFinding | None:
    found = package.find_attribute(path, node, _WEB)

    if found is None or found.value is None:
        return _check_missing_web(path, name, node, package)

    value, owner = found.value, found.module
    if isinstance(value, ast.Call):
        root_name = _root_name_of(value.func)
        if root_name and package.is_unknown(owner, root_name) and package.class_from(owner, value.func) is None:
            callee = _name_of(value.func) or root_name
            return WebWorldFinding(
                code=WEB_UNDEFINED,
                detail=f"{name} sets web = {callee}(), but {root_name} is never defined or imported",
                severity=SEVERITY_LOAD,
                world_class=name,
            )
        callee = _name_of(value.func) or ""
        webworld = package.class_from(owner, value.func)
        if webworld is not None:
            return _check_tutorials(name, webworld, package) or _check_presets(name, webworld, package)
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

    if isinstance(value, (ast.Name, ast.Attribute)):
        # web = MyGameWeb and web = web_world.MyGameWeb are the same mistake, so both are resolved
        # the same way; only where to look for a same-named instance differs.
        written = ast.unparse(value)
        home = owner if isinstance(value, ast.Name) else package.module_of(owner, value.value)
        attribute = value.id if isinstance(value, ast.Name) else value.attr
        if package.class_from(owner, value) is not None and (
            home is None or attribute not in package.instances(home)
        ):
            return WebWorldFinding(
                code=WEB_NOT_INSTANTIATED,
                detail=(
                    f"{name} sets web = {written} instead of web = {written}(); core asserts "
                    "'WebWorld has to be instantiated.' and the world will not load"
                ),
                severity=SEVERITY_LOAD,
                world_class=name,
            )
        root_name = _root_name_of(value)
        if root_name and package.is_unknown(owner, root_name) and package.class_from(owner, value) is None:
            return WebWorldFinding(
                code=WEB_UNDEFINED,
                detail=f"{name} sets web = {written}, but {root_name} is never defined or imported",
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
        module=path,
        target_class=name,
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
            module=webworld.module,
            target_class=web_name,
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


def _check_presets(world_name: str, webworld: "_Class", package: "_Package") -> WebWorldFinding | None:
    """``options_presets`` maps a preset *name* to a dict of option values, not to a value.

    Getting this wrong is worse than it sounds. The WebHost renders one template per preset and does
    ``option_key in preset``; against a scalar that raises TypeError, ``create_options_files()``
    propagates it, and the server never finishes starting - so this takes the whole site down rather
    than dropping one game.
    """
    found = package.find_attribute(webworld.module, webworld.node, _PRESETS)
    if found is None or found.value is None:
        return None

    where = package.value_of(found.module, found.value)
    presets = where.value
    if not isinstance(presets, ast.Dict):
        return None

    for key, value in zip(presets.keys, presets.values, strict=False):
        if not package.is_definitely_scalar(where.module, value):
            continue
        preset = key.value if isinstance(key, ast.Constant) else "?"
        return WebWorldFinding(
            code=PRESETS_NOT_NESTED,
            detail=(
                f"{webworld.name} sets 'options_presets' to a flat mapping - preset {preset!r} is a "
                "single value, not a dictionary of option settings. The WebHost renders a template "
                "per preset and this raises, so create_options_files() fails and the server does "
                "not start at all. It should read like "
                "{'Preset Name': {'option_name': value, ...}}"
            ),
            severity=SEVERITY_WEBHOST,
            world_class=world_name,
        )
    return None


def _registers_as_world(node: ast.ClassDef) -> bool:
    """Whether ``AutoWorldRegister`` will treat this class as a world, by core's own rule.

    Naming ``World`` as a base is the obvious case, but it is not the rule core uses and it misses
    real worlds: Age Of Empires II writes ``class Age2World(CachedRuleBuilderWorld)``, whose base
    lives in core and so cannot be resolved from inside the apworld. Nothing about that class was
    ever checked, and it reached the image without a WebWorld.

    The rule core actually applies is ``"game" in dct`` - the class's own body - and it then asserts
    ``item_name_to_id`` and ``location_name_to_id`` are there too. A class carrying all three is one
    Archipelago registers, whatever it inherits from. Requiring all three is also what keeps patch
    containers out: they carry ``game`` as well, but never the id maps.
    """
    return all(_own_attribute(node, attribute) is not None for attribute in _WORLD_MARKERS)


#: What core requires of a class in its own body before it registers as a world.
_WORLD_MARKERS = ("game", "item_name_to_id", "location_name_to_id")


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
    """An expression, and the module whose scope it should be read in."""

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
        #: "from __future__ import annotations" makes every annotation here a plain string at
        #: runtime, which is what decides how core reads this module's 'settings'.
        self.string_annotations = any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
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
        self.escapes: dict[str, list[tuple[int, str]]] = {}
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
            # Python strips a UTF-8 BOM when it imports a file; ast.parse does not, and would
            # otherwise reject a perfectly importable module for its first character.
            with warnings.catch_warnings(record=True) as raised:
                warnings.simplefilter("always")
                tree = ast.parse(source.lstrip("\ufeff"))
        except SyntaxError as error:
            self.errors[path] = error
            return None
        # Matched on the message rather than the category: the same complaint is a DeprecationWarning
        # before Python 3.12 and a SyntaxWarning from 3.12 on, and the crawler need not be running
        # the interpreter the image will.
        found = [
            (raised_warning.lineno, str(raised_warning.message))
            for raised_warning in raised
            if _INVALID_ESCAPE in str(raised_warning.message)
        ]
        if found:
            self.escapes[path] = found
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
        """Every class in the modules this world reaches that Archipelago will register as a world."""
        for path in self.reachable():
            index = self.index(path)
            if index is None:
                continue
            for name, node in index.classes.items():
                if self.descends_from(path, node, _WORLD_BASE) or _registers_as_world(node):
                    yield path, name, node

    # -- name resolution ---------------------------------------------------------------

    def class_from(self, module: str, expression: ast.expr, depth: int = 0) -> _Class | None:
        """The class an expression names, following module aliases as well as plain names.

        ``web = MyGameWeb()`` and ``web = web_world.MyGameWeb()`` are equally common, and the second
        needs the alias resolving to a module before the class can be looked up inside it.
        """
        if isinstance(expression, ast.Name):
            return self.class_def(module, expression.id, depth)
        if isinstance(expression, ast.Attribute):
            container = self.module_of(module, expression.value)
            if container is not None:
                return self.class_def(container, expression.attr, depth)
        return None

    def module_of(self, module: str, expression: ast.expr, depth: int = 0) -> str | None:
        """The module inside this world that an expression refers to, if any."""
        if depth > _MAX_DEPTH:
            return None
        index = self.index(module)
        if index is None:
            return None
        if isinstance(expression, ast.Name):
            entry = index.imports.get(expression.id)
            # A module alias is recorded with an empty original name.
            if entry is not None and entry.module is not None and not entry.name:
                return entry.module
            return None
        if isinstance(expression, ast.Attribute):
            parent = self.module_of(module, expression.value, depth + 1)
            if parent is not None:
                candidate = self.join(parent, expression.attr)
                return candidate if self.has(candidate) else None
        return None

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

    def value_of(self, module: str, expression: ast.expr, depth: int = 0) -> _Attribute:
        """Follow a name to what it was assigned, across modules where the import is visible.

        The module comes back with the value, because whatever the value refers to has to be looked
        up where it was written - a dict in ``Options.py`` names classes from ``Options.py``, not
        from the module that imported it.
        """
        if depth > _MAX_DEPTH or not isinstance(expression, ast.Name):
            return _Attribute(module, expression)
        index = self.index(module)
        if index is None:
            return _Attribute(module, expression)
        if expression.id in index.assigned:
            return self.value_of(module, index.assigned[expression.id], depth + 1)
        entry = index.imports.get(expression.id)
        if entry is not None and entry.module is not None and entry.name:
            origin = self.index(entry.module)
            if origin is not None and entry.name in origin.assigned:
                return self.value_of(entry.module, origin.assigned[entry.name], depth + 1)
        return _Attribute(module, expression)

    def is_definitely_scalar(self, module: str, expression: ast.expr, depth: int = 0) -> bool:
        """Whether an expression provably evaluates to a single value rather than a mapping.

        Only provable cases count: a literal, a name bound to one, or a class attribute like
        ``DragunGoal.default`` whose definition is visible. Anything else is left alone.
        """
        if depth > _MAX_DEPTH:
            return False
        where = self.value_of(module, expression)
        resolved = where.value
        if isinstance(resolved, ast.Constant):
            return resolved.value is not None
        if isinstance(resolved, (ast.List, ast.Tuple, ast.Set)):
            return True
        if isinstance(resolved, ast.Attribute):
            owner = self.class_from(where.module, resolved.value)
            if owner is not None:
                found = self.find_attribute(owner.module, owner.node, resolved.attr)
                if found is not None and found.value is not None:
                    return self.is_definitely_scalar(found.module, found.value, depth + 1)
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

    def own_and_bases(self, start: "_Class", depth: int = 0) -> Iterator["_Class"]:
        """A class and the base classes of it this world defines, nearest first."""
        yield start
        if depth > _MAX_DEPTH:
            return
        for _name, resolved in self.bases(start.module, start.node):
            if resolved is not None:
                yield from self.own_and_bases(resolved, depth + 1)

    def string_annotations(self, module: str) -> bool:
        """Whether this module's annotations reach core as strings rather than objects."""
        index = self.index(module)
        return index is not None and index.string_annotations

    def find_annotation(self, module: str, node: ast.ClassDef, attribute: str, depth: int = 0) -> ast.expr | None:
        """The *annotation* on a class attribute, which is what core reads for ``settings``.

        Distinct from :meth:`find_attribute`, which wants the assigned value: here the annotation is
        the whole point, and an attribute assigned without one has nothing to check.
        """
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign):
                if isinstance(statement.target, ast.Name) and statement.target.id == attribute:
                    return statement.annotation

        if depth <= _MAX_DEPTH:
            for _name, resolved in self.bases(module, node):
                if resolved is not None:
                    found = self.find_annotation(resolved.module, resolved.node, attribute, depth + 1)
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
