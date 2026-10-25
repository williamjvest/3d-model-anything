"""Checks for kl.py's geometry. Run: python3 -m unittest tools/kavelight/test_kl.py"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kl  # noqa: E402


def room_json(flip_wall=None, drop_wall=None):
    """A 6 x 4 m room as bundle_to_room.py writes it: interior faces, origin = left end seen from
    inside, n into the room. A door on the north wall, a window on the east wall, one table."""
    W, D = 3.0, 2.0
    walls = [  # (a, b) with the room on the right of a -> b
        ([-W, -D], [W, -D], [{'kind': 'door', 'u0': 1.0, 'u1': 1.9, 'v0': 0.0, 'v1': 2.0}]),   # north
        ([W, -D], [W, D], [{'kind': 'window', 'u0': 1.5, 'u1': 2.5, 'v0': 0.9, 'v1': 2.1}]),   # east
        ([W, D], [-W, D], []),                                                                # south
        ([-W, D], [-W, -D], []),                                                              # west
    ]
    out = []
    for i, (a, b, ops) in enumerate(walls):
        if i == drop_wall:
            continue
        length = math.dist(a, b)
        u = [(b[0] - a[0]) / length, 0, (b[1] - a[1]) / length]
        n = [-u[2], 0, u[0]]
        if i == flip_wall:  # what bundle_to_room's 5 cm probe does to a wall drawn just outside the outline
            a, b = b, a
            u = [-u[0], 0, -u[2]]
            n = [-n[0], 0, -n[2]]
            ops = [dict(o, u0=length - o['u1'], u1=length - o['u0']) for o in ops]
        out.append({'index': i, 'a': a, 'b': b, 'height': 2.7, 'origin': [a[0], 0, a[1]], 'u': u, 'n': n,
                    'length': length, 'confidence': 'high', 'openings': ops})
    return {'source': 'synthetic', 'transform': {'theta': 0, 'center': [0, 0], 'floorY': 0,
                                                 'worldToRoom': [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]},
            'polygon': [[-W, -D], [W, -D], [W, D], [-W, D]], 'walls': out,
            'objects': [{'category': 'table', 'center': [1.0, 0.4, 0.5], 'size': [1.2, 0.8, 0.6],
                         'rotY': math.pi / 2, 'front': [1, 0]}], 'photos': []}


class SpecTests(unittest.TestCase):
    def test_room_on_the_right_and_corners_joined(self):
        spec = kl.build_spec(room_json(flip_wall=1), {}, 'Test', 0.1)
        self.assertTrue(any('re-voted' in n for n in spec['notes']))
        for w in spec['walls']:
            u = kl.norm(kl.sub(w['b'], w['a']))
            mid = kl.mul(kl.add(w['a'], w['b']), 0.5)
            self.assertGreater(kl.dot(kl.right_of(u), kl.mul(mid, -1)), 0, w['name'])  # right side faces the centre
            self.assertEqual(set(w['joins']), {'a', 'b'}, w['name'])
        east = next(w for w in spec['walls'] if w['scan_index'] == 1)
        self.assertEqual(east['openings'][0]['at'], [3.0, 0.0])  # window centre survives the flip

    def test_gap_reported(self):
        spec = kl.build_spec(room_json(drop_wall=2), {}, 'Test', 0.1)
        self.assertTrue(any('6.00 m of floor outline has no wall' in n for n in spec['notes']), spec['notes'])

    def test_prop_box(self):
        p = kl.build_spec(room_json(), {}, 'Test', 0.1)['props'][0]
        self.assertEqual(p['center'], [1.0, 0.5])
        self.assertEqual(p['bottom'], 0.0)
        self.assertAlmostEqual(p['heading_degrees'], 90.0)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.spec = kl.build_spec(room_json(), {}, 'Test', 0.2)
        self.steps = kl.plan_steps(self.spec, room_side=-1)

    def test_centrelines_offset_out_and_closed(self):
        walls = {s['args']['name']: s['args'] for s in self.steps if s['args'].get('type') == 'wall'}
        north = walls['Wall 0']
        self.assertEqual(north['from'], [-3.1, -2.1])  # interior face at z = -2, pushed 0.1 out, corners met
        self.assertEqual(north['to'], [3.1, -2.1])
        ends = sorted(tuple(p) for a in walls.values() for p in (a['from'], a['to']))
        self.assertEqual(len(set(ends)), 4)  # every corner shared by exactly two walls

    def test_opening_along_from_centre(self):
        door = next(s['args'] for s in self.steps if s['tool'] == 'add_opening' and s['args']['type'] == 'door')
        # door centre at u = 1.45 from the interior corner (-3, -2) -> x = -1.55; wall centre x = 0
        self.assertAlmostEqual(door['along'], -1.55, places=3)
        self.assertEqual(door['side'], -1)
        self.assertNotIn('sill', door)
        win = next(s['args'] for s in self.steps if s['tool'] == 'add_opening' and s['args']['type'] == 'window')
        self.assertEqual(win['sill'], 0.9)

    def test_floor_and_ceiling(self):
        floor = self.steps[0]['args']
        self.assertEqual(floor['type'], 'floor')
        self.assertEqual(floor['size'], [6.4, 4.4])
        ceiling = next(s['args'] for s in self.steps if s['args'].get('type') == 'ceiling')
        self.assertEqual(ceiling['base_height'], 2.7)


class RunnerTests(unittest.TestCase):
    def test_images_found_in_nested_and_stringified_results(self):
        found = []
        kl.find_images({'content': [{'type': 'text', 'text': 'x'}, {'type': 'image', 'data': 'QUJD', 'mimeType': 'image/png'}]}, found)
        kl.find_images('[{"type":"image","data":"REVG","mimeType":"image/jpeg"}]', found)
        self.assertEqual(found, [('image/png', 'QUJD'), ('image/jpeg', 'REVG')])
        self.assertEqual(kl.strip_images({'a': [{'type': 'image', 'data': 'QUJD'}]}), {'a': [{'type': 'image', 'saved': True}]})


if __name__ == '__main__':
    unittest.main()
