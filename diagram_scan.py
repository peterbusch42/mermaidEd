"""Directory scanner for Mermaid and draw.io diagram files.

Pure Python (no Streamlit dependency) so it can be reused and tested on its own.
Each discovered diagram becomes a plain dict that the gallery page ships to the
browser, where the thumbnails are rendered client-side.
"""

import base64
import hashlib
import html
import os
import platform
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

MERMAID_EXTS = (".mmd", ".mermaid")
MARKDOWN_EXTS = (".md", ".markdown")
DRAWIO_EXTS = (".drawio", ".dio")
DRAWIO_IMAGE_EXTS = (".drawio.svg", ".drawio.png")

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".idea", ".vscode"}

MAX_FILES = 1500                     # stop walking after this many diagram files
MAX_PREVIEW_BYTES = 5 * 1024 * 1024  # larger files are listed but not previewed
MAX_SEARCH_CHARS = 6000              # text shipped to the browser for filtering
MAX_LABELS = 12                      # label chips shown on a card

MERMAID_FENCE = re.compile(r"^```+\s*mermaid\s*$\n(.*?)^```+\s*$", re.MULTILINE | re.DOTALL | re.IGNORECASE)
MERMAID_LABEL = re.compile(
    r'[\[\(\{>]+"?([^\[\]\(\)\{\}"|]+?)"?[\]\)\}]+'                              # A["Label"], B(Label), C{Label}
    r'|\|([^|]+)\|'                                                             # -->|edge label|
    r'|^\s*(?:participant|actor|class|state)\s+(?:\w+\s+as\s+)?([^\s{~][^{\n]*?)\s*$'  # sequence / class / state names
    r'|:\s*([^:\n]+?)\s*$',                                                      # message / transition text
    re.MULTILINE)
MERMAID_TYPES = {
    "flowchart": "Flowchart", "graph": "Flowchart", "sequencediagram": "Sequence",
    "classdiagram": "Class", "statediagram": "State", "statediagram-v2": "State",
    "erdiagram": "ER", "journey": "Journey", "gantt": "Gantt", "pie": "Pie",
    "mindmap": "Mindmap", "timeline": "Timeline", "gitgraph": "Git graph",
    "quadrantchart": "Quadrant", "requirementdiagram": "Requirement",
    "c4context": "C4", "c4container": "C4", "c4component": "C4", "c4dynamic": "C4",
    "c4deployment": "C4", "sankey-beta": "Sankey", "xychart-beta": "XY chart",
    "block-beta": "Block", "architecture-beta": "Architecture", "packet-beta": "Packet",
}


def classify(path):
    """Return 'mermaid', 'markdown', 'drawio', 'drawio-image' or None for a file path."""
    name = path.name.lower()
    if name.endswith(DRAWIO_IMAGE_EXTS):
        return "drawio-image"
    if name.endswith(DRAWIO_EXTS):
        return "drawio"
    if name.endswith(MERMAID_EXTS):
        return "mermaid"
    if name.endswith(MARKDOWN_EXTS):
        return "markdown"
    return None


def find_diagram_files(root, recursive=True):
    """Walk `root` and return (sorted list of candidate paths, truncated flag)."""
    root = Path(root)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if not recursive:
            dirnames[:] = []
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if classify(p):
                found.append(p)
                if len(found) >= MAX_FILES:
                    return found, True
    return found, False


# ============================================================
# MERMAID
# ============================================================
def mermaid_type(code):
    for line in code.splitlines():
        s = line.strip()
        if not s or s.startswith("%%") or s == "---" or ":" in s.split(" ")[0]:
            continue  # skip blanks, comments, front-matter fences and `title: ...` lines
        return MERMAID_TYPES.get(s.split()[0].lower(), s.split()[0])
    return "Mermaid"


def mermaid_labels(code):
    labels = []
    for m in MERMAID_LABEL.finditer(code):
        text = next((g for g in m.groups() if g), "").strip()
        if any(c.isalnum() for c in text) and text not in labels:
            labels.append(text)
    return labels


def _mermaid_entry(path, code, block=None):
    labels = mermaid_labels(code)
    return {
        "kind": "mermaid",
        "subtype": mermaid_type(code),
        "block": block,
        "labels": labels[:MAX_LABELS],
        "label_count": len(labels),
        "search": code[:MAX_SEARCH_CHARS],
        "source": code,
    }


# ============================================================
# DRAW.IO
# ============================================================
def _inflate_diagram(text):
    """Decode a compressed <diagram> payload (base64 + raw deflate + URI encoding)."""
    raw = base64.b64decode(text)
    try:
        data = zlib.decompress(raw, -15).decode("utf-8")
    except zlib.error:
        data = raw.decode("utf-8")
    return urllib.parse.unquote(data)


