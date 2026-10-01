import streamlit as st
import streamlit.components.v1 as components
import re
import json
import hashlib

st.set_page_config(layout="wide", page_title="Ultimate Mermaid Architect v3.1")

# --- UI Header ---
st.title("⚡ Ultimate Mermaid Architect & Designer — v3.1")
st.markdown("Design, drag-and-drop, style, and compile your systems workflows dynamically — **with live styling & subgraph-safe shape rewriting**.")

# --- Rich Default Mermaid Code ---
default_code = """flowchart TD
    subgraph client_zone ["Client & UI Layer"]
        UI["Dashboard UI"]
        Console["Attack Console"]
    end

    subgraph core_sec ["Security & Core Processing"]
        Broker["KUKSA Databroker"]
        Orchestrator["Agent Orchestrator"]
    end

    UI <-->|gRPC Bidirectional| Broker
    Console ==>|High-Priority Payload| Broker
    Broker -.->|Async Event| Orchestrator
    Orchestrator --- Output["Terminal Output"]
"""

# --- Sidebar: Extended Visual Styling Controls ---
st.sidebar.header("🎨 Canvas & Node Stylist")

# Node Shape & Geometry (rectangle = safe identity default)
node_shape = st.sidebar.selectbox("Default Node Shape", [
    "rectangle (Sharp Box - Safe Default)",
    "roundrectangle (Rounded Box)",
    "ellipse (Oval)",
    "diamond (Rhombus)",
    "hexagon (Hexagon)"
])
_raw_shape = node_shape.split(" ")[0]

# Map UI label -> valid Cytoscape shape identifier
SHAPE_MAP_CYTOSCAPE = {
    "roundrectangle": "round-rectangle",
    "rectangle":      "rectangle",
    "ellipse":        "ellipse",
    "diamond":        "diamond",
    "hexagon":        "hexagon",
}
node_shape_value = SHAPE_MAP_CYTOSCAPE.get(_raw_shape, "rectangle")

node_padding = st.sidebar.slider("Node Padding / Spacing (px)", 10, 50, 16)

# Typography Controls
font_family = st.sidebar.selectbox("Font Family", ["sans-serif", "monospace", "serif", "cursive"])
font_size = st.sidebar.slider("Node Font Size (px)", 10, 24, 12)
subgraph_font_size = st.sidebar.slider("Subgraph Font Size (px)", 12, 32, 16)

# Palette Settings
st.sidebar.subheader("🌈 Color Customizer")
bg_color = st.sidebar.color_picker("Node Fill Color", "#e8f0fe")
stroke_color = st.sidebar.color_picker("Node Border Color", "#1a73e8")
text_color = st.sidebar.color_picker("Node Text Color", "#0d47a1")
stroke_width = st.sidebar.slider("Border Width (px)", 1, 5, 2)

subgraph_bg = st.sidebar.color_picker("Subgraph Fill Color", "#fafafa")
subgraph_border = st.sidebar.color_picker("Subgraph Border Color", "#cccccc")

# --- Main Editor ---
st.subheader("📝 Mermaid Source Editor")
mermaid_input = st.text_area("Edit your Mermaid flowchart code below:", value=default_code, height=320)

# ============================================================
# SHAPE TRANSFORMER — subgraph-safe, line-aware
# ============================================================
def apply_shape_to_mermaid(text, shape_key):
    """Rewrite ["X"] node syntax to match selected shape — but ONLY on node-definition lines."""
    if shape_key == "rectangle":
        return text  # identity — no work needed

    shape_wrappers = {
        "roundrectangle": r'\1("\2")',
        "ellipse":        r'\1(["\2"])',
        "diamond":        r'\1{"\2"}',
        "hexagon":        r'\1{{"\2"}}',
    }
    replacement = shape_wrappers.get(shape_key)
    if not replacement:
        return text

    # Lines we must NEVER touch
    skip_prefixes = ("subgraph", "end", "style", "class", "classDef", "%%", "flowchart", "graph")

    out_lines = []
    node_def_pattern = re.compile(r'^(\s*\w+)\["([^"]+)"\]\s*$')

    for line in text.split('\n'):
        stripped = line.lstrip()
        if any(stripped.startswith(p) for p in skip_prefixes):
            out_lines.append(line)
            continue

        # Standalone node definition
        if node_def_pattern.match(line):
            out_lines.append(re.sub(r'^(\s*\w+)\["([^"]+)"\]\s*$', replacement, line))
            continue

        # Edge lines with inline node defs: `A["foo"] --> B["bar"]`
        rewritten = re.sub(r'(\w+)\["([^"]+)"\]', replacement, line)
        out_lines.append(rewritten)

    return '\n'.join(out_lines)

