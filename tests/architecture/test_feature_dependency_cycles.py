import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# Strongly connected feature groups that are temporarily accepted.  The list
# is empty: cross-feature dependencies may form a DAG, but must not form a
# cycle.  Shared capabilities flow through foundation-level seams (e.g.
# foundation.cluster_port) wired by the owning feature or composition root.
FROZEN_CYCLIC_COMPONENTS: set[tuple[str, ...]] = set()


def cross_feature_imports() -> set[tuple[str, str]]:
    """Return the set of directed ``source -> target`` feature imports.

    Both top-level and function-level (deferred) imports count: a deferred
    import still couples the two features at runtime, it only hides the
    cycle from import time.
    """
    edges: set[tuple[str, str]] = set()
    for path in (ROOT / 'features').rglob('*.py'):
        relative = str(path.relative_to(ROOT))
        if '/tests/' in relative or '__pycache__' in relative:
            continue
        feature = path.relative_to(ROOT / 'features').parts[0]
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                if not module.startswith('features.'):
                    continue
                parts = module.split('.')
                if len(parts) >= 2 and parts[1] != feature:
                    edges.add((feature, parts[1]))
    return edges


def cyclic_components(
    edges: set[tuple[str, str]],
) -> set[tuple[str, ...]]:
    """Return all strongly connected feature groups containing a cycle."""
    graph: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: set[tuple[str, ...]] = set()

    def visit(feature: str) -> None:
        nonlocal index
        indices[feature] = index
        lowlinks[feature] = index
        index += 1
        stack.append(feature)
        on_stack.add(feature)

        for dependency in graph[feature]:
            if dependency not in indices:
                visit(dependency)
                lowlinks[feature] = min(
                    lowlinks[feature], lowlinks[dependency]
                )
            elif dependency in on_stack:
                lowlinks[feature] = min(
                    lowlinks[feature], indices[dependency]
                )

        if lowlinks[feature] != indices[feature]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == feature:
                break
        if len(component) > 1:
            components.add(tuple(sorted(component)))

    for feature in sorted(graph):
        if feature not in indices:
            visit(feature)
    return components


class FeatureDependencyCycleTests(unittest.TestCase):
    def test_no_new_feature_dependency_cycles(self):
        components = cyclic_components(cross_feature_imports())
        unexpected = components - FROZEN_CYCLIC_COMPONENTS
        self.assertEqual(
            sorted(unexpected),
            [],
            'Feature dependency cycle detected. Wire a shared capability '
            'through the composition root (bootstrap) or a foundation-level '
            'seam, or explicitly document the strongly connected group in '
            'FROZEN_CYCLIC_COMPONENTS while it is being decoupled.',
        )

    def test_frozen_cycles_are_still_present(self):
        components = cyclic_components(cross_feature_imports())
        stale = FROZEN_CYCLIC_COMPONENTS - components
        self.assertEqual(
            sorted(stale),
            [],
            'A frozen feature cycle no longer exists — remove it from '
            'FROZEN_CYCLIC_COMPONENTS so the ratchet keeps moving.',
        )

    def test_cycle_detector_catches_longer_cycles(self):
        edges = {('alpha', 'beta'), ('beta', 'gamma'), ('gamma', 'alpha')}
        self.assertEqual(
            cyclic_components(edges),
            {('alpha', 'beta', 'gamma')},
        )

    def test_cycle_detector_accepts_acyclic_graph(self):
        edges = {('alpha', 'beta'), ('alpha', 'gamma'), ('beta', 'gamma')}
        self.assertEqual(cyclic_components(edges), set())
