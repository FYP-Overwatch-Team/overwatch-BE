"""Turning a containment tree plus a set of open containers into one view.

The rule is small enough to state in a sentence: **walk down from the
repository, and wherever a folder is open, draw what is inside it underneath
it.** Everything else follows — a shut folder stands for its whole subtree, so
every relationship inside that subtree is drawn against the folder, and
relationships that stay entirely inside it are not drawn at all.

An open folder stays on screen. That is what makes the picture a tree rather
than a set of boxes that changes identity every time you click: you can see
which folder each node came out of, and close it again from where it is.

That rolling-up is what makes the view readable at any zoom. A hundred calls
between two folders become one edge marked ×100; open one of the folders and
the same hundred calls redistribute across its children. Nothing is invented
and nothing is hidden — the weight always adds up.

One container can still hold more than anyone wants to look at, so children
are **paged**: the most connected are drawn and the rest gather behind a
marker that carries their relationships. The marker behaves exactly like a
shut folder, which is why the weight still adds up when it appears.

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
    ViewContainment,
    ViewEdge,
    ViewGraph,
    ViewNode,
    ViewOverflow,
    ViewRequest,
    draw_rank,
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
    walk = _walk(repo_full_name, children_by_parent, request)
    view_edges, edges_truncated = _roll_up(
        repo_full_name, walk.visible, walk.represented_by, edges, request,
    )

    return ViewGraph(
        nodes=tuple(walk.visible),
        edges=view_edges,
        expanded=tuple(sorted(walk.opened, key=lambda node: node.id)),
        overflows=tuple(walk.overflows),
        containment=_containment(walk),
        truncated=walk.truncated or edges_truncated,
    )


def _containment(walk: "_Walk") -> tuple[ViewContainment, ...]:
    """The tree, as lines between nodes that are both on screen.

    A top-level folder's parent is the repository, which is the frame the view
    is drawn in rather than a box within it, so those get no line.
    """
    on_screen = {node.id for node in walk.visible}
    lines = [
        ViewContainment(parent=node.parent_id, child=node.id)
        for node in walk.visible
        if node.parent_id is not None and node.parent_id in on_screen
    ]
    lines.extend(
        ViewContainment(parent=marker.container_id, child=marker.id)
        for marker in walk.overflows
        if marker.container_id in on_screen
    )
    return tuple(sorted(lines, key=lambda line: (line.parent, line.child)))


class _Walk:
    """What one pass down the tree produced."""

    __slots__ = ("visible", "opened", "overflows", "represented_by", "truncated")

    def __init__(self) -> None:
        self.visible: list[ViewNode] = []
        self.opened: list[ViewNode] = []
        self.overflows: list[ViewOverflow] = []
        #: Child id -> the marker that speaks for it on the canvas.
        self.represented_by: dict[str, str] = {}
        self.truncated = False


def _walk(
    repo_full_name: str,
    children_by_parent: Mapping[str, Sequence[ViewNode]],
    request: ViewRequest,
) -> _Walk:
    """Breadth-first from the repository, descending only into open containers.

    An id the client asked to expand but whose own parent is shut is never
    reached, so it is not reported as open — the client can then forget it
    instead of sending it forever. A container whose contents were not
    fetched is drawn shut for the same reason: better one honest box than a
    hole where its children should be.
    """
    requested = set(request.expanded)
    revealed = set(request.revealed)
    names = _names(children_by_parent)

    walk = _Walk()
    queue: deque[str] = deque([repository_id(repo_full_name)])
    descended: set[str] = set(queue)
    placed: set[str] = set()

    while queue:
        parent = queue.popleft()
        drawable: list[ViewNode] = []

        for child in children_by_parent.get(parent, ()):
            if child.id in placed or child.id in descended:
                continue  # a tree, but never trust the shape of stored data

            if (
                child.is_container
                and child.id in requested
                and child.id in children_by_parent
            ):
                # An open folder is drawn *and* descended into. It is never
                # dropped for the node cap: without it the nodes beneath would
                # hang off nothing, and the cap is already bounded by how many
                # folders the fetch will open.
                opened = replace(child, parent_id=parent, expanded=True)
                walk.opened.append(opened)
                walk.visible.append(opened)
                placed.add(child.id)
                descended.add(child.id)
                queue.append(child.id)
                continue

            if request.filters.allows_node(child.label):
                drawable.append(child)

        _draw(walk, parent, drawable, names, placed, request, revealed)

    # Folders before files before definitions, so a level reads like a
    # directory listing. Ties broken by id, so the layout is stable.
    walk.visible.sort(key=lambda node: (draw_rank(node.label), node.id))
    return walk


def _draw(
    walk: _Walk,
    parent: str,
    drawable: list[ViewNode],
    names: Mapping[str, str],
    placed: set[str],
    request: ViewRequest,
    revealed: set[str],
) -> None:
    """Draw one folder's children, gathering the surplus behind a marker.

    Folders come before files, as they would in a directory listing, and
    within each group the most connected come first — so a page that has to
    stop short keeps what is worth looking at rather than whichever names sort
    earliest.
    """
    drawable.sort(key=lambda node: (draw_rank(node.label), -node.degree, node.id))

    limit = len(drawable) if parent in revealed else request.caps.page_size
    shown, surplus = drawable[:limit], drawable[limit:]

    for child in shown:
        if len(walk.visible) >= request.caps.max_nodes:
            walk.truncated = True
            return
        walk.visible.append(replace(child, parent_id=parent))
        placed.add(child.id)

    if not surplus:
        return

    marker = ViewOverflow(
        container_id=parent,
        container_name=names.get(parent, parent),
        shown=len(shown),
        hidden=len(surplus),
    )
    walk.overflows.append(marker)
    for child in surplus:
        walk.represented_by[child.id] = marker.id


def _names(children_by_parent: Mapping[str, Sequence[ViewNode]]) -> dict[str, str]:
    """Every container's name, gathered from wherever it appeared as a child."""
    return {
        node.id: node.name
        for children in children_by_parent.values()
        for node in children
    }


def _roll_up(
    repo_full_name: str,
    visible: Sequence[ViewNode],
    represented_by: Mapping[str, str],
    edges: Sequence[RollupEdge],
    request: ViewRequest,
) -> tuple[tuple[ViewEdge, ...], bool]:
    """Redraw every relationship against whichever node is actually on screen."""
    visible_ids = {node.id for node in visible}
    placement = PlacementCache(repo_full_name, visible_ids, represented_by)

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
