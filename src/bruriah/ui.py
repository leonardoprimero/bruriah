# Visual DAG Explorer (bruriah ui):
# Launches a lightweight local web server serving an interactive decision
# lineage graph.  Zero external dependencies beyond the stdlib http.server
# and the D3.js library loaded from CDN.
from __future__ import annotations

import json
import sqlite3
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .platform import PlatformPaths


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionNode:
    """A decision in the lineage DAG, ready for rendering."""

    id: str  # document_ref
    label: str  # subject / title
    path: str  # relative_path of the source document
    commit: str  # commit SHA (from metadata)
    author: str
    date: str
    status: str  # "active", "superseded", "deprecated", "amended"
    files: list[str]  # files this decision touches
    body_preview: str  # first ~200 chars of reasoning
    premises: list[dict[str, str]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    has_drift: bool = False


@dataclass(frozen=True)
class LineageEdge:
    """An edge in the lineage DAG."""

    source: str  # predecessor document_ref
    target: str  # successor document_ref
    relation: str  # "supersedes", "deprecates", "amends"


@dataclass(frozen=True)
class DAGData:
    """Complete DAG data for the frontend."""

    nodes: list[DecisionNode]
    edges: list[LineageEdge]


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------


def _extract_subject(text: str) -> str:
    """Extract the first heading from passage text."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return "(untitled)"


def _extract_metadata_field(metadata_json: str, field: str) -> str:
    """Safely extract a field from the JSON metadata column."""
    try:
        data = json.loads(metadata_json)
        return str(data.get(field, ""))
    except (json.JSONDecodeError, TypeError):
        return ""


def _extract_files_from_text(text: str) -> list[str]:
    """Extract files from the '## Files this decision touched' section."""
    files: list[str] = []
    in_files_section = False
    for line in text.splitlines():
        if "files this decision touched" in line.lower():
            in_files_section = True
            continue
        if in_files_section:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped.startswith("- `") and stripped.endswith("`"):
                files.append(stripped[3:-1])
            elif stripped.startswith("- "):
                files.append(stripped[2:])
    return files


def _extract_author_date(text: str) -> tuple[str, str]:
    """Extract author and date from the Bruriah decision header format."""
    author = ""
    date = ""
    for line in text.splitlines():
        if "**Decided:**" in line:
            # Format: **Decided:** YYYY-MM-DD · **Commit:** `sha` · **Author:** Name
            parts = line.split("·")
            for part in parts:
                part = part.strip()
                if "Decided:" in part:
                    date = part.replace("**Decided:**", "").strip()
                elif "Author:" in part:
                    author = part.replace("**Author:**", "").strip()
    return author, date


def build_dag_from_database(database: sqlite3.Connection) -> DAGData:
    """Query the snapshot SQLite database and build the complete DAG data.

    Reads all documents and their lineage relations to produce a graph
    structure the frontend can render.
    """
    nodes: list[DecisionNode] = []
    edges: list[LineageEdge] = []

    # Track which documents are predecessors (superseded/deprecated/amended)
    superseded_refs: set[str] = set()
    deprecated_refs: set[str] = set()
    amended_refs: set[str] = set()

    # 1. Read lineage relations
    has_lineage = False
    try:
        row = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lineage'").fetchone()
        has_lineage = row is not None
    except sqlite3.DatabaseError:
        pass

    if has_lineage:
        try:
            lineage_rows = database.execute(
                "SELECT successor_ref, predecessor_target, predecessor_ref, relation FROM lineage"
            ).fetchall()
            for succ_ref, _pred_target, pred_ref, relation in lineage_rows:
                if pred_ref:
                    edges.append(
                        LineageEdge(
                            source=pred_ref,
                            target=succ_ref,
                            relation=relation,
                        )
                    )
                    if relation == "supersedes":
                        superseded_refs.add(pred_ref)
                    elif relation == "deprecates":
                        deprecated_refs.add(pred_ref)
                    elif relation == "amends":
                        amended_refs.add(pred_ref)
        except sqlite3.DatabaseError:
            pass

    # 2. Read premises and alternatives if counterfactual tables exist
    doc_premises: dict[str, list[dict[str, str]]] = {}
    doc_alts: dict[str, list[dict[str, Any]]] = {}
    doc_has_drift: dict[str, bool] = {}

    has_cf = False
    try:
        row = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='premises'").fetchone()
        has_cf = row is not None
    except sqlite3.DatabaseError:
        pass

    if has_cf:
        try:
            try:
                p_rows = database.execute(
                    "SELECT premise_id, statement, status, invalidated_by, rationale, document_ref, invalidation_document_ref FROM premises"
                ).fetchall()
            except sqlite3.OperationalError:
                p_rows = [
                    (*r, None)
                    for r in database.execute(
                        "SELECT premise_id, statement, status, invalidated_by, rationale, document_ref FROM premises"
                    ).fetchall()
                ]
            for pid, stmt, p_status, inv_by, rationale, d_ref, _inv_d_ref in p_rows:
                if d_ref not in doc_premises:
                    doc_premises[d_ref] = []
                doc_premises[d_ref].append(
                    {
                        "id": pid,
                        "statement": stmt,
                        "status": p_status,
                        "invalidated_by": inv_by or "",
                        "rationale": rationale or "",
                    }
                )
                if p_status == "invalidated":
                    doc_has_drift[d_ref] = True

            a_rows = database.execute(
                "SELECT name, disposition, reason, premises, document_ref FROM alternatives"
            ).fetchall()
            for name, disp, reason, premises_json, d_ref in a_rows:
                if d_ref not in doc_alts:
                    doc_alts[d_ref] = []
                p_list = []
                try:
                    p_list = json.loads(premises_json)
                except (json.JSONDecodeError, TypeError):
                    pass
                doc_alts[d_ref].append(
                    {
                        "name": name,
                        "disposition": disp,
                        "reason": reason,
                        "premises": p_list,
                    }
                )
        except sqlite3.DatabaseError:
            pass

    # 3. Read all documents and their first passage (for subject + body)
    try:
        doc_rows = database.execute("SELECT document_ref, relative_path, metadata FROM documents").fetchall()
    except sqlite3.DatabaseError:
        return DAGData(nodes=[], edges=[])

    for doc_ref, relative_path, metadata_json in doc_rows:
        commit = _extract_metadata_field(metadata_json, "commit")

        # Get the first passage for this document (contains heading + body)
        try:
            passage_rows = database.execute(
                "SELECT text FROM passages WHERE document_ref = ? ORDER BY start_line LIMIT 3",
                (doc_ref,),
            ).fetchall()
        except sqlite3.DatabaseError:
            passage_rows = []

        full_text = "\n".join(row[0] for row in passage_rows) if passage_rows else ""
        subject = _extract_subject(full_text)
        author, date = _extract_author_date(full_text)
        files = _extract_files_from_text(full_text)

        # Body preview: strip the header lines and take first 200 chars
        body_lines = []
        past_header = False
        for line in full_text.splitlines():
            if past_header:
                body_lines.append(line)
            elif line.strip() == "" and body_lines == []:
                continue
            elif not line.startswith("#") and "**Decided:**" not in line:
                past_header = True
                body_lines.append(line)
        body_preview = "\n".join(body_lines)[:200]

        # Determine status
        status = "active"
        if doc_ref in superseded_refs:
            status = "superseded"
        elif doc_ref in deprecated_refs:
            status = "deprecated"
        elif doc_ref in amended_refs:
            status = "amended"

        nodes.append(
            DecisionNode(
                id=doc_ref,
                label=subject,
                path=relative_path,
                commit=commit[:8] if commit else "",
                author=author,
                date=date,
                status=status,
                files=files,
                body_preview=body_preview,
                premises=doc_premises.get(doc_ref, []),
                alternatives=doc_alts.get(doc_ref, []),
                has_drift=doc_has_drift.get(doc_ref, False),
            )
        )

    return DAGData(nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# Embedded HTML/JS frontend
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🏛️ Bruriah — Decision Lineage Explorer</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
         background: #0d1117; color: #c9d1d9; overflow: hidden; }

  #header { position: fixed; top: 0; left: 0; right: 0; z-index: 100;
            background: #161b22; border-bottom: 1px solid #30363d;
            padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  #header h1 { font-size: 18px; font-weight: 600; color: #f0f6fc; }
  #header h1 span { color: #58a6ff; }
  #header .stats { font-size: 13px; color: #8b949e; margin-left: auto; }
  #header .stats b { color: #c9d1d9; }

  #legend { position: fixed; top: 52px; left: 0; right: 0; z-index: 99;
            background: #161b22cc; backdrop-filter: blur(8px);
            padding: 8px 24px; display: flex; gap: 20px; font-size: 12px;
            border-bottom: 1px solid #30363d; }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .legend-dot { width: 12px; height: 12px; border-radius: 50%; }

  #graph { width: 100vw; height: 100vh; padding-top: 90px; }

  #detail { position: fixed; top: 90px; right: 0; width: 400px; bottom: 0;
            background: #161b22; border-left: 1px solid #30363d;
            padding: 20px; overflow-y: auto; transform: translateX(100%);
            transition: transform 0.2s ease; z-index: 50; }
  #detail.open { transform: translateX(0); }
  #detail h2 { font-size: 16px; font-weight: 600; color: #f0f6fc; margin-bottom: 8px; }
  #detail .meta { font-size: 12px; color: #8b949e; margin-bottom: 12px; }
  #detail .meta span { margin-right: 12px; }
  #detail .status-badge { display: inline-block; padding: 2px 8px; border-radius: 12px;
                          font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .status-active { background: #238636; color: #fff; }
  .status-superseded { background: #da3633; color: #fff; }
  .status-deprecated { background: #d29922; color: #fff; }
  .status-amended { background: #58a6ff; color: #fff; }
  #detail .section { margin-top: 16px; }
  #detail .section h3 { font-size: 13px; color: #8b949e; text-transform: uppercase;
                        letter-spacing: 0.5px; margin-bottom: 6px; }
  #detail .files-list { list-style: none; }
  #detail .files-list li { font-size: 13px; font-family: 'SF Mono', 'Fira Code', monospace;
                           padding: 2px 0; color: #79c0ff; }
  #detail .body-preview { font-size: 13px; line-height: 1.5; color: #8b949e;
                          white-space: pre-wrap; }
  #detail .close-btn { position: absolute; top: 12px; right: 12px; background: none;
                       border: none; color: #8b949e; cursor: pointer; font-size: 18px; }
  #detail .close-btn:hover { color: #f0f6fc; }

  .node { cursor: pointer; }
  .node circle { stroke-width: 2; transition: r 0.15s ease; }
  .node:hover circle { r: 14; }
  .node text { font-size: 11px; fill: #c9d1d9; pointer-events: none; }
  .node.drift circle { stroke: #e3b341 !important; stroke-width: 3.5px !important; stroke-dasharray: 4 2; }

  .legend-drift { width: 12px; height: 12px; border-radius: 50%; border: 2px dashed #e3b341; background: #e3b34133; }
  .premise-card { background: #21262d; border-radius: 6px; padding: 8px 10px; margin-bottom: 8px; font-size: 12px; }
  .premise-active { border-left: 3px solid #238636; }
  .premise-invalidated { border-left: 3px solid #da3633; }
  .alt-card { background: #21262d; border-radius: 6px; padding: 8px 10px; margin-bottom: 8px; font-size: 12px; border-left: 3px solid #8b949e; }
  .alert-drift { background: #e3b3411a; border: 1px solid #e3b341; border-radius: 6px; padding: 10px; margin-bottom: 14px; color: #e3b341; font-size: 12px; line-height: 1.4; }

  .link { stroke-opacity: 0.5; fill: none; }
  .link-supersedes { stroke: #da3633; }
  .link-deprecates { stroke: #d29922; }
  .link-amends { stroke: #58a6ff; }

  .link-label { font-size: 9px; fill: #8b949e; pointer-events: none; }

  marker { overflow: visible; }

  #search { position: fixed; top: 90px; left: 24px; z-index: 80;
            background: #21262d; border: 1px solid #30363d; border-radius: 6px;
            padding: 8px 12px; color: #c9d1d9; font-size: 13px; width: 260px;
            outline: none; }
  #search:focus { border-color: #58a6ff; }
  #search::placeholder { color: #484f58; }

  #timeline { position: fixed; bottom: 0; left: 0; right: 0; z-index: 80;
              background: #161b22; border-top: 1px solid #30363d;
              padding: 12px 24px; display: flex; align-items: center; gap: 12px; }
  #timeline .label { font-size: 12px; color: #8b949e; white-space: nowrap; }
  #timeline input[type="range"] { flex: 1; accent-color: #58a6ff; }
  #timeline .date-display { font-size: 12px; color: #c9d1d9; font-family: monospace; min-width: 90px; }
</style>
</head>
<body>
<div id="header">
  <h1>🏛️ <span>Bruriah</span> Decision Lineage Explorer</h1>
  <div class="stats" id="stats"></div>
</div>
<div id="legend">
  <div class="legend-item"><div class="legend-dot" style="background:#238636"></div> Active</div>
  <div class="legend-item"><div class="legend-dot legend-drift"></div> Premise Drift</div>
  <div class="legend-item"><div class="legend-dot" style="background:#da3633"></div> Superseded</div>
  <div class="legend-item"><div class="legend-dot" style="background:#d29922"></div> Deprecated</div>
  <div class="legend-item"><div class="legend-dot" style="background:#58a6ff"></div> Amended</div>
  <div class="legend-item" style="margin-left:auto;color:#484f58">Scroll to zoom · Drag to pan · Click node for details</div>
</div>
<input id="search" type="text" placeholder="🔍 Search decisions, files, authors...">
<svg id="graph"></svg>
<div id="detail">
  <button class="close-btn" onclick="closeDetail()">✕</button>
  <div id="detail-content"></div>
</div>
<div id="timeline">
  <span class="label">Timeline</span>
  <input type="range" id="timeline-slider" min="0" max="100" value="100">
  <span class="date-display" id="timeline-date">All</span>
</div>

<script>
const DATA = __DAG_JSON__;

const statusColor = {
  active: '#238636', superseded: '#da3633',
  deprecated: '#d29922', amended: '#58a6ff'
};

// Stats
const total = DATA.nodes.length;
const active = DATA.nodes.filter(n => n.status === 'active').length;
const stale = total - active;
document.getElementById('stats').innerHTML =
  `<b>${total}</b> decisions · <b>${active}</b> active · <b>${stale}</b> stale · <b>${DATA.edges.length}</b> lineage edges`;

// SVG setup
const svg = d3.select('#graph')
  .attr('width', '100%').attr('height', '100%');
const width = window.innerWidth;
const height = window.innerHeight;

// Arrow markers
const defs = svg.append('defs');
['supersedes', 'deprecates', 'amends'].forEach(rel => {
  defs.append('marker')
    .attr('id', `arrow-${rel}`)
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 22).attr('refY', 0)
    .attr('markerWidth', 8).attr('markerHeight', 8)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', statusColor[rel === 'supersedes' ? 'superseded' :
                               rel === 'deprecates' ? 'deprecated' : 'amended']);
});

const g = svg.append('g');

// Zoom
const zoom = d3.zoom()
  .scaleExtent([0.1, 4])
  .on('zoom', e => g.attr('transform', e.transform));
svg.call(zoom);

// Force simulation
const simulation = d3.forceSimulation(DATA.nodes)
  .force('link', d3.forceLink(DATA.edges).id(d => d.id).distance(180))
  .force('charge', d3.forceManyBody().strength(-400))
  .force('center', d3.forceCenter(width / 2, height / 2))
  .force('collision', d3.forceCollide().radius(40));

// Edges
const link = g.selectAll('.link')
  .data(DATA.edges).join('line')
  .attr('class', d => `link link-${d.relation}`)
  .attr('stroke-width', 2)
  .attr('marker-end', d => `url(#arrow-${d.relation})`);

// Edge labels
const linkLabel = g.selectAll('.link-label')
  .data(DATA.edges).join('text')
  .attr('class', 'link-label')
  .text(d => d.relation);

// Nodes
const node = g.selectAll('.node')
  .data(DATA.nodes).join('g')
  .attr('class', d => 'node' + (d.has_drift ? ' drift' : ''))
  .call(d3.drag()
    .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }))
  .on('click', (e, d) => showDetail(d));

node.append('circle')
  .attr('r', d => d.status === 'active' ? 10 : 8)
  .attr('fill', d => statusColor[d.status] || '#8b949e')
  .attr('stroke', d => d.has_drift ? '#e3b341' : d3.color(statusColor[d.status] || '#8b949e').brighter(0.5));

node.append('text')
  .attr('dx', 16).attr('dy', 4)
  .text(d => d.label.length > 35 ? d.label.slice(0, 35) + '…' : d.label);

// Tick
simulation.on('tick', () => {
  link
    .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
    .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
  linkLabel
    .attr('x', d => (d.source.x + d.target.x) / 2)
    .attr('y', d => (d.source.y + d.target.y) / 2);
  node.attr('transform', d => `translate(${d.x},${d.y})`);
});

// Detail panel
function showDetail(d) {
  const panel = document.getElementById('detail');
  const content = document.getElementById('detail-content');
  const filesHtml = d.files.length > 0
    ? `<ul class="files-list">${d.files.map(f => `<li>${f}</li>`).join('')}</ul>`
    : '<p style="color:#484f58;font-size:13px">No files recorded</p>';

  const driftBanner = d.has_drift
    ? `<div class="alert-drift"><strong>⚠️ Premise Drift Warning:</strong> One or more foundational premises governing this decision have been invalidated downstream.</div>`
    : '';

  let premisesHtml = '<p style="color:#484f58;font-size:13px">No premises recorded</p>';
  if (d.premises && d.premises.length > 0) {
    premisesHtml = d.premises.map(p => `
      <div class="premise-card ${p.status === 'invalidated' ? 'premise-invalidated' : 'premise-active'}">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
          <strong>${p.id}</strong>
          <span class="status-badge ${p.status === 'invalidated' ? 'status-superseded' : 'status-active'}">${p.status}</span>
        </div>
        <div style="color:#c9d1d9">${p.statement}</div>
        ${p.invalidated_by ? `<div style="color:#f85149;margin-top:4px;font-size:11px">Invalidated by: ${p.invalidated_by}</div>` : ''}
      </div>
    `).join('');
  }

  let altsHtml = '<p style="color:#484f58;font-size:13px">No evaluated alternatives</p>';
  if (d.alternatives && d.alternatives.length > 0) {
    altsHtml = d.alternatives.map(a => `
      <div class="alt-card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
          <strong>${a.name}</strong>
          <span class="status-badge status-deprecated">${a.disposition}</span>
        </div>
        <div style="color:#8b949e;margin-bottom:4px">${a.reason}</div>
        ${a.premises && a.premises.length > 0 ? `<div style="color:#58a6ff;font-size:11px">Conditioned on: ${a.premises.join(', ')}</div>` : ''}
      </div>
    `).join('');
  }

  content.innerHTML = `
    <h2>${d.label}</h2>
    ${driftBanner}
    <div class="meta">
      <span class="status-badge status-${d.status}">${d.status}</span>
      ${d.has_drift ? '<span class="status-badge" style="background:#e3b341;color:#0d1117">drift</span>' : ''}
      <span>📝 ${d.commit || 'unknown'}</span>
      <span>👤 ${d.author || 'unknown'}</span>
      <span>📅 ${d.date || 'unknown'}</span>
    </div>
    <div class="section">
      <h3>Source</h3>
      <p style="font-size:13px;font-family:monospace;color:#79c0ff">${d.path}</p>
    </div>
    <div class="section">
      <h3>Premises (${d.premises ? d.premises.length : 0})</h3>
      ${premisesHtml}
    </div>
    <div class="section">
      <h3>Evaluated Alternatives (${d.alternatives ? d.alternatives.length : 0})</h3>
      ${altsHtml}
    </div>
    <div class="section">
      <h3>Governed Files</h3>
      ${filesHtml}
    </div>
    <div class="section">
      <h3>Reasoning</h3>
      <div class="body-preview">${d.body_preview || 'No preview available'}</div>
    </div>
  `;
  panel.classList.add('open');

  // Highlight connected nodes
  const connected = new Set();
  DATA.edges.forEach(e => {
    const sid = typeof e.source === 'object' ? e.source.id : e.source;
    const tid = typeof e.target === 'object' ? e.target.id : e.target;
    if (sid === d.id) connected.add(tid);
    if (tid === d.id) connected.add(sid);
  });
  connected.add(d.id);

  node.select('circle').attr('opacity', n => connected.has(n.id) ? 1 : 0.15);
  node.select('text').attr('opacity', n => connected.has(n.id) ? 1 : 0.15);
  link.attr('opacity', e => {
    const sid = typeof e.source === 'object' ? e.source.id : e.source;
    const tid = typeof e.target === 'object' ? e.target.id : e.target;
    return sid === d.id || tid === d.id ? 1 : 0.05;
  });
}

function closeDetail() {
  document.getElementById('detail').classList.remove('open');
  node.select('circle').attr('opacity', 1);
  node.select('text').attr('opacity', 1);
  link.attr('opacity', 1);
}

// Search
document.getElementById('search').addEventListener('input', function() {
  const q = this.value.toLowerCase();
  if (!q) {
    node.attr('opacity', 1);
    link.attr('opacity', 1);
    return;
  }
  const matches = new Set();
  DATA.nodes.forEach(n => {
    const searchable = [n.label, n.author, n.path, ...n.files].join(' ').toLowerCase();
    if (searchable.includes(q)) matches.add(n.id);
  });
  node.attr('opacity', d => matches.has(d.id) ? 1 : 0.1);
  link.attr('opacity', e => {
    const sid = typeof e.source === 'object' ? e.source.id : e.source;
    const tid = typeof e.target === 'object' ? e.target.id : e.target;
    return matches.has(sid) || matches.has(tid) ? 0.6 : 0.05;
  });
});

// Timeline slider
const dates = DATA.nodes.map(n => n.date).filter(Boolean).sort();
const slider = document.getElementById('timeline-slider');
const dateDisplay = document.getElementById('timeline-date');
slider.addEventListener('input', function() {
  const pct = parseInt(this.value);
  if (pct >= 100 || dates.length === 0) {
    dateDisplay.textContent = 'All';
    node.attr('opacity', 1);
    link.attr('opacity', 1);
    return;
  }
  const idx = Math.floor(pct / 100 * dates.length);
  const cutoff = dates[Math.min(idx, dates.length - 1)];
  dateDisplay.textContent = cutoff;
  const visible = new Set();
  DATA.nodes.forEach(n => { if (!n.date || n.date <= cutoff) visible.add(n.id); });
  node.attr('opacity', d => visible.has(d.id) ? 1 : 0.1);
  link.attr('opacity', e => {
    const sid = typeof e.source === 'object' ? e.source.id : e.source;
    const tid = typeof e.target === 'object' ? e.target.id : e.target;
    return visible.has(sid) && visible.has(tid) ? 0.6 : 0.05;
  });
});

// Keyboard: Escape to close detail
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDetail(); });

// Auto-fit initial view
setTimeout(() => {
  const bounds = g.node().getBBox();
  if (bounds.width > 0 && bounds.height > 0) {
    const padding = 60;
    const scale = Math.min(
      (width - padding * 2) / bounds.width,
      (height - padding * 2) / bounds.height,
      1.5
    );
    const tx = width / 2 - scale * (bounds.x + bounds.width / 2);
    const ty = height / 2 - scale * (bounds.y + bounds.height / 2);
    svg.transition().duration(750).call(
      zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale)
    );
  }
}, 2000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _UIHandler(BaseHTTPRequestHandler):
    """Serves the embedded single-page DAG explorer."""

    dag_json: str = "{}"

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            html = _HTML_TEMPLATE.replace("__DAG_JSON__", self.dag_json)
            self._respond(200, "text/html", html.encode("utf-8"))
        elif self.path == "/api/dag":
            self._respond(200, "application/json", self.dag_json.encode("utf-8"))
        else:
            self._respond(404, "text/plain", b"Not Found")

    def _respond(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default noisy access logs."""


class UIError(ValueError):
    """Raised when the UI server encounters a startup error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def run_ui(
    paths: PlatformPaths,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Launch the Bruriah Decision Lineage Explorer web server.

    Parameters
    ----------
    paths:
        Platform paths pointing to the active snapshot.
    port:
        TCP port to bind (0 = pick an ephemeral port automatically).
    open_browser:
        Whether to open the default browser on startup.
    """
    from .platform import PlatformError, open_snapshot

    try:
        snapshot = open_snapshot(paths)
    except PlatformError as error:
        raise UIError(error.code) from error

    try:
        dag = build_dag_from_database(snapshot.database)
    finally:
        snapshot.database.close()

    dag_json = json.dumps(
        {"nodes": [asdict(n) for n in dag.nodes], "edges": [asdict(e) for e in dag.edges]},
        ensure_ascii=False,
    )

    # Inject DAG data into the handler class
    handler = type("Handler", (_UIHandler,), {"dag_json": dag_json})

    with HTTPServer(("127.0.0.1", port), handler) as server:
        actual_port = server.server_address[1]
        url = f"http://127.0.0.1:{actual_port}"
        print("🏛️  Bruriah Decision Lineage Explorer")
        print(f"   {url}")
        print(f"   {len(dag.nodes)} decisions · {len(dag.edges)} lineage edges")
        print("   Press Ctrl+C to stop\n")

        if open_browser:
            webbrowser.open(url)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
