"""Search quality: which files each way of searching finds for labelled questions.

Every variant is scored on the hits of one run, so they compare like for like:

- bm25: keyword search over the full-text index, as the chat uses it (`search_nodes`)
- meaning: embeddings (`code.similar`), at the production threshold
- rrf: bm25 and meaning fused by reciprocal rank
- weighted w: BM25 and meaning scores blended, w being BM25's share
- routed: `route_of` picks the leading search and `merge_hits` merges both, as the chat does

The decision rules for adopting each change are fixed here, before the numbers are seen.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.core.config import Settings
from app.embeddings import code
from app.knowledge_graph.ports import KnowledgeGraphStore
from app.query.context import search_nodes
from app.query.retrieval import BUDGETS, Route, RouteBudget, merge_hits, route_of
from scripts.eval.cases import CODE_RETRIEVAL, NEGATIVE, RETRIEVAL

#: What the chat uses; the eval process itself runs with no threshold, to sweep it.
THRESHOLD = Settings.model_fields["code_similarity_threshold"].default
MEANING_HITS = 20
FUSION_WEIGHTS = (0.2, 0.4, 0.6)
#: (meaning seeds, keyword seeds) tried for the meaning route
MEANING_BUDGETS = ((2, 4), (3, 2), (4, 2), (4, 1))
#: One question in about 40: a smaller difference is a tie.
TIE = 0.03
SUBSETS = ("plain", "code", "all")


@dataclass
class Case:
    question: str
    wanted: tuple[str, ...]
    #: "plain" English, or naming "code"
    kind: str
    bm25: list[dict] = field(default_factory=list)
    #: rows with their cosine `score`, closest first
    meaning: list[dict] = field(default_factory=list)


async def run(store: KnowledgeGraphStore, repo: str, asked_by: tuple[str, str]) -> dict:
    """Search with every variant for every labelled question; the scores, sweeps and decisions."""
    cases = [Case(question, wanted, "plain") for question, wanted in RETRIEVAL]
    cases += [Case(question, wanted, "code") for question, wanted in CODE_RETRIEVAL]
    for case in cases:
        await _search(store, repo, asked_by, case)
    negatives = [Case(question, (), "negative") for question in NEGATIVE]
    for case in negatives:
        await _search(store, repo, asked_by, case)

    variants = _variants()
    scores = {name: _scores(cases, order) for name, order in variants.items()}
    route_accuracy = sum(route_of(case.question) == _expected_route(case) for case in cases) / len(cases)
    budgets = {
        f"meaning {meaning}, keyword {keyword}": _scores(
            [case for case in cases if case.kind == "plain"],
            lambda case, budget=RouteBudget(keyword, meaning, keyword_snippets=0, meaning_snippets=0): _routed(case, budget),
        )["plain"]
        for meaning, keyword in MEANING_BUDGETS
    }
    return {
        "scores": scores,
        "route_accuracy": round(route_accuracy, 3),
        "meaning_route_budgets": budgets,
        "negatives": _negative_noise(negatives),
        "decisions": _decisions(scores, route_accuracy, budgets),
        "misses": {
            name: [case.question for case in cases if (_rank(variants[name](case), case.wanted) or 99) > 5]
            for name in ("bm25", "meaning", "routed")
        },
    }


async def _search(store: KnowledgeGraphStore, repo: str, asked_by: tuple[str, str], case: Case) -> None:
    case.bm25 = await search_nodes(store, repo, case.question)
    hits = await code.similar(*asked_by, repo, case.question, MEANING_HITS)
    details = await asyncio.gather(*(store.node_detail(repo, node_id) for node_id, _ in hits))
    case.meaning = [
        {"id": detail["id"], **detail["properties"], "score": score}
        for detail, (_, score) in zip(details, hits, strict=True) if detail and score >= THRESHOLD
    ]


def _variants() -> dict[str, Callable[[Case], list[dict]]]:
    """Each variant as the rows it ranks, best first."""
    variants: dict[str, Callable[[Case], list[dict]]] = {
        "bm25": lambda case: case.bm25,
        "meaning": lambda case: case.meaning,
        "rrf": lambda case: _rrf(case.bm25, case.meaning),
        "routed": lambda case: _routed(case, BUDGETS[route_of(case.question)]),
    }
    for weight in FUSION_WEIGHTS:
        variants[f"weighted {weight}"] = lambda case, weight=weight: _weighted(case.bm25, case.meaning, weight)
    return variants


def _routed(case: Case, budget: RouteBudget) -> list[dict]:
    """The order the chat starts from: the leading search's share first."""
    return merge_hits(route_of(case.question), case.bm25, case.meaning, budget.keyword_seeds, budget.meaning_seeds)