# ============================================================
# MERMAID PARSER — extract nodes, edges, subgraphs for Cytoscape
# ============================================================
def parse_mermaid(text):
    nodes, edges, subgraphs = [], [], {}
    active_subgraphs = []

    subgraph_start_pattern = re.compile(r'^subgraph\s+(\w+)(?:\s+\["([^"]+)"\])?', re.IGNORECASE)
    node_pattern = re.compile(r'^(\w+)(?:\["([^"]+)"\]|\("([^"]+)"\)|\{"([^"]+)"\}|\{\{"([^"]+)"\}\})?$')
    edge_p1 = re.compile(r'^(\w+)\s*(<-->|-->|---|==>|-\.->|<==>)\s*(?:\|([^|]+)\|)?\s*(\w+)')
    edge_p2 = re.compile(r'^(\w+)\s*(<-->|-->|---|==>|-\.->|<==>)\s*(\w+)')

    seen_nodes = set()

    for raw in text.split('\n'):
        line = raw.strip()
        if not line or line.startswith('%%') or line.lower().startswith(('flowchart', 'graph')):
            continue

        sg = subgraph_start_pattern.match(line)
        if sg:
            sid, slabel = sg.group(1), sg.group(2) or sg.group(1)
            subgraphs[sid] = slabel
            active_subgraphs.append(sid)
            if sid not in seen_nodes:
                nodes.append({"id": sid, "label": slabel, "parent": active_subgraphs[-2] if len(active_subgraphs) > 1 else None, "is_parent": True})
                seen_nodes.add(sid)
            continue

        if line.lower() == 'end':
            if active_subgraphs:
                active_subgraphs.pop()
            continue

        edge_match = edge_p1.match(line) or edge_p2.match(line)
        if edge_match:
            groups = edge_match.groups()
            src, arrow, tgt = groups[0], groups[1], groups[-1]
            label = groups[2] if len(groups) == 4 else ""
            for nid in (src, tgt):
                if nid not in seen_nodes:
                    nodes.append({"id": nid, "label": nid, "parent": active_subgraphs[-1] if active_subgraphs else None, "is_parent": False})
                    seen_nodes.add(nid)
            edges.append({"source": src, "target": tgt, "label": label or "", "arrow": arrow})
            continue

        nm = node_pattern.match(line)
        if nm:
            nid = nm.group(1)
            label = next((g for g in nm.groups()[1:] if g), nid)
            if nid not in seen_nodes:
                nodes.append({"id": nid, "label": label, "parent": active_subgraphs[-1] if active_subgraphs else None, "is_parent": False})
                seen_nodes.add(nid)

    return nodes, edges, subgraphs

# ============================================================
# COMPILE STYLED CODE (Mermaid output)
# ============================================================
mermaid_input_shaped = apply_shape_to_mermaid(mermaid_input, _raw_shape)

style_directive = f"""
%%{{init: {{'theme':'base', 'themeVariables': {{
    'primaryColor': '{bg_color}',
    'primaryBorderColor': '{stroke_color}',
    'primaryTextColor': '{text_color}',
    'lineColor': '{stroke_color}',
    'fontFamily': '{font_family}',
    'fontSize': '{font_size}px',
    'clusterBkg': '{subgraph_bg}',
    'clusterBorder': '{subgraph_border}'
}}}}}}%%
"""