def _clean_label(value):
    value = re.sub(r"<br\s*/?>|</div>|</p>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_drawio_xml(xml_text):
    """Return {'pages': [names], 'labels': [...], 'vertices': n, 'edges': n} for an mxfile string."""
    root = ET.fromstring(xml_text)
    models = []  # (page name, mxGraphModel element)
    if root.tag == "mxGraphModel":
        models.append(("Page-1", root))
    else:
        for i, diagram in enumerate(root.iter("diagram")):
            name = diagram.get("name") or f"Page-{i + 1}"
            model = diagram.find("mxGraphModel")
            if model is None and (diagram.text or "").strip():
                model = ET.fromstring(_inflate_diagram(diagram.text.strip()))
            if model is not None:
                models.append((name, model))

    labels, vertices, edges = [], 0, 0
    for _, model in models:
        for el in model.iter():
            if el.tag == "mxCell":
                vertices += el.get("vertex") == "1"
                edges += el.get("edge") == "1"
            # UserObject / object wrap an mxCell and carry the label themselves
            raw = el.get("label") if el.tag in ("UserObject", "object") else el.get("value")
            text = _clean_label(raw or "")
            if text and text not in labels:
                labels.append(text)
    return {"pages": [name for name, _ in models], "labels": labels, "vertices": vertices, "edges": edges}


def _png_mxfile(data):
    """Extract the embedded mxfile XML from a .drawio.png text chunk."""
    pos = 8
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"tEXt":
            key, _, value = chunk.partition(b"\0")
            if key == b"mxfile":
                return urllib.parse.unquote(value.decode("latin-1"))
        elif ctype == b"zTXt":
            key, _, rest = chunk.partition(b"\0")
            if key in (b"mxfile", b"mxGraphModel"):
                return urllib.parse.unquote(zlib.decompress(rest[1:]).decode("latin-1"))
    return None


def _drawio_entry(path, kind):
    info = {"pages": [], "labels": [], "vertices": 0, "edges": 0}
    entry = {"kind": "drawio", "subtype": "draw.io", "block": None}
    error = None
    raw = path.read_bytes()

    if kind == "drawio":
        source = raw.decode("utf-8", errors="replace")
        entry["source"] = source
        xml_text = source
    else:
        is_svg = path.name.lower().endswith(".svg")
        mime = "image/svg+xml" if is_svg else "image/png"
        entry["image"] = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        entry["subtype"] = "draw.io SVG" if is_svg else "draw.io PNG"
        if is_svg:
            xml_text = ET.fromstring(raw).get("content")
        else:
            xml_text = _png_mxfile(raw)

    try:
        if xml_text:
            info = parse_drawio_xml(xml_text)
    except (ET.ParseError, ValueError, zlib.error, UnicodeDecodeError) as exc:
        error = f"Could not read diagram contents: {exc}"

    entry.update({
        "pages": info["pages"],
        "labels": info["labels"][:MAX_LABELS],
        "label_count": len(info["labels"]),
        "vertices": info["vertices"],
        "edge_count": info["edges"],
        "search": " ".join(info["pages"] + info["labels"])[:MAX_SEARCH_CHARS],
        "error": error,
    })
    return entry


# ============================================================
# PUBLIC API
# ============================================================
def load_entries(path_str):
    """Parse one file into zero or more gallery entries (Markdown can hold several Mermaid blocks)."""
    path = Path(path_str)
    kind = classify(path)
    stat = path.stat()
    base = {"path": str(path), "name": path.name, "mtime": stat.st_mtime, "size": stat.st_size}

    if stat.st_size > MAX_PREVIEW_BYTES:
        if kind == "markdown":
            return []
        return [{**base, "kind": "drawio" if kind.startswith("drawio") else "mermaid",
                 "subtype": "too large", "block": None, "labels": [], "label_count": 0, "search": "",
                 "error": f"File is larger than {MAX_PREVIEW_BYTES // (1024 * 1024)} MB — preview skipped."}]

    try:
        if kind == "mermaid":
            return [{**base, **_mermaid_entry(path, path.read_text(encoding="utf-8", errors="replace"))}]
        if kind == "markdown":
            blocks = MERMAID_FENCE.findall(path.read_text(encoding="utf-8", errors="replace"))
            return [{**base, **_mermaid_entry(path, code.strip(), block=i + 1 if len(blocks) > 1 else None)}
                    for i, code in enumerate(blocks)]
        return [{**base, **_drawio_entry(path, kind)}]
    except (OSError, ET.ParseError) as exc:
        return [{**base, "kind": "drawio" if kind.startswith("drawio") else "mermaid", "subtype": "unreadable",
                 "block": None, "labels": [], "label_count": 0, "search": "", "error": str(exc)}]


def find_drawio_app():
    """Return a command prefix that opens a file in the draw.io desktop app, or None."""
    system = platform.system()
    if system == "Darwin":
        for app in ("/Applications/draw.io.app", os.path.expanduser("~/Applications/draw.io.app")):
            if os.path.isdir(app):
                return ["open", "-a", app]
        return None
    if system == "Windows":
        for base in (os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "") + r"\Programs"):
            exe = os.path.join(base, "draw.io", "draw.io.exe")
            if base and os.path.isfile(exe):
                return [exe]
        return None
    exe = shutil.which("drawio") or shutil.which("draw.io")
    return [exe] if exe else None


def drawio_file_for(entry):
    """Return the file draw.io should open for a gallery entry.

    draw.io (v31+) opens .drawio, .drawio.svg/.png, .mmd and .mermaid files directly — Mermaid is
    converted into editable draw.io shapes. A Mermaid block inside Markdown is written to a
    temporary .mmd file first.
    """
    if classify(Path(entry["path"])) != "markdown":
        return entry["path"]
    stem = Path(entry["path"]).stem
    tag = hashlib.sha1(entry["path"].encode()).hexdigest()[:6]
    target = Path(tempfile.gettempdir()) / "mermaidEd" / f"{stem}-block{entry['block'] or 1}-{tag}.mmd"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(entry["source"], encoding="utf-8")
    return str(target)


def open_in_drawio(path):
    """Launch the draw.io desktop app with `path`. Raises RuntimeError if it is not installed."""
    cmd = find_drawio_app()
    if not cmd:
        raise RuntimeError("draw.io desktop app not found. Install it from https://www.drawio.com/")
    subprocess.Popen(cmd + [str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
