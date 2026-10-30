"""American spelling outside the website: the owner is American (2026-10-27).

The website has its own check (`apps/web/lib/american-spelling.test.ts`).
This one reads the prose of everything else: Python comments and docstrings,
the names of tests, and the markdown docs.

Names that cross a boundary keep their spelling, so this check doesn't read
code: a Temporal workflow's class is its type name (`ReadLaserColoursWorkflow`),
and tables, columns, API schemas and contract fields are stored or on the wire
(`automatic_laser_colour_readings`, `LaserColoursView`). Renaming one is a
migration or a contract change, not a spelling fix. For the same reason prose
may name them: a word joined to others by `_`, or inside backticks, is a name.

Migrations are history, and their SQL is pinned by hash, so they're skipped.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: British spellings and the American ones, whole words only.
BRITISH = [
    (r"colou?rs?|colou?r(ed|ing)|uncolou?red", "colour", "color"),
    (r"behaviours?", "behaviour", "behavior"),
    (r"(milli|centi|kilo)?metres?", "metre", "meter"),
    (r"(de)?centr(e|es|ed|ing)", "centre", "center"),
    (r"(un|re|mis)?labell(ed|ing|ers?)", "labelled", "labeled"),
    (r"cancell(ed|ing)", "cancelled", "canceled"),
    (r"judgements?", "judgement", "judgment"),
    (r"grey(scale)?", "grey", "gray"),
    (r"honou?r(s|ed|ing)?", "honour", "honor"),
    (r"neighbou?r(s|hood|ing)?", "neighbour", "neighbor"),
    (r"favou?r(s|ed|ing|ite)?", "favour", "favor"),
    (r"modell(ed|ing)", "modelled", "modeled"),
    (r"signall(ed|ing)", "signalled", "signaled"),
    (r"travell(ed|ing)", "travelled", "traveled"),
    (r"licence", "licence", "license"),
    (r"artefacts?", "artefact", "artifact"),
    (r"catalogues?", "catalogue", "catalog"),
    (r"whilst", "whilst", "while"),
    (r"analys(e|es|ed|ing)", "analyse", "analyze"),
    (
        r"(re|un)?(organ|recogn|normal|serial|material|optim|maxim|minim|initial"
        r"|general|real|character|parameter|quant|summar|priorit|standard"
        r"|final|visual|author|custom|synchron|util|categor|random|sanit"
        r"|stabil|emphas|special|capital|central|penal|apolog)is(e|es|ed|ing|ation"
        r"|ations|able)",
        "-ise",
        "-ize",
    ),
]

_WORD = re.compile(
    r"(?<![A-Za-z0-9_/])("
    + "|".join(f"(?:{p})" for p, _, _ in BRITISH)
    + r")(?![A-Za-z0-9_/]|\.[a-z]{2,4}\b)",
    re.IGNORECASE,
)

#: American words the patterns above would otherwise take for British.
AMERICAN = {"color", "colors", "colored", "coloring", "uncolored", "honor", "honors"}
AMERICAN |= {"honored", "honoring", "neighbor", "neighbors", "neighborhood"}
AMERICAN |= {"neighboring", "favor", "favors", "favored", "favoring", "favorite"}

#: Names someone else spells, written where a backtick can't go (a literal
#: block): the laser-color backfill's Temporal workflow ids.
THEIRS = ("laser-colour-backfill-",)

_CODE = re.compile(r"``.*?``|`[^`\n]*`", re.DOTALL)


def british_words(text: str) -> list[str]:
    """Every British spelling in prose, skipping names: anything in backticks,
    joined to a word by `_`, or part of a path (`laser_validation/judgement.py`)."""
    for name in THEIRS:
        text = text.replace(name, "")
    text = _CODE.sub("", text)
    return [
        m.group(1) for m in _WORD.finditer(text) if m.group(1).lower() not in AMERICAN
    ]


def _docstrings(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield node, doc


def python_prose(source: str) -> list[tuple[int, str]]:
    """A Python file's prose, with line numbers: its comments, its docstrings,
    and the names of its tests."""
    found = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            found.append((tok.start[0], tok.string))
    tree = ast.parse(source)
    for node, doc in _docstrings(tree):
        found.append((getattr(node, "lineno", 1), doc))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith(("test_", "Test")):
                found.append((node.lineno, node.name.replace("_", " ")))
    return found


_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def markdown_prose(text: str) -> str:
    """A markdown file without its code blocks, which hold names, not prose."""
    return _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


SKIPPED_DIRS = {"node_modules", ".venv", "migrations", "__pycache__", ".git"}


def _files(suffix: str):
    for top in ("services", "deploy", "tests", "docs"):
        for path in (ROOT / top).rglob(f"*{suffix}"):
            if not SKIPPED_DIRS & set(path.relative_to(ROOT).parts):
                yield path
    if suffix == ".md":
        yield from sorted(ROOT.glob("*.md"))


def test_finds_british_spellings_and_leaves_names_alone():
    assert british_words("the laser colour is centred, labelled by the labellers") == [
        "colour",
        "centred",
        "labelled",
        "labellers",
    ]
    assert british_words("Normalised whilst behaviour judgements honour it") == [
        "Normalised",
        "whilst",
        "behaviour",
        "judgements",
        "honour",
    ]
    assert (
        british_words("`laser_colour` and laser_colour_store and LaserColoursView")
        == []
    )
    assert british_words("``colour`` and laser_validation/judgement.py") == []
    assert british_words("--workflow-id laser-colour-backfill-dry-...") == []
    assert (
        british_words("color, centered, labeled, honors, neighbors, gray, realize")
        == []
    )
    assert (
        british_words("precise, otherwise, noise, raised, analysis, CancelledError")
        == []
    )


def test_python_prose_is_comments_docstrings_and_test_names():
    source = '''"""Module colour."""
x = "colour"  # the centre
def test_the_labelled_frame():
    """Its behaviour."""
    laser_colour = 1
'''
    words = [w for _, text in python_prose(source) for w in british_words(text)]
    assert sorted(words) == ["behaviour", "centre", "colour", "labelled"]


def test_python_is_written_in_american():
    found = [
        f"{path.relative_to(ROOT)}:{line}: {word}"
        for path in _files(".py")
        for line, text in python_prose(path.read_text())
        for word in british_words(text)
    ]
    assert found == []


def test_docs_are_written_in_american():
    found = [
        f"{path.relative_to(ROOT)}: {word}"
        for path in _files(".md")
        for word in british_words(markdown_prose(path.read_text()))
    ]
    assert found == []
