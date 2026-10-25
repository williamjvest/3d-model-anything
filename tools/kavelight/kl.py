#!/usr/bin/env python3
"""Room scan -> KaveLight set, in three steps.

  kl.py spec <scan> -o set.json [--name NAME] [--thickness M] [--room N] [--plan plan.png]
  kl.py plan <set.json> -o steps.json [--room-side N] [--new-scene]
  kl.py run  <steps.json> [--out DIR]     (on a Mac whose OpenCode has the kavelight MCP connected)

<scan> is any of:
  - a Polycam capture's optimized_roomplan.json. It is Apple RoomPlan plus the capture's GPS
    and compass heading, so the sun lands right without a manual north.
  - a 3D Model Anything session bundle (a folder with manifest.json, structure.json, rooms/).
    These carry no GPS or heading: set `site` in set.json by hand.
  - a room.json made by scan-to-threejs's bundle_to_room.py.

set.json is the reviewable middle step. Walls are interior-face lines in set coordinates
(metres, +Y up, floor at 0) with the room on the RIGHT of a -> b. Openings sit at a point on that
line. Props are boxes. Fix it by hand (merge walls, correct heights, add what RoomPlan missed,
such as an open garage door) before `plan`; `extra_steps` passes raw KaveLight calls through.

`plan` turns it into KaveLight MCP calls. Kit walls are centred on their from/to line, so each
interior line is pushed out half a thickness and corners are re-intersected; that keeps the
interior faces where the scan measured them and leaves corner endpoints coincident.

`run` serves steps.json on localhost and has OpenCode's `creative` agent replay it through Code
Mode with a fixed loop (fetch the steps, call tools.kavelight[tool](args), post every result
back). No model writes any geometry. Results and images land in --out.
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_SCRIPTS = HERE.parent.parent / '.agents' / 'skills' / 'scan-to-threejs' / 'scripts'

# RoomPlan object categories -> how a stand-in box is dressed. Bounds only: swap in catalog props
# or build_prop recipes where the shot needs the real shape.
DRESS = {
    'storage': ('metalBrushedSteel', '#6d6f73'),
    'table': ('woodPine', None),
    'chair': ('paintMatteBlack', None),
    'sofa': ('fabricLinen', '#5d5a55'),
    'bed': ('fabricLinen', '#cfc8bb'),
    'refrigerator': ('paintMatteBlack', '#e9e9e6'),
    'stove': ('metalBrushedSteel', None),
    'oven': ('metalBrushedSteel', None),
    'dishwasher': ('metalBrushedSteel', None),
    'washerDryer': ('paintMatteBlack', '#e9e9e6'),
    'sink': ('ceramicGlossWhite', None),
    'toilet': ('ceramicGlossWhite', None),
    'bathtub': ('ceramicGlossWhite', None),
    'television': ('paintMatteBlack', '#111111'),
    'fireplace': ('paintMatteBlack', '#3a3633'),
    'stairs': ('woodOak', None),
}


# ---------------------------------------------------------------- small vector helpers
def sub(a, b):
    return [a[0] - b[0], a[1] - b[1]]


def add(a, b):
    return [a[0] + b[0], a[1] + b[1]]


def mul(a, s):
    return [a[0] * s, a[1] * s]


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def norm(a):
    length = math.hypot(a[0], a[1])
    return [a[0] / length, a[1] / length] if length else [0.0, 0.0]


def right_of(u):
    """Inward normal for a wall whose room is on the right of travel (+Y up): u x up."""
    return [-u[1], u[0]]


def intersect(p, d, q, e):
    """Point where line p + s*d meets line q + t*e, or None when (nearly) parallel."""
    den = cross(d, e)
    if abs(den) < 1e-6:
        return None
    s = cross(sub(q, p), e) / den
    return add(p, mul(d, s))


def r(v, n=4):
    return [round(x, n) for x in v] if isinstance(v, (list, tuple)) else round(v, n)


def inside(poly, x, z):
    hit = False
    for i in range(len(poly)):
        (x1, z1), (x2, z2) = poly[i], poly[(i + 1) % len(poly)]
        if (z1 > z) != (z2 > z) and x < x1 + (z - z1) * (x2 - x1) / (z2 - z1):
            hit = not hit
    return hit


# ---------------------------------------------------------------- scan loading
def normalize_roomplan(d):
    """Polycam's optimized RoomPlan lacks per-surface confidence; bundle_to_room expects it."""
    out = {k: d.get(k) or [] for k in ('walls', 'doors', 'windows', 'openings', 'objects', 'floors')}
    for k in ('walls', 'doors', 'windows', 'openings'):
        for e in out[k]:
            e.setdefault('confidence', {'high': {}})
    for o in out['objects']:
        o.setdefault('confidence', {'high': {}})
    return out


