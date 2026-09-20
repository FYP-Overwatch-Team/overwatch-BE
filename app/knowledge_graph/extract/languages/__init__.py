"""Registry of language specs.

To support a new language: add a spec module, add its query fragments, and
register it here. Nothing else in the pipeline changes.
"""

from app.knowledge_graph.extract.languages.base import LanguageSpec
from app.knowledge_graph.extract.languages.javascript import JavaScriptSpec
from app.knowledge_graph.extract.languages.python import PythonSpec

_SPECS: dict[str, LanguageSpec] = {
    "python": PythonSpec(),
    # tree-sitter ships JSX with the JavaScript grammar, separately for TS.
    "javascript": JavaScriptSpec("javascript", ("js_core.scm", "jsx.scm")),
    "typescript": JavaScriptSpec("typescript", ("js_core.scm", "ts_extra.scm")),
    "tsx": JavaScriptSpec("tsx", ("js_core.scm", "ts_extra.scm", "jsx.scm")),
}

SUPPORTED_LANGUAGES = tuple(_SPECS)


def spec_for(language: str) -> LanguageSpec | None:
    return _SPECS.get(language)


__all__ = ["LanguageSpec", "SUPPORTED_LANGUAGES", "spec_for"]
