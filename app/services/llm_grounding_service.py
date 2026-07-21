"""Gemini-grounded Q&A and flow-walkthrough over the architecture graph.

Grounding is enforced twice: once in the prompt (the model is told to only
reference the listed nodes) and once after the call (every node id/name the
model returns is resolved and checked against the live graph; anything that
doesn't match is dropped rather than passed through). A broken highlight in
the demo is worse than a slightly incomplete answer.
"""

import json

import structlog

from app.core.exceptions import AppError, NotFoundError
from app.integrations.gemini_client import get_gemini_client
from app.services.graph_service import get_graph_repository

logger = structlog.get_logger("app.llm")

ASK_SYSTEM_INSTRUCTION = (
    "You are an assistant answering questions about a software repository's "
    "architecture graph. Only reference node names and edges provided in the "
    "graph context below. Never invent a service that isn't listed. If the "
    "answer isn't determinable from the graph context, say so plainly."
)

FLOW_SYSTEM_INSTRUCTION = (
    "You are generating an ordered walkthrough of how a request or process "
    "flows through the services in the graph context below. Only reference "
    "node names provided in the graph context. Never invent a service that "
    "isn't listed. Return the steps in the order the flow would actually occur."
)

ASK_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "highlighted_node_names": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "highlighted_node_names"],
}

FLOW_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "step_node_names": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["step_node_names"],
}


def _name_index(nodes: list[dict]) -> dict[str, str]:
    return {n["name"]: n["id"] for n in nodes}


def _format_nodes(nodes: list[dict]) -> str:
    return "\n".join(f"- {n['name']} (kind: {n['kind']})" for n in nodes) or "(no nodes)"


def _format_edges(edges: list[dict], nodes_by_id: dict[str, dict]) -> str:
    lines = []
    for e in edges:
        src = nodes_by_id.get(e["source"], {}).get("name", e["source"])
        dst = nodes_by_id.get(e["target"], {}).get("name", e["target"])
        lines.append(f"- {src} --{e['type']}--> {dst}")
    return "\n".join(lines) or "(no edges)"


async def _load_full_graph_summary(repo_full_name: str) -> dict:
    graph = await get_graph_repository().fetch_graph(repo_full_name)
    if not graph["nodes"]:
        raise NotFoundError("no graph available for this repository yet", error_code="graph_not_ready")
    return graph


async def _load_context_graph(repo_full_name: str, focused_node_id: str | None) -> dict:
    if focused_node_id is None:
        # No focus: a lightweight summary (names + edge types only) keeps the prompt small and cheap.
        return await _load_full_graph_summary(repo_full_name)

    graph_repo = get_graph_repository()
    existing = await graph_repo.existing_node_ids([focused_node_id])
    if not existing:
        raise NotFoundError("focused node not found in graph", error_code="node_not_found")
    return await graph_repo.neighborhood(focused_node_id, hops=2)


def _parse_json_response(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AppError(
            "LLM returned malformed output", error_code="llm_bad_output", status_code=502,
        ) from exc


async def ask(repo_full_name: str, question: str, focused_node_id: str | None = None) -> dict:
    context = await _load_context_graph(repo_full_name, focused_node_id)
    nodes_by_id = {n["id"]: n for n in context["nodes"]}
    name_to_id = _name_index(context["nodes"])

    prompt = (
        f"Graph nodes:\n{_format_nodes(context['nodes'])}\n\n"
        f"Graph edges:\n{_format_edges(context['edges'], nodes_by_id)}\n\n"
        f"Question: {question}"
    )
    raw = await get_gemini_client().generate_json(ASK_SYSTEM_INSTRUCTION, prompt, ASK_RESPONSE_SCHEMA)
    parsed = _parse_json_response(raw)

    claimed_names = parsed.get("highlighted_node_names", [])
    resolved_ids = [name_to_id[name] for name in claimed_names if name in name_to_id]
    dropped = set(claimed_names) - set(name_to_id)
    if dropped:
        logger.warning("llm_hallucinated_nodes_dropped", repo=repo_full_name, names=sorted(dropped))

    return {"answer": parsed.get("answer", ""), "highlighted_node_ids": resolved_ids}


async def flow(repo_full_name: str, question: str) -> dict:
    context = await _load_full_graph_summary(repo_full_name)
    nodes_by_id = {n["id"]: n for n in context["nodes"]}
    name_to_id = _name_index(context["nodes"])

    prompt = (
        f"Graph nodes:\n{_format_nodes(context['nodes'])}\n\n"
        f"Graph edges:\n{_format_edges(context['edges'], nodes_by_id)}\n\n"
        f"Describe the flow for: {question}"
    )
    raw = await get_gemini_client().generate_json(FLOW_SYSTEM_INSTRUCTION, prompt, FLOW_RESPONSE_SCHEMA)
    parsed = _parse_json_response(raw)

    step_names = parsed.get("step_node_names", [])
    ordered_ids = []
    dropped = []
    for name in step_names:
        if name in name_to_id:
            ordered_ids.append(name_to_id[name])
        else:
            dropped.append(name)
    if dropped:
        logger.warning("llm_hallucinated_flow_steps_dropped", repo=repo_full_name, names=dropped)

    return {"step_node_ids": ordered_ids}
