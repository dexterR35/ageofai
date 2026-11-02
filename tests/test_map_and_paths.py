from engine.pathfinding import astar
from engine.rules import default_rules
from engine.worldmap import WATER, WorldMap, castle_positions, generate_map, mirror

R = default_rules()


def test_map_is_mirrored_fairly():
    m = generate_map(R, seed=3)
    n = m.size
    half = len(m.nodes) // 2
    for a, b in zip(m.nodes[:half], m.nodes[half:]):
        assert a.kind == b.kind
        assert (b.x, b.y) == mirror(n, a.x, a.y, a.size)
    for kind in ("tree", "stone", "iron", "gold", "bush", "animal"):
        assert len(m.by_kind[kind]) % 2 == 0 and m.by_kind[kind], kind


def test_map_is_deterministic():
    a, b = generate_map(R, seed=9), generate_map(R, seed=9)
    assert [(n.kind, n.x, n.y) for n in a.nodes] == [(n.kind, n.x, n.y) for n in b.nodes]
    assert a.terrain == b.terrain


def test_castles_have_space_and_resources_nearby():
    m = generate_map(R, seed=5)
    for cx, cy in castle_positions(R):
        assert m.can_place(cx, cy, 3)
        trees = [t for t in m.by_kind["tree"] if abs(t.x - cx) + abs(t.y - cy) < 30]
        stone = [s for s in m.by_kind["stone"] if abs(s.x - cx) + abs(s.y - cy) < 30]
        assert len(trees) > 20 and stone


def test_astar_goes_around_water():
    m = WorldMap(20)
    for y in range(0, 15):        # a wall of water with a gap at the bottom
        i = m.idx(10, y)
        m.terrain[i] = WATER
        m.blocked[i] = 1
    path = astar(m, (5, 5), [(15, 5)])
    assert path is not None and path[-1] == (15, 5)
    assert any(y >= 15 for _, y in path)
    assert all(not m.blocked[m.idx(x, y)] for x, y in path)


def test_astar_no_corner_cutting_and_unreachable():
    m = WorldMap(10)
    for x, y in [(4, 3), (3, 4)]:
        m.blocked[m.idx(x, y)] = 1
    path = astar(m, (3, 3), [(4, 4)])
    assert path is not None and len(path) > 1           # cannot slip diagonally between the two blocks
    for x in range(10):
        m.blocked[m.idx(x, 6)] = 1
    assert astar(m, (0, 0), [(0, 9)]) is None