def _rrf(*rankings: Sequence[dict]) -> list[dict]:
    """Reciprocal rank fusion: each list adds 1 / (60 + rank) for every node in it."""
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, 1):
            scores[row["id"]] = scores.get(row["id"], 0) + 1 / (60 + rank)
            rows.setdefault(row["id"], row)
    return sorted(rows.values(), key=lambda row: -scores[row["id"]])


def _weighted(bm25: Sequence[dict], meaning: Sequence[dict], weight: float) -> list[dict]:
    """BM25 scores scaled to 0..1 and blended with cosine similarity, `weight` being BM25's share."""
    top = max((row["score"] for row in bm25), default=0) or 1
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for row in bm25:
        scores[row["id"]] = scores.get(row["id"], 0) + weight * row["score"] / top
        rows.setdefault(row["id"], row)
    for row in meaning:
        scores[row["id"]] = scores.get(row["id"], 0) + (1 - weight) * row["score"]
        rows.setdefault(row["id"], row)
    return sorted(rows.values(), key=lambda row: -scores[row["id"]])


def _scores(cases: Sequence[Case], order: Callable[[Case], list[dict]]) -> dict[str, dict[str, float]]:
    """recall@5, recall@10 and MRR for each subset of questions."""
    result = {}
    for subset in SUBSETS:
        ranks = [_rank(order(case), case.wanted) for case in cases if subset in ("all", case.kind)]
        if ranks:
            result[subset] = {
                "recall@5": _recall(ranks, 5), "recall@10": _recall(ranks, 10),
                "mrr": round(sum(1 / rank for rank in ranks if rank) / len(ranks), 3),
            }
    return result


def _rank(rows: Sequence[dict], wanted: tuple[str, ...]) -> int | None:
    """Where the first file that answers the question comes, counting each file once."""
    paths = list(dict.fromkeys(str(row.get("file_path") or row.get("path") or "") for row in rows))
    return next((rank for rank, path in enumerate(paths, 1) if path in wanted), None)


def _recall(ranks: Sequence[int | None], k: int) -> float:
    return round(sum(rank is not None and rank <= k for rank in ranks) / len(ranks), 3)


def _expected_route(case: Case) -> Route:
    return Route.KEYWORD if case.kind == "code" else Route.MEANING


def _negative_noise(negatives: Sequence[Case]) -> dict:
    """How much each search finds for questions nothing in the repository answers."""
    return {
        "questions_with_bm25_hits": sum(bool(case.bm25) for case in negatives),
        "meaning_hits": sum(len(case.meaning) for case in negatives),
    }


def _decisions(scores: dict, route_accuracy: float, budgets: dict) -> dict:
    """The rules agreed before the run, applied to its numbers."""
    def at5(variant: str, subset: str) -> float:
        return scores[variant][subset]["recall@5"]

    best_weighted = max((f"weighted {weight}" for weight in FUSION_WEIGHTS), key=lambda variant: at5(variant, "all"))
    best_budget_score = max(budget["recall@5"] for budget in budgets.values())
    return {
        "routing_adopted": (
            at5("routed", "all") >= max(at5("meaning", "all"), at5("rrf", "all")) - TIE
            and at5("routed", "code") >= at5("meaning", "code") + 0.10
            and at5("routed", "plain") >= at5("meaning", "plain") - TIE
            and route_accuracy >= 0.9
        ),
        "meaning_route_budget": next(
            name for name, budget in budgets.items() if budget["recall@5"] >= best_budget_score - TIE
        ),
        "weighted_beats_routing": all(
            at5(best_weighted, subset) > at5("routed", subset) + 0.05 for subset in ("plain", "code")
        ),
    }