def polycam_site(d):
    site = {}
    lla = d.get('latLonAlt') or {}
    if 'latitude' in lla:
        site.update(latitude=lla['latitude'], longitude=lla['longitude'], altitude_m=lla.get('altitude'))
    ori = d.get('orientation') or {}
    if 'orientation_degrees' in ori:
        # Compass bearing of the capture session's -Z axis, clockwise from true north. Checked
        # 2026-10-25 against satellite imagery of the first capture: the walls it implies land
        # within 3 degrees of the building's roof edges; the opposite sign is 38 degrees off.
        site['session_minus_z_bearing'] = ori['orientation_degrees']
        site['heading_uncertainty_deg'] = ori.get('uncertainty_degrees')
    return site


def run_bundle_to_room(bundle_dir, room_index, out_json):
    cmd = [sys.executable, str(SKILL_SCRIPTS / 'bundle_to_room.py'), str(bundle_dir), '-o', str(out_json)]
    if room_index is not None:
        cmd += ['--room', str(room_index)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode:
        sys.exit(f'bundle_to_room.py failed:\n{res.stdout}\n{res.stderr}')
    return json.load(open(out_json))


def load_scan(path, room_index=None):
    """Returns (room.json dict, site dict, source label)."""
    p = Path(path)
    if p.is_dir() and (p / 'optimized_roomplan.json').exists():
        p = p / 'optimized_roomplan.json'
    if p.is_file():
        d = json.load(open(p))
        walls = d.get('walls') or []
        if walls and 'a' in walls[0]:
            return d, {}, p.name
        if not walls:
            sys.exit(f'{p}: no walls; not a RoomPlan file or room.json')
        site = polycam_site(d)
        with tempfile.TemporaryDirectory() as tmp:
            json.dump(normalize_roomplan(d), open(Path(tmp) / 'structure.json', 'w'))
            room = run_bundle_to_room(tmp, None, Path(tmp) / 'room.json')
        return room, site, f'{p.parent.name}/{p.name}'
    if p.is_dir():
        with tempfile.TemporaryDirectory() as tmp:
            room = run_bundle_to_room(p, room_index, Path(tmp) / 'room.json')
        return room, {}, p.name + ('' if room_index is None else f' room {room_index}')
    sys.exit(f'{path}: not found')


# ---------------------------------------------------------------- spec (scan -> set.json)
def set_minus_z_bearing(room, site):
    """Compass bearing of the set's -Z axis, from the capture's heading and bundle_to_room's turn."""
    if 'session_minus_z_bearing' not in site:
        return None
    m = room['transform']['worldToRoom']  # column-major 4x4: world (session) -> room (set)
    R = [[m[c * 4 + rr] for c in range(3)] for rr in range(3)]
    raw = [-R[2][0], -R[2][1], -R[2][2]]  # R^T (0, 0, -1)
    return (site['session_minus_z_bearing'] + math.degrees(math.atan2(raw[0], -raw[2]))) % 360


def build_spec(room, site, name, thickness, snap=0.3):
    poly = room.get('polygon') or []
    mids = [mul(add(w['a'], w['b']), 0.5) for w in room['walls']]
    centroid = [sum(m[0] for m in mids) / len(mids), sum(m[1] for m in mids) / len(mids)]
    notes = []

    walls = []
    for w in room['walls']:
        a, b = list(w['a']), list(w['b'])
        u = norm(sub(b, a))
        n = right_of(u)
        mid = mul(add(a, b), 0.5)
        # bundle_to_room probes 5 cm, which flips the normal of a wall drawn a hair outside the floor
        # outline. Vote with deeper probes, then fall back to the direction of the room's centroid.
        score = 0
        if poly:
            for dist in (0.25, 0.5, 1.0):
                score += inside(poly, *add(mid, mul(n, dist))) - inside(poly, *add(mid, mul(n, -dist)))
        if score == 0:
            score = 1 if dot(sub(centroid, mid), n) >= 0 else -1
        length = math.dist(a, b)
        ops = []
        for o in w.get('openings', []):
            uc = (o['u0'] + o['u1']) / 2
            at = add(a, mul(u, uc))
            kind = {'opening': 'archway'}.get(o['kind'], o['kind'])
            ops.append({'type': kind, 'at': r(at), 'width': r(o['u1'] - o['u0'], 3),
                        'height': r(o['v1'] - o['v0'], 3), 'sill': r(o['v0'], 3)})
        if score < 0:  # room is on the left: flip so it is on the right of a -> b
            a, b = b, a
            notes.append(f"wall {w['index']}: inward normal re-voted (bundle_to_room had it facing out)")
        walls.append({'name': f"Wall {w['index']}", 'scan_index': w['index'], 'a': a, 'b': b,
                      'height': w['height'], 'thickness': thickness, 'length': r(length, 3),
                      'confidence': w.get('confidence'), 'openings': ops})

    # Close corners: ends of two non-parallel walls within `snap` meet where their lines cross.
    ends = [(i, e) for i in range(len(walls)) for e in ('a', 'b')]
    for k, (i, ei) in enumerate(ends):
        for j, ej in ends[k + 1:]:
            if i == j:
                continue
            wi, wj = walls[i], walls[j]
            if math.dist(wi[ei], wj[ej]) > snap:
                continue
            di, dj = norm(sub(wi['b'], wi['a'])), norm(sub(wj['b'], wj['a']))
            if abs(cross(di, dj)) < 0.5:
                continue
            x = intersect(wi['a'], di, wj['a'], dj)
            if x and math.dist(x, wi[ei]) <= snap and math.dist(x, wj[ej]) <= snap:
                wi[ei], wj[ej] = x, x
                wi.setdefault('joins', {})[ei] = wj['name']
                wj.setdefault('joins', {})[ej] = wi['name']
    for w in walls:
        w['a'], w['b'] = r(w['a']), r(w['b'])
        w['length'] = r(math.dist(w['a'], w['b']), 3)

    gaps = perimeter_gaps(poly, walls)
    for g in gaps:
        notes.append(f"{g['length']:.2f} m of floor outline has no wall, {g['from']} -> {g['to']}: "
                     'an open door or a missed wall? Add it to walls/openings if it is real.')

    props, counts = [], {}
    for o in room.get('objects', []):
        cat = o['category']
        counts[cat] = counts.get(cat, 0) + 1
        w_, h_, d_ = o['size']
        mat, color = DRESS.get(cat, ('paintMatteBlack', '#808080'))
        props.append({'name': f"{cat.capitalize()} {counts[cat]}", 'category': cat,
                      'center': r([o['center'][0], o['center'][2]], 3),
                      'bottom': r(o['center'][1] - h_ / 2, 3),
                      'size': r([w_, h_, d_], 3), 'heading_degrees': r(math.degrees(o['rotY']), 1),
                      'material': mat, 'color': color})

    spec = {
        'format': 'kavelight-set/1',
        'name': name,
        'units': 'metres, +Y up, floor at y = 0; walls are interior faces with the room on the right of a -> b',
        'source': room.get('source'),
        'site': {},
        'floor': None,
        'walls': walls,
        'ceiling': {'height': r(max(w['height'] for w in walls), 3)},
        'props': props,
        'extra_steps': [],
        'notes': notes,
    }
    xs = [p[0] for w in walls for p in (w['a'], w['b'])]
    zs = [p[1] for w in walls for p in (w['a'], w['b'])]
    pad = thickness
    spec['floor'] = {'center': r([(min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2], 3),
                     'size': r([max(xs) - min(xs) + 2 * pad, max(zs) - min(zs) + 2 * pad], 3)}
    if site:
        spec['site'] = {k: v for k, v in site.items() if k != 'session_minus_z_bearing'}
        bearing = set_minus_z_bearing(room, site)
        if bearing is not None:
            spec['site']['minus_z_bearing'] = round(bearing, 2)
    return spec


def perimeter_gaps(poly, walls, tol=0.2, min_len=0.5):
    """Spans of the floor outline with no wall within `tol` of them."""
    gaps = []
    for i in range(len(poly)):
        p, q = poly[i], poly[(i + 1) % len(poly)]
        length = math.dist(p, q)
        if length < min_len:
            continue
        d = norm(sub(q, p))
        covered = []
        for w in walls:
            wd = norm(sub(w['b'], w['a']))
            if abs(cross(d, wd)) > 0.2:
                continue
            if abs(cross(d, sub(w['a'], p))) > tol:
                continue
            s0, s1 = sorted((dot(sub(w['a'], p), d), dot(sub(w['b'], p), d)))
            covered.append((max(0, s0), min(length, s1)))
        covered.sort()
        t = 0.0
        for s0, s1 in covered + [(length, length)]:
            if s0 - t >= min_len:
                gaps.append({'from': r(add(p, mul(d, t)), 2), 'to': r(add(p, mul(d, s0)), 2), 'length': round(s0 - t, 2)})
            t = max(t, s1)
    return gaps


# ---------------------------------------------------------------- plan (set.json -> KaveLight steps)
def centrelines(spec):
    """Kit walls are centred on from -> to: push each interior line out half its thickness, then
    re-intersect the joined corners so the centreline loop closes exactly."""
    walls = {w['name']: w for w in spec['walls']}
    lines = {}
    for w in spec['walls']:
        u = norm(sub(w['b'], w['a']))
        out = mul(right_of(u), -w['thickness'] / 2)
        lines[w['name']] = {'a': add(w['a'], out), 'b': add(w['b'], out), 'u': u}
    for name, w in walls.items():
        for end, other in (w.get('joins') or {}).items():
            if other not in lines:
                continue
            L, M = lines[name], lines[other]
            x = intersect(L['a'], L['u'], M['a'], M['u'])
            if x and math.dist(x, L[end]) < 1.0:
                L[end] = x
    return lines


def plan_steps(spec, room_side=1, new_scene=None):
    steps = []
    if new_scene:
        steps.append({'tool': 'new_scene', 'ref': 'scene', 'args': new_scene})
    mats = spec.get('materials') or {}
    if spec.get('floor'):
        f = spec['floor']
        args = {'type': 'floor', 'name': f.get('name', f"{spec['name']} floor"), 'center': f['center'], 'size': f['size']}
        if f.get('material') or mats.get('floor'):
            args['material'] = f.get('material') or mats['floor']
        steps.append({'tool': 'add_set_piece', 'ref': 'floor', 'args': args})
    lines = centrelines(spec)
    for w in spec['walls']:
        L = lines[w['name']]
        args = {'type': w.get('type', 'wall'), 'name': w['name'], 'from': r(L['a']), 'to': r(L['b']),
                'height': w['height'], 'thickness': w['thickness']}
        if w.get('base_height'):
            args['base_height'] = w['base_height']
        if w.get('material') or mats.get('walls'):
            args['material'] = w.get('material') or mats['walls']
        steps.append({'tool': 'add_set_piece', 'ref': w['name'], 'args': args})
    for w in spec['walls']:
        L = lines[w['name']]
        mid, d = mul(add(L['a'], L['b']), 0.5), norm(sub(L['b'], L['a']))
        for k, o in enumerate(w.get('openings', [])):
            args = {'wall': w['name'], 'type': o['type'], 'along': round(dot(sub(o['at'], mid), d), 3),
                    'width': o['width'], 'height': o['height'], 'side': room_side}
            if o['type'] == 'window' or o.get('sill'):
                args['sill'] = o.get('sill', 0)
            if o.get('name'):
                args['name'] = o['name']
            args.update(o.get('design') or {})
            steps.append({'tool': 'add_opening', 'ref': f"{w['name']} opening {k}", 'args': args})
    c = spec.get('ceiling')
    if c:
        args = {'type': 'ceiling', 'name': c.get('name', f"{spec['name']} ceiling"), 'base_height': c['height']}
        for key in ('center', 'size', 'material'):
            if c.get(key):
                args[key] = c[key]
        steps.append({'tool': 'add_set_piece', 'ref': 'ceiling', 'args': args})
    for p in spec.get('props', []):
        if p.get('skip'):
            continue
        w_, h_, d_ = p['size']
        part = {'name': 'body', 'shape': 'box', 'size': [w_, h_, d_], 'position': [0, h_ / 2, 0],
                'material': p.get('material', 'paintMatteBlack')}
        if p.get('color'):
            part['color'] = p['color']
        if p.get('round'):
            part['round'] = p['round']
        steps.append({'tool': 'build_prop', 'ref': p['name'], 'args': {
            'name': p['name'], 'parts': p.get('parts') or [part],
            'position': [p['center'][0], max(0.0, p.get('bottom', 0)), p['center'][1]],
            'heading_degrees': p.get('heading_degrees', 0), 'snap_to_surface': p.get('snap', True)}})
    steps.extend(spec.get('extra_steps', []))
    return steps


# ---------------------------------------------------------------- run (replay steps through OpenCode)
RUNNER = """const B = "http://127.0.0.1:{port}";
const plan = await (await fetch(B + "/steps")).json();
const out = [];
for (let i = 0; i < plan.steps.length; i++) {{
  const s = plan.steps[i];
  let ok = true, result;
  try {{ result = await tools.kavelight[s.tool](s.args || {{}}); }}
  catch (e) {{ ok = false; result = String((e && e.message) || e); }}
  await fetch(B + "/result", {{ method: "POST", headers: {{ "content-type": "application/json" }},
    body: JSON.stringify({{ i, ref: s.ref || null, tool: s.tool, ok, result }}) }});
  out.push({{ i, ref: s.ref || null, ok }});
  if (!ok && !s.continue_on_error) break;
}}
return JSON.stringify({{ steps: plan.steps.length, ran: out.length, failed: out.filter(o => !o.ok) }});"""

PROMPT = ("Mechanical replay, no judgment needed. Do not load skills, read files or call any other tool. "
          "Call the execute tool exactly once with exactly this code, unchanged:\n\n{code}\n\n"
          "Then reply with only the string it returned.")


def find_images(obj, found):
    if isinstance(obj, dict):
        if obj.get('type') == 'image' and isinstance(obj.get('data'), str):
            found.append((obj.get('mimeType') or 'image/png', obj['data']))
        for v in obj.values():
            find_images(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_images(v, found)
    elif isinstance(obj, str) and obj[:1] in '[{' and '"image"' in obj:
        try:
            find_images(json.loads(obj), found)
        except ValueError:
            pass


def serve(steps, out_dir, port):
    import base64
    out_dir.mkdir(parents=True, exist_ok=True)
    log = open(out_dir / 'results.jsonl', 'a')
    body = json.dumps({'steps': steps}).encode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200 if self.path == '/steps' else 404)
            self.send_header('content-type', 'application/json')
            self.end_headers()
            if self.path == '/steps':
                self.wfile.write(body)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['content-length'])))
            imgs = []
            find_images(data.get('result'), imgs)
            names = []
            for k, (mime, b64) in enumerate(imgs):
                ext = 'jpg' if 'jpeg' in mime else 'png'
                safe = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in str(data.get('ref') or data['tool']))
                fn = out_dir / f"{data['i']:03d}_{safe}_{k}.{ext}"
                fn.write_bytes(base64.b64decode(b64))
                names.append(fn.name)
            text = data.get('result')
            if imgs:
                text = strip_images(text)
            log.write(json.dumps({'i': data['i'], 'ref': data.get('ref'), 'tool': data['tool'], 'ok': data['ok'],
                                  'images': names, 'result': text}) + '\n')
            log.flush()
            self.send_response(204)
            self.end_headers()

    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def strip_images(obj):
    if isinstance(obj, dict):
        if obj.get('type') == 'image':
            return {'type': 'image', 'saved': True}
        return {k: strip_images(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_images(v) for v in obj]
    if isinstance(obj, str) and obj[:1] in '[{' and '"image"' in obj:
        try:
            return strip_images(json.loads(obj))
        except ValueError:
            return obj
    return obj


def run(steps, out_dir, port, workdir, agent, opencode, title):
    srv = serve(steps, out_dir, port)
    code = RUNNER.format(port=port)
    cmd = [opencode, 'run', '--agent', agent, '--title', title, PROMPT.format(code=code)]
    print(f'replaying {len(steps)} steps via {agent} in {workdir} …', flush=True)
    # opencode2 takes its location from $PWD, not the process cwd; a stale PWD silently runs the
    # session somewhere the creative MCPs aren't scoped on ("Unknown tool 'kavelight.…'").
    env = dict(os.environ, PWD=str(workdir))
    res = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True)
    srv.shutdown()
    (out_dir / 'opencode.log').write_text(res.stdout + '\n--- stderr ---\n' + res.stderr)
    rows = [json.loads(line) for line in open(out_dir / 'results.jsonl')]
    rows = [row for row in rows if row['i'] < len(steps)]
    failed = [row for row in rows if not row['ok']]
    print(f'{len(rows)}/{len(steps)} steps ran, {len(failed)} failed; results in {out_dir}/results.jsonl')
    for row in failed:
        print(f"  step {row['i']} {row['tool']} ({row['ref']}): {str(row['result'])[:400]}")
    if not rows:
        print('nothing ran; see opencode.log', file=sys.stderr)
    return 0 if rows and not failed and len(rows) == len(steps) else 1


