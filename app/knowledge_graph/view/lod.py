"""Turning a containment tree plus a set of open containers into one view.

The rule is small enough to state in a sentence: **walk down from the
repository, and wherever a container is open, draw its children instead of
it.** Everything else follows — a shut container stands for its whole subtree,
so every relationship inside that subtree is drawn against the container, and
relationships that stay entirely inside it are not drawn at all.

That rolling-up is what makes the view readable at any zoom. A hundred calls
between two folders become one edge marked ×100; open one of the folders and
the same hundred calls redistribute across its children. Nothing is invented
and nothing is hidden — the weight always adds up.

Pure: no database, no clock, no I/O. The service layer fetches, this decides.
"""

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import replace

from app.knowledge_graph.build.ids import repository_id
from app.knowledge_graph.build.model import EdgeType, NodeLabel
from app.knowledge_graph.view.hierarchy import PlacementCache
from app.knowledge_graph.view.model import (
    Granularity,
    OPENS_THROUGH,
    RollupEdge,
    ViewEdge,
    ViewGraph,
    ViewNode,
    ViewRequest,
)


def opens_automatically(label: NodeLabel, granularity: Granularity | None) -> bool:
    """Whether `expand_to` should open a container of this label without being asked."""
    if granularity is None:
        return False
    return label in OPENS_THROUGH[granularity]


def project(
    repo_full_name: str,
    children_by_parent: Mapping[str, Sequence[ViewNode]],
    edges: Sequence[RollupEdge],
    request: ViewRequest,
) -> ViewGraph:
    """Render one view of the graph.

    `request.expanded` is taken as final: resolving a granularity into a
    concrete set of open containers is the service's job, because it is the
    service that can fetch their contents.
    """
    visible, opened, nodes_truncated = _walk(repo_full_name, children_by_parent, request)
    view_edges, edges_truncated = _roll_up(repo_full_name, visible, edges, request)

    return ViewGraph(
        nodes=tuple(visible),
        edges=view_edges,
        expanded=tuple(sorted(opened, key=lambda node: node.id)),
        truncated=nodes_truncated or edges_truncated,
    )


def _walk(
    repo_full_name: str,
    children_by_parent: Mapping[str, Sequence[ViewNode]],
    request: ViewRequest,
) -> tuple[list[ViewNode], list[ViewNode], bool]:
    """Breadth-first from the repository, descending only into open containers.

    An id the client asked to expand but whose own parent is shut is never
    reached, so it is not reported as open — the client can then forget it
    instead of sending it forever. A container whose contents were not
    fetched is drawn shut for the same reason: better one honest box than a
    hole where its children should be.
    """
    requested = set(request.expanded)
    queue: deque[str] = deque([repository_id(repo_full_name)])
    descended: set[str] = set(queue)
    opened: list[ViewNode] = []
    visible: list[ViewNode] = []
    placed: set[str] = set()
    truncated = False

    while queue:
        parent = queue.popleft()
        for child in children_by_parent.get(parent, ()):
            if child.id in placed or child.id in descended:
                continue  # a tree, but never trust the shape of stored data

            open_it = (
                child.is_container
                and child.id in requested
                and child.id in children_by_parent
            )
            if open_it:
                opened.append(replace(child, parent_id=parent))
                descended.add(child.id)
                queue.append(child.id)
                continue

            if not request.filters.allows_node(child.label):
                continue
            if len(visible) >= request.caps.max_nodes:
                truncated = True
                continue
            visible.append(replace(child, parent_id=parent))
            placed.add(child.id)

    visible.sort(key=lambda node: node.id)
    return visible, opened, truncated


def _roll_up(
    repo_full_name: str,
    visible: Sequence[ViewNode],
    edges: Sequence[RollupEdge],
    request: ViewRequest,
) -> tuple[tuple[ViewEdge, ...], bool]:
    """Redraw every relationship against whichever node is actually on screen."""
    visible_ids = {node.id for node in visible}
    placement = PlacementCache(repo_full_name, visible_ids)

    weights: dict[tuple[str, str, EdgeType], int] = {}
    collapsed: set[tuple[str, str, EdgeType]] = set()
    truncated = False

    for edge in edges:
        if not request.filters.allows_edge(edge.type):
            continue

        source = placement.place(edge.source, edge.source_label)
        target = placement.place(edge.target, edge.target_label)
        if source is None or target is None:
            continue  # a filter hid the whole branch this edge lands in
        if source == target:
            continue  # entirely inside one shut container: nothing to draw

        key = (source, target, edge.type)
        if key not in weights and len(weights) >= request.caps.max_edges:
            truncated = True
            continue

        weights[key] = weights.get(key, 0) + max(edge.weight, 1)
        if source != edge.source or target != edge.target:
            collapsed.add(key)

    rendered = tuple(
        ViewEdge(
            source=source,
            target=target,
            type=edge_type,
            weight=weight,
            collapsed=(source, target, edge_type) in collapsed,
        )
        for (source, target, edge_type), weight in sorted(
            weights.items(), key=lambda item: (item[0][0], item[0][1], str(item[0][2])),
        )
    )
    return rendered, truncated
