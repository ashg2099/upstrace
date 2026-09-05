from collections import deque

from .manifest import Model, list_nodes


def graph() -> tuple[dict[str, Model], dict[str, list[str]]]:
    """Returns (nodes by name, parents by name).

    Parents ko sirf un nodes tak filter kiya jaata hai jinhe hum jaante hain,
    taaki project ke bahar ki koi dependency walk ko na tode.
    """
    nodes = {n.name: n for n in list_nodes()}
    parents = {
        name: [p for p in node.parents if p in nodes]
        for name, node in nodes.items()
    }
    return nodes, parents


def children_map(parents: dict[str, list[str]]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {name: [] for name in parents}
    for child, ps in parents.items():
        for parent in ps:
            children[parent].append(child)
    return children


def _walk(start: str, edges: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    queue = deque(edges.get(start, []))
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(edges.get(node, []))
    return seen


def ancestors(name: str, parents: dict[str, list[str]]) -> set[str]:
    """Is node ke upar sab kuch."""
    return _walk(name, parents)


def descendants(name: str, parents: dict[str, list[str]]) -> set[str]:
    """Is node ke neeche sab kuch."""
    return _walk(name, children_map(parents))