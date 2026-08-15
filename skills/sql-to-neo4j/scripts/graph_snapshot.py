#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "neo4j>=5.28,<6",
# ]
# ///
"""Export a self-contained HTML snapshot of the graph — the no-Neo4j view.

One file, zero dependencies, works offline and by email: the data is embedded
as JSON and the force-directed renderer is ~150 lines of vanilla canvas JS
(no CDN, no Browser login, no Neo4j needed to view). Read-only against the
database; caps keep the file and the physics honest (highest-degree nodes are
kept when the graph exceeds --max-nodes).

Interactions: drag nodes, drag background to pan, wheel to zoom, hover for
properties, legend colors by label.

Run with:  uv run graph_snapshot.py [--out graph-snapshot.html] [--max-nodes 400]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ENV_FILE = ".neo4j.env"
PALETTE = [
    "#4e9df9", "#22d3ee", "#f472b6", "#a5e3a1", "#f6c177",
    "#c4b5fd", "#fb7185", "#5eead4", "#fde047", "#94a3b8",
]
PROP_VALUE_CAP = 120  # chars per property value in tooltips


def read_env_file(path: str) -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        with Path(path).open() as f:
            for raw in f:
                line = raw.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    info[k] = v
    except FileNotFoundError:
        pass
    return info


TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
  html,body{margin:0;height:100%;background:#0b0b14;color:#e8e8f0;
    font:14px/1.4 system-ui,sans-serif;overflow:hidden}
  #bar{position:fixed;top:0;left:0;right:0;padding:10px 14px;display:flex;
    gap:14px;align-items:center;background:rgba(11,11,20,.85);z-index:2}
  #bar b{font-size:15px}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;
    border:1px solid rgba(255,255,255,.15);border-radius:999px;font-size:12px}
  .dot{width:9px;height:9px;border-radius:50%}
  #tip{position:fixed;display:none;max-width:340px;background:#181826;
    border:1px solid rgba(255,255,255,.2);border-radius:8px;padding:8px 10px;
    font-size:12px;pointer-events:none;z-index:3;word-break:break-word}
  #tip b{color:#fff}
  canvas{display:block}
  #note{position:fixed;bottom:8px;left:14px;font-size:11px;color:#8888a0}
</style></head><body>
<div id="bar"><b>__TITLE__</b><span id="legend"></span>
<span class="chip" id="counts"></span></div>
<div id="tip"></div><div id="note">drag nodes · drag background to pan · wheel to zoom · made by pg-graph-toolkit (offline snapshot)</div>
<canvas id="c"></canvas>
<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
let W, H; const fit = () => { W = cv.width = innerWidth; H = cv.height = innerHeight; };
fit(); addEventListener('resize', fit);
const nodes = DATA.nodes, links = DATA.links;
const byId = {}; nodes.forEach((n, i) => { byId[n.id] = n;
  const a = 2 * Math.PI * i / nodes.length, r = Math.min(W, H) * .35;
  n.x = W / 2 + r * Math.cos(a) + (i % 7) * 3; n.y = H / 2 + r * Math.sin(a) + (i % 5) * 3;
  n.vx = 0; n.vy = 0; });
links.forEach(l => { l.s = byId[l.source]; l.t = byId[l.target]; });
const L = links.filter(l => l.s && l.t);
const deg = {}; L.forEach(l => { deg[l.s.id] = (deg[l.s.id] || 0) + 1; deg[l.t.id] = (deg[l.t.id] || 0) + 1; });
nodes.forEach(n => n.r = Math.min(18, 5 + Math.sqrt(deg[n.id] || 0) * 1.6));
let scale = 1, ox = 0, oy = 0, alpha = 1;
function step() {
  if (alpha > 0.005) {
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy || 1;
      if (d2 < 160000) { const f = 1200 / d2 * alpha; const d = Math.sqrt(d2);
        dx /= d; dy /= d; a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f; } }
    L.forEach(l => { let dx = l.t.x - l.s.x, dy = l.t.y - l.s.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - 90) * 0.02 * alpha;
      dx /= d; dy /= d; l.s.vx += dx * f; l.s.vy += dy * f; l.t.vx -= dx * f; l.t.vy -= dy * f; });
    nodes.forEach(n => { n.vx += (W / 2 - n.x) * 0.0008 * alpha; n.vy += (H / 2 - n.y) * 0.0008 * alpha;
      if (n !== drag.node) { n.x += n.vx *= 0.85; n.y += n.vy *= 0.85; } });
    alpha *= 0.995;
  }
  draw(); requestAnimationFrame(step);
}
function draw() {
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, H);
  ctx.setTransform(scale, 0, 0, scale, ox, oy);
  ctx.strokeStyle = 'rgba(255,255,255,.14)'; ctx.lineWidth = 1 / scale;
  L.forEach(l => { ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke(); });
  nodes.forEach(n => { ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 7);
    ctx.fillStyle = DATA.colors[n.label] || '#94a3b8'; ctx.fill(); });
  if (hover) { ctx.beginPath(); ctx.arc(hover.x, hover.y, hover.r + 2, 0, 7);
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 2 / scale; ctx.stroke(); }
}
const tip = document.getElementById('tip');
let hover = null; const drag = { node: null, bg: false, px: 0, py: 0 };
const toWorld = e => [(e.clientX - ox) / scale, (e.clientY - oy) / scale];
function pick(e) { const [x, y] = toWorld(e);
  return nodes.find(n => (n.x - x) ** 2 + (n.y - y) ** 2 < (n.r + 3) ** 2); }
cv.onmousedown = e => { const n = pick(e);
  if (n) { drag.node = n; } else { drag.bg = true; } drag.px = e.clientX; drag.py = e.clientY; };
addEventListener('mouseup', () => { drag.node = null; drag.bg = false; });
cv.onmousemove = e => {
  if (drag.node) { const [x, y] = toWorld(e); drag.node.x = x; drag.node.y = y; alpha = Math.max(alpha, .1); }
  else if (drag.bg) { ox += e.clientX - drag.px; oy += e.clientY - drag.py; drag.px = e.clientX; drag.py = e.clientY; }
  else { hover = pick(e);
    if (hover) { tip.style.display = 'block';
      tip.style.left = (e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 14) + 'px';
      tip.innerHTML = '<b>:' + hover.label + '</b><br>' + Object.entries(hover.props)
        .map(([k, v]) => k + ' = ' + String(v)).join('<br>');
    } else tip.style.display = 'none'; } };
cv.onwheel = e => { e.preventDefault(); const f = e.deltaY < 0 ? 1.12 : 0.9;
  ox = e.clientX - (e.clientX - ox) * f; oy = e.clientY - (e.clientY - oy) * f; scale *= f; };
document.getElementById('legend').innerHTML = Object.entries(DATA.colors)
  .map(([l, c]) => `<span class="chip"><span class="dot" style="background:${c}"></span>${l} (${DATA.label_counts[l]})</span>`).join(' ');
document.getElementById('counts').textContent =
  DATA.nodes.length + ' nodes · ' + L.length + ' relationships' + (DATA.capped ? ' · sampled' : '');
step();
</script></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--out", default="graph-snapshot.html")
    ap.add_argument("--max-nodes", type=int, default=400)
    ap.add_argument("--max-rels", type=int, default=1500)
    ap.add_argument("--title", default="Graph snapshot")
    args = ap.parse_args()

    info = read_env_file(args.env_file)
    if not info.get("NEO4J_URI"):
        sys.exit(f"error: {args.env_file} missing or incomplete — run graph_context.py first")

    from neo4j import GraphDatabase, RoutingControl
    from neo4j.exceptions import Neo4jError

    try:
        with GraphDatabase.driver(info["NEO4J_URI"], auth=(info["NEO4J_USER"], info["NEO4J_PASSWORD"])) as driver:

            def rows(q: str, **p) -> list[dict]:
                recs, _, _ = driver.execute_query(q, p, database_="neo4j", routing_=RoutingControl.READ)
                return [r.data() for r in recs]

            total = rows("MATCH (n) RETURN count(n) AS c")[0]["c"]
            capped = total > args.max_nodes
            # keep the highest-degree nodes: they carry the structure
            raw_nodes = rows(
                "MATCH (n) WITH n, COUNT { (n)--() } AS d ORDER BY d DESC LIMIT $cap "
                "RETURN elementId(n) AS id, labels(n)[0] AS label, properties(n) AS props",
                cap=args.max_nodes,
            )
            ids = [n["id"] for n in raw_nodes]
            raw_rels = rows(
                "MATCH (a)-[r]->(b) WHERE elementId(a) IN $ids AND elementId(b) IN $ids "
                "RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type LIMIT $cap",
                ids=ids, cap=args.max_rels,
            )
    except Neo4jError as exc:
        sys.exit(f"error: {exc.code}: {exc.message}")

    labels = sorted({n["label"] or "?" for n in raw_nodes})
    colors = {lbl: PALETTE[i % len(PALETTE)] for i, lbl in enumerate(labels)}
    label_counts: dict[str, int] = {}
    nodes = []
    for n in raw_nodes:
        lbl = n["label"] or "?"
        label_counts[lbl] = label_counts.get(lbl, 0) + 1
        props = {
            k: (s[:PROP_VALUE_CAP] + "…" if len(s := str(v)) > PROP_VALUE_CAP else s)
            for k, v in list((n["props"] or {}).items())[:8]
        }
        nodes.append({"id": n["id"], "label": lbl, "props": props})

    data = {
        "nodes": nodes,
        "links": raw_rels,
        "colors": colors,
        "label_counts": label_counts,
        "capped": capped,
        "total_nodes_in_graph": total,
    }
    payload = json.dumps(data).replace("</", "<\\/")
    html = TEMPLATE.replace("__TITLE__", args.title).replace("__DATA__", payload)
    Path(args.out).write_text(html)
    print(
        json.dumps(
            {
                "out": args.out,
                "nodes": len(nodes),
                "relationships": len(raw_rels),
                "total_nodes_in_graph": total,
                "sampled": capped,
                "note": "open the file in any browser — no Neo4j, no internet needed",
            }
        )
    )


if __name__ == "__main__":
    main()
