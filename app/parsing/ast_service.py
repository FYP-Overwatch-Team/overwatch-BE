"""tree-sitter based extraction of imports and top-level definitions.

Uses tree_sitter_language_pack for prebuilt grammars (same approach as Aider's
repo map). Extraction is deliberately shallow for eval 1: import targets and
top-level class/function names per file.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pathspec
from tree_sitter_language_pack import get_parser

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

IGNORE_DIRS = {
    ".git", "node_modules", "dist", "build", "out", "coverage", "vendor",
    "venv", ".venv", "__pycache__", ".next", ".turbo", "target", ".pytest_cache",
    ".idea", ".vscode",
}

MAX_FILE_BYTES = 512 * 1024  # skip generated/bundled monsters


@dataclass
class FileParse:
    path: str  # repo-relative, posix separators
    language: str
    imports: list[str] = field(default_factory=list)
    definitions: list[dict] = field(default_factory=list)  # {"name", "kind"}


def _load_gitignore(repo_root: Path) -> pathspec.PathSpec | None:
    gitignore = repo_root / ".gitignore"
    if not gitignore.is_file():
        return None
    return pathspec.PathSpec.from_lines("gitignore", gitignore.read_text(encoding="utf-8", errors="ignore").splitlines())


def iter_source_files(repo_root: Path) -> list[Path]:
    spec = _load_gitignore(repo_root)
    results: list[Path] = []
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        if spec and spec.match_file(rel.as_posix()):
            continue
        if rel.suffix not in LANGUAGE_BY_EXT:
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        results.append(path)
    return results


def parse_repo(repo_root: Path) -> list[FileParse]:
    if not repo_root.is_dir():
        raise FileNotFoundError(f"repo checkout not found at {repo_root}")
    return [parse_file(repo_root, path) for path in iter_source_files(repo_root)]


def parse_file(repo_root: Path, path: Path) -> FileParse:
    language = LANGUAGE_BY_EXT[path.suffix]
    source = path.read_bytes()
    tree = get_parser(language).parse(source)
    rel = path.relative_to(repo_root).as_posix()
    result = FileParse(path=rel, language=language)

    if language == "python":
        _extract_python(tree.root_node, source, result)
    else:
        _extract_js_ts(tree.root_node, source, result)
    return result


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _extract_python(root, source: bytes, result: FileParse) -> None:
    for node in root.named_children:
        if node.type == "import_statement":
            for child in node.named_children:  # dotted_name | aliased_import
                target = child.child_by_field_name("name") if child.type == "aliased_import" else child
                if target is not None:
                    result.imports.append(_text(target, source))
        elif node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is not None:
                result.imports.append(_text(module, source))
        elif node.type == "function_definition":
            result.definitions.append({"name": _text(node.child_by_field_name("name"), source), "kind": "function"})
        elif node.type == "class_definition":
            result.definitions.append({"name": _text(node.child_by_field_name("name"), source), "kind": "class"})
        elif node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None and inner.type in ("function_definition", "class_definition"):
                kind = "function" if inner.type == "function_definition" else "class"
                result.definitions.append({"name": _text(inner.child_by_field_name("name"), source), "kind": kind})


def _extract_js_ts(root, source: bytes, result: FileParse) -> None:
    for node in root.named_children:
        if node.type == "import_statement":
            src = node.child_by_field_name("source")
            if src is not None:
                result.imports.append(_text(src, source).strip("'\""))
        elif node.type in ("lexical_declaration", "variable_declaration"):
            _extract_js_declaration(node, source, result)
        elif node.type == "function_declaration":
            result.definitions.append({"name": _text(node.child_by_field_name("name"), source), "kind": "function"})
        elif node.type == "class_declaration":
            result.definitions.append({"name": _text(node.child_by_field_name("name"), source), "kind": "class"})
        elif node.type == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is None:
                continue
            if decl.type == "function_declaration":
                result.definitions.append({"name": _text(decl.child_by_field_name("name"), source), "kind": "function"})
            elif decl.type == "class_declaration":
                result.definitions.append({"name": _text(decl.child_by_field_name("name"), source), "kind": "class"})
            elif decl.type in ("lexical_declaration", "variable_declaration"):
                _extract_js_declaration(decl, source, result)


def _extract_js_declaration(node, source: bytes, result: FileParse) -> None:
    """Pick up `const x = require('y')` imports and top-level arrow-function defs."""
    for declarator in (c for c in node.named_children if c.type == "variable_declarator"):
        value = declarator.child_by_field_name("value")
        name = declarator.child_by_field_name("name")
        if value is None or name is None:
            continue
        if value.type == "call_expression":
            fn = value.child_by_field_name("function")
            args = value.child_by_field_name("arguments")
            if fn is not None and _text(fn, source) == "require" and args is not None and args.named_child_count:
                result.imports.append(_text(args.named_children[0], source).strip("'\""))
        elif value.type == "arrow_function":
            result.definitions.append({"name": _text(name, source), "kind": "function"})