style_application = f"""
    classDef customStyle fill:{bg_color},stroke:{stroke_color},stroke-width:{stroke_width}px,color:{text_color};
"""

compiled_code = style_directive + "\n" + mermaid_input_shaped + style_application

# Live render token (visual proof of reactivity)
st.sidebar.caption(f"🔄 Live render token: `{font_size}-{font_family[:3]}-{stroke_width}-{_raw_shape[:4]}`")

# ============================================================
# DUAL-TAB VIEW
# ============================================================
tab_mermaid, tab_canvas = st.tabs(["🧜 Mermaid Render", "🎨 Interactive Canvas"])

# ---------- Tab 1: Mermaid ----------
with tab_mermaid:
    cache_buster = hashlib.md5(
        f"{compiled_code}{font_size}{font_family}{subgraph_font_size}{node_padding}".encode()
    ).hexdigest()[:8]

    mermaid_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                margin: 0;
                background: {subgraph_bg};
                padding: 15px;
                font-family: {font_family};
            }}
            /* Post-render CSS overrides — bypass Mermaid classDef limits */
            .mermaid .node text,
            .mermaid .nodeLabel,
            .mermaid .label {{
                font-family: {font_family} !important;
                font-size: {font_size}px !important;
                fill: {text_color} !important;
            }}
            .mermaid .cluster text,
            .mermaid .cluster .nodeLabel {{
                font-size: {subgraph_font_size}px !important;
                font-family: {font_family} !important;
            }}
            .mermaid .node rect,
            .mermaid .node polygon,
            .mermaid .node circle,
            .mermaid .node ellipse {{
                stroke-width: {stroke_width}px !important;
            }}
            .mermaid .cluster rect {{
                fill: {subgraph_bg} !important;
                stroke: {subgraph_border} !important;
            }}
        </style>
    </head>
    <body>
        <div class="mermaid" id="diagram-{cache_buster}">
{compiled_code}
        </div>
        <script type="module">
            import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
            mermaid.initialize({{ startOnLoad: true, securityLevel: 'loose' }});
        </script>
    </body>
    </html>
    """
    components.html(mermaid_html, height=560, scrolling=True)

# ---------- Tab 2: Cytoscape Canvas ----------
with tab_canvas:
    nodes, edges, subgraphs = parse_mermaid(mermaid_input)

    cy_elements = []
    for n in nodes:
        data = {"id": n["id"], "label": n["label"]}
        if n.get("parent"):
            data["parent"] = n["parent"]
        cy_elements.append({"data": data, "classes": "parent" if n["is_parent"] else "child"})
    for e in edges:
        cy_elements.append({"data": {
            "source": e["source"], "target": e["target"], "label": e["label"]
        }})

    cy_json = json.dumps(cy_elements)

    cytoscape_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <script src="https://unpkg.com/cytoscape@3.26.0/dist/cytoscape.min.js"></script>
        <style>
            #cy {{
                width: 100%;
                height: 480px;
                background: {subgraph_bg};
                border: 1px solid {subgraph_border};
                border-radius: 8px;
            }}
            .toolbar {{
                margin-bottom: 8px;
            }}
            button {{
                background: {stroke_color};
                color: white;
                border: none;
                padding: 8px 14px;
                border-radius: 6px;
                margin-right: 6px;
                cursor: pointer;
                font-family: {font_family};
            }}
            textarea {{
                width: 100%;
                height: 160px;
                margin-top: 10px;
                font-family: monospace;
                display: none;
            }}
        </style>
    </head>
    <body>
        <div class="toolbar">
            <button onclick="exportMermaid()">📜 Export as Mermaid</button>
            <button onclick="exportPNG()">🖼️ Export as PNG</button>
        </div>
        <div id="cy"></div>
        <textarea id="mermaid-output" readonly></textarea>
        <script>
            const elements = {cy_json};
            const cy = cytoscape({{
                container: document.getElementById('cy'),
                elements: elements,
                style: [
                    {{
                        selector: 'node.child',
                        style: {{
                            'shape': '{node_shape_value}',
                            'background-color': '{bg_color}',
                            'border-color': '{stroke_color}',
                            'border-width': {stroke_width},
                            'label': 'data(label)',
                            'color': '{text_color}',
                            'font-family': '{font_family}',
                            'font-size': {font_size},
                            'text-valign': 'center',
                            'text-halign': 'center',
                            'padding': {node_padding},
                            'width': 'label',
                            'height': 'label'
                        }}
                    }},
                    {{
                        selector: 'node.parent',
                        style: {{
                            'background-color': '{subgraph_bg}',
                            'border-color': '{subgraph_border}',
                            'border-width': 1,
                            'label': 'data(label)',
                            'font-size': {subgraph_font_size},
                            'font-family': '{font_family}',
                            'text-valign': 'top',
                            'text-halign': 'center',
                            'padding': {node_padding + 10},
                            'color': '{text_color}'
                        }}
                    }},
                    {{
                        selector: 'edge',
                        style: {{
                            'width': 2,
                            'line-color': '{stroke_color}',
                            'target-arrow-color': '{stroke_color}',
                            'target-arrow-shape': 'triangle',
                            'curve-style': 'bezier',
                            'label': 'data(label)',
                            'font-size': {max(10, font_size - 2)},
                            'font-family': '{font_family}',
                            'color': '{text_color}',
                            'text-background-color': '{subgraph_bg}',
                            'text-background-opacity': 0.85,
                            'text-background-padding': 3
                        }}
                    }}
                ],
                layout: {{ name: 'cose', padding: 30, nodeRepulsion: 8000, idealEdgeLength: 110 }}
            }});

            function exportMermaid() {{
                let lines = ['flowchart TD'];

                // 1. Subgraphs
                cy.nodes(':parent').forEach(parent => {{
                    lines.push('    subgraph ' + parent.id() + ' ["' + parent.data('label') + '"]');
                    parent.children().forEach(child => {{
                        lines.push('        ' + child.id() + '["' + child.data('label') + '"]');
                    }});
                    lines.push('    end');
                }});

                // 2. Orphan nodes
                cy.nodes().forEach(node => {{
                    if (!node.isParent() && !node.parent().length) {{
                        lines.push('    ' + node.id() + '["' + node.data('label') + '"]');
                    }}
                }});

                // 3. Edges
                cy.edges().forEach(edge => {{
                    const label = edge.data('label') ? '|' + edge.data('label') + '|' : '';
                    const arrow = ' -->' + label + ' ';
                    lines.push('    ' + edge.source().id() + arrow + edge.target().id());
                }});

                // 4. Style injection
                lines.push('    classDef customStyle fill:{bg_color},stroke:{stroke_color},stroke-width:{stroke_width}px,color:{text_color},font-family:{font_family},font-size:{font_size}px;');
                cy.nodes().forEach(node => {{
                    if (!node.isParent()) {{
                        lines.push('    class ' + node.id() + ' customStyle;');
                    }}
                }});

                const outputBox = document.getElementById('mermaid-output');
                outputBox.value = lines.join('\\n');
                outputBox.style.display = 'block';
            }}

            function exportPNG() {{
                const pngContent = cy.png({{ scale: 2, bg: '{subgraph_bg}', full: true }});
                const downloadLink = document.createElement('a');
                downloadLink.href = pngContent;
                downloadLink.download = 'canvas_flowchart.png';
                document.body.appendChild(downloadLink);
                downloadLink.click();
                document.body.removeChild(downloadLink);
            }}
        </script>
    </body>
    </html>
    """
    components.html(cytoscape_html, height=600)

# --- Global Styled Code Export ---
st.subheader("💾 Visual Theme Code Base")
st.code(compiled_code, language="mermaid")