# ---------------------------------------------------------------- plan picture
def draw(spec, path, ppm=90):
    from PIL import Image, ImageDraw
    lines = centrelines(spec)
    pts = [p for w in spec['walls'] for p in (w['a'], w['b'])]
    minx, maxx = min(p[0] for p in pts) - 0.8, max(p[0] for p in pts) + 0.8
    minz, maxz = min(p[1] for p in pts) - 0.8, max(p[1] for p in pts) + 0.8
    img = Image.new('RGB', (int((maxx - minx) * ppm), int((maxz - minz) * ppm)), (20, 20, 24))
    d = ImageDraw.Draw(img)
    P = lambda x, z: ((x - minx) * ppm, (z - minz) * ppm)
    for g in range(math.floor(minx), math.ceil(maxx) + 1):
        d.line([P(g, minz), P(g, maxz)], fill=(48, 48, 60))
        d.text(P(g + 0.03, minz + 0.05), f'x{g}', fill=(0, 190, 255))
    for g in range(math.floor(minz), math.ceil(maxz) + 1):
        d.line([P(minx, g), P(maxx, g)], fill=(48, 48, 60))
        d.text(P(minx + 0.05, g + 0.02), f'z{g}', fill=(255, 200, 0))
    for p in spec.get('props', []):
        if p.get('skip'):
            continue
        cx, cz = p['center']
        hw, hd, a = p['size'][0] / 2, p['size'][2] / 2, math.radians(p.get('heading_degrees', 0))
        cs = [(cx + math.cos(a) * sx * hw + math.sin(a) * sz * hd, cz - math.sin(a) * sx * hw + math.cos(a) * sz * hd)
              for sx, sz in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        d.polygon([P(*c) for c in cs], outline=(220, 160, 60), fill=(60, 48, 28))
        d.text(P(cx - 0.3, cz), p['name'], fill=(255, 210, 130))
    for w in spec['walls']:
        L = lines[w['name']]
        u = norm(sub(L['b'], L['a']))
        n = right_of(u)
        t = w['thickness'] / 2
        quad = [add(L['a'], mul(n, t)), add(L['b'], mul(n, t)), add(L['b'], mul(n, -t)), add(L['a'], mul(n, -t))]
        d.polygon([P(*q) for q in quad], fill=(200, 200, 205))
        m = mul(add(L['a'], L['b']), 0.5)
        lab = add(m, mul(n, 0.35))
        d.text(P(*lab), f"{w['name']} h{w['height']:.2f}", fill=(255, 255, 255))
        for o in w.get('openings', []):
            mid = mul(add(L['a'], L['b']), 0.5)
            along = dot(sub(o['at'], mid), u)
            c = add(mid, mul(u, along))
            p0, p1 = add(c, mul(u, -o['width'] / 2)), add(c, mul(u, o['width'] / 2))
            col = {'door': (80, 190, 255), 'window': (120, 255, 150), 'archway': (255, 120, 220)}.get(o['type'], (255, 255, 0))
            d.line([P(*p0), P(*p1)], fill=col, width=7)
            d.text(P(*add(c, mul(n, 0.6))), o.get('name') or o['type'], fill=col)
    if spec['site'].get('minus_z_bearing') is not None:
        b = math.radians(spec['site']['minus_z_bearing'])
        # north in set coordinates: -Z points at bearing b, +X at b + 90
        nx, nz = -math.sin(b), -math.cos(b)
        o = (maxx - 0.6, minz + 0.6)
        d.line([P(*o), P(o[0] + nx * 0.45, o[1] + nz * 0.45)], fill=(255, 80, 80), width=4)
        d.text(P(o[0] + nx * 0.55, o[1] + nz * 0.55), 'N', fill=(255, 80, 80))
    img.save(path)


# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    a = sp.add_parser('spec', help='scan -> set.json')
    a.add_argument('scan')
    a.add_argument('-o', required=True)
    a.add_argument('--name', default='Scanned set')
    a.add_argument('--thickness', type=float, default=0.12)
    a.add_argument('--room', type=int, default=None, help='3DMA bundle room index')
    a.add_argument('--plan', help='also draw the set to this PNG')
    b = sp.add_parser('plan', help='set.json -> steps.json')
    b.add_argument('spec')
    b.add_argument('-o', required=True)
    b.add_argument('--room-side', type=int, default=1, help="add_opening side value meaning 'room on the right of from -> to'")
    b.add_argument('--new-scene', help='JSON for new_scene first, e.g. {"start":"empty","location":"GARAGE","setting":"INT"}')
    b.add_argument('--draw', help='also draw the set to this PNG')
    b.add_argument('--keep-going', action='store_true',
                   help='mark every step continue_on_error (each is its own undo step; report all failures at once)')
    c = sp.add_parser('run', help='replay steps.json in KaveLight through OpenCode')
    c.add_argument('steps')
    c.add_argument('--out', default=None)
    c.add_argument('--port', type=int, default=8765)
    c.add_argument('--workdir', default=os.environ.get('KL_WORKDIR', os.getcwd()),
                   help='directory whose OpenCode config has the kavelight MCP (env KL_WORKDIR)')
    c.add_argument('--agent', default=os.environ.get('KL_AGENT', 'creative'))
    c.add_argument('--opencode', default=None,
                   help='OpenCode CLI with Code Mode and the kavelight MCP (default: opencode2 on PATH or in /opt/homebrew/bin)')
    c.add_argument('--title', default=None)
    args = ap.parse_args()

    if args.cmd == 'spec':
        room, site, label = load_scan(args.scan, args.room)
        spec = build_spec(room, site, args.name, args.thickness)
        spec['source'] = label
        json.dump(spec, open(args.o, 'w'), indent=1)
        xs = [p[0] for w in spec['walls'] for p in (w['a'], w['b'])]
        zs = [p[1] for w in spec['walls'] for p in (w['a'], w['b'])]
        print(f"{spec['name']}: {max(xs) - min(xs):.2f} x {max(zs) - min(zs):.2f} m, {len(spec['walls'])} walls, "
              f"{sum(len(w['openings']) for w in spec['walls'])} openings, {len(spec['props'])} props -> {args.o}")
        for note in spec['notes']:
            print('  note:', note)
        if spec['site']:
            print('  site:', spec['site'])
        if args.plan:
            draw(spec, args.plan)
            print('  plan:', args.plan)
    elif args.cmd == 'plan':
        spec = json.load(open(args.spec))
        steps = plan_steps(spec, args.room_side, json.loads(args.new_scene) if args.new_scene else None)
        if args.keep_going:
            for s in steps:
                s['continue_on_error'] = True
        json.dump({'format': 'kavelight-steps/1', 'set': spec['name'], 'steps': steps}, open(args.o, 'w'), indent=1)
        tally = {}
        for s in steps:
            tally[s['tool']] = tally.get(s['tool'], 0) + 1
        print(f"{len(steps)} steps -> {args.o}: " + ', '.join(f'{k} x{v}' for k, v in tally.items()))
        if args.draw:
            draw(spec, args.draw)
            print('  plan:', args.draw)
    else:
        data = json.load(open(args.steps))
        steps = data['steps'] if isinstance(data, dict) else data
        out = Path(args.out or Path(args.steps).with_suffix('').as_posix() + '-run')
        title = args.title or f"KaveLight replay: {data.get('set', Path(args.steps).stem) if isinstance(data, dict) else Path(args.steps).stem}"
        # Never fall back to a bare `opencode`: a v1 CLI has no Code Mode, so every step would fail.
        oc = args.opencode or shutil.which('opencode2') or next(
            (p for p in ('/opt/homebrew/bin/opencode2', '/usr/local/bin/opencode2') if os.path.exists(p)), None)
        if not oc:
            sys.exit('opencode2 not found; pass --opencode')
        sys.exit(run(steps, out, args.port, args.workdir, args.agent, oc, title))


if __name__ == '__main__':
    main()
