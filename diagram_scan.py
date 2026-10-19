"""Directory scanner for Mermaid and draw.io diagram files, Markdown documents,
Word (.docx), OpenDocument Text (.odt) and PDF documents.

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
OFFICE_EXTS = (".docx", ".odt")  # Word / OpenDocument Text — read natively, opened with the default app

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".idea", ".vscode"}

MAX_FILES = 1500                     # stop walking after this many diagram files
MAX_PREVIEW_BYTES = 5 * 1024 * 1024  # larger files are listed but not previewed
MAX_SEARCH_CHARS = 6000              # text shipped to the browser for filtering
MAX_LABELS = 12                      # label chips shown on a card
MAX_DOC_PREVIEW_CHARS = 4000         # Markdown shipped to the browser for a document thumbnail
MAX_OFFICE_CHARS = 20000             # paragraph text pulled from a Word / OpenDocument file
MAX_PDF_PREVIEW_BYTES = 25 * 1024 * 1024  # PDFs are bigger on average — first-page render is cheap anyway
MAX_PDF_TEXT_PAGES = 300             # pages read for a PDF's word count and search text
PDF_PREVIEW_DPI = 100                # resolution of the first-page PNG thumbnail
PANDOC_TIMEOUT_S = 120

MERMAID_FENCE = re.compile(r"^```+\s*mermaid\s*$\n(.*?)^```+\s*$", re.MULTILINE | re.DOTALL | re.IGNORECASE)
MERMAID_LABEL = re.compile(
    r'[\[\(\{>]+"?([^\[\]\(\)\{\}"|]+?)"?[\]\)\}]+'                              # A["Label"], B(Label), C{Label}
    r'|\|([^|]+)\|'                                                             # -->|edge label|
    r'|^\s*(?:participant|actor|class|state)\s+(?:\w+\s+as\s+)?([^\s{~][^{\n]*?)\s*$'  # sequence / class / state names
    r'|:\s*([^:\n]+?)\s*$',                                                      # message / transition text
    re.MULTILINE)
FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)
FENCED_CODE = re.compile(r"^(```+|~~~+).*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
MD_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
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
    """Return 'mermaid', 'markdown', 'drawio', 'drawio-image', 'docx', 'odt', 'pdf' or None for a file path."""
    name = path.name.lower()
    if name.endswith(DRAWIO_IMAGE_EXTS):
        return "drawio-image"
    if name.endswith(DRAWIO_EXTS):
        return "drawio"
    if name.endswith(MERMAID_EXTS):
        return "mermaid"
    if name.endswith(MARKDOWN_EXTS):
        return "markdown"
    if name.endswith(OFFICE_EXTS):
        return "docx" if name.endswith(".docx") else "odt"
    if name.endswith(".pdf"):
        return "pdf"
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
# MARKDOWN DOCUMENTS
# ============================================================
def _markdown_entry(text):
    """The whole Markdown file as one document card; double-click converts it to .docx with pandoc."""
    match = FRONT_MATTER.match(text)
    body = text[match.end():] if match else text
    title = re.search(r"^title:[ \t]*[\"']?(.+?)[\"']?[ \t]*$", match.group(1), re.MULTILINE) if match else None
    prose = FENCED_CODE.sub("", body)  # `# comments` in code blocks are not headings
    headings = []
    for h in MD_HEADING.findall(prose):
        h = re.sub(r"[*`]", "", h).strip()
        if h and h not in headings:
            headings.append(h)
    preview = f"# {title.group(1)}\n\n{body}" if title else body  # pandoc puts the title on top too
    return {
        "kind": "markdown",
        "subtype": "Markdown",
        "block": None,
        "labels": headings[:MAX_LABELS],
        "label_count": len(headings),
        "words": len(prose.split()),
        "search": text[:MAX_SEARCH_CHARS],
        "source": preview[:MAX_DOC_PREVIEW_CHARS],
    }


# ============================================================
# WORD (.docx) AND OPENDOCUMENT (.odt) DOCUMENTS
# ============================================================
def _md_escape(text):
    """Escape extracted text so the browser's Markdown renderer shows it as plain prose."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([`*_\\|])", r"\\\1", text)


def _docx_body_blocks(doc):
    """Yield paragraphs and tables of a python-docx Document in body order."""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _docx_heading_level(paragraph):
    """Heading depth 1-6 from the paragraph style, or 0 for body text. Matches style ids too,
    which stay English ('Heading1') in documents created in other languages."""
    style = paragraph.style
    name = " ".join(str(getattr(style, "name", "") or "").lower().split())
    style_id = str(getattr(style, "style_id", "") or "").lower()
    m = re.search(r"heading\s*-?\s*([1-6])", name) or re.search(r"heading\s*-?\s*([1-6])", style_id)
    if m:
        return int(m.group(1))
    if name == "title":
        return 1
    if name == "subtitle":
        return 2
    return 0


def _collect_docx(path):
    """Blocks of a .docx as ('h', level, text) / ('p', text) / ('table', [[cell, …], …])."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    blocks = []
    for block in _docx_body_blocks(docx.Document(str(path))):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                level = _docx_heading_level(block)
                blocks.append(("h", level, text) if level else ("p", text))
        elif isinstance(block, Table):
            rows = [[" ".join(p.text for p in cell.paragraphs).strip() for cell in row.cells]
                    for row in block.rows]
            blocks.append(("table", rows))
    return blocks


def _collect_odt(path):
    """Blocks of an .odt in the same shape as _collect_docx, walking the ODF body recursively."""
    from odf import teletype
    from odf.namespaces import TEXTNS
    from odf.opendocument import load
    blocks = []

    def walk(node):
        for child in node.childNodes:
            tag = getattr(child, "qname", (None, None))[1]  # odfpy elements are typed by qname, not class
            if tag == "h":  # heading — depth comes from its outline level
                raw = child.attributes.get((TEXTNS, "outline-level"), "1")  # str or (ns, name) tuple
                if isinstance(raw, tuple):
                    raw = raw[1]
                try:
                    level = min(6, max(1, int(raw)))
                except (TypeError, ValueError):
                    level = 1
                text = teletype.extractText(child).strip()
                if text:
                    blocks.append(("h", level, text))
            elif tag == "p":
                text = teletype.extractText(child).strip()
                if text:
                    blocks.append(("p", text))
            elif hasattr(child, "childNodes"):
                walk(child)  # lists and tables: their paragraphs stay in document order

    walk(load(str(path)).text)
    return blocks


def _office_entry(path, kind):
    """Word / OpenDocument document as one card, like a Markdown document: the text is turned into
    Markdown for a page-preview thumbnail, headings become label chips, double-click opens the file
    itself with the app the OS associates with it (Word, LibreOffice, Pages …)."""
    blocks = _collect_docx(path) if kind == "docx" else _collect_odt(path)
    lines, headings, plain = [], [], []
    words = total = 0

    def count(text):
        nonlocal words, total
        words += len(text.split())
        total += len(text)

    for block in blocks:
        if total > MAX_OFFICE_CHARS:
            break
        if block[0] == "h":
            _, level, text = block
            lines.append(f"{'#' * level} {_md_escape(text)}")
            if text not in headings:
                headings.append(text)
            count(text)
            plain.append(text)
        elif block[0] == "p":
            lines.append(_md_escape(block[1]))
            count(block[1])
            plain.append(block[1])
        else:  # table — a GFM table if it has a body row, plain pipe-joined lines otherwise
            rows = [[cell for cell in row] for row in block[1] if any(row)]
            if not rows:
                continue
            cols = max(len(row) for row in rows)
            shown = ["| " + " | ".join(_md_escape(cell) for cell in row + [""] * (cols - len(row))) + " |"
                     for row in rows[:20]]
            if len(shown) > 1:
                shown.insert(1, "|" + " --- |" * cols)
            lines.append("\n".join(shown))
            for row in rows:
                plain.extend(cell for cell in row if cell)
                count(" ".join(row))

    return {
        "kind": kind,
        "subtype": "Word" if kind == "docx" else "OpenDocument",
        "block": None,
        "labels": headings[:MAX_LABELS],
        "label_count": len(headings),
        "words": words,
        "search": " ".join(plain)[:MAX_SEARCH_CHARS],
        "source": "\n\n".join(lines)[:MAX_DOC_PREVIEW_CHARS],
    }


# ============================================================
# PDF DOCUMENTS
# ============================================================
def _pdf_entry(path):
    """PDF document as one card. Unlike the other document kinds the thumbnail is the real thing:
    the first page rendered to PNG (shown through the same image path as .drawio.png exports).
    The page texts feed the search filter and word count, the document outline (bookmarks) and
    metadata title provide the label chips, and double-click opens the file itself with the app
    the OS associates with it (Preview, Acrobat, Chrome …)."""
    import pymupdf
    with pymupdf.open(str(path)) as doc:
        if not doc.page_count:
            raise ValueError("the PDF has no pages")
        pixmap = doc[0].get_pixmap(dpi=PDF_PREVIEW_DPI)
        image = f"data:image/png;base64,{base64.b64encode(pixmap.tobytes('png')).decode('ascii')}"

        words, texts, total = 0, [], 0
        for i in range(min(doc.page_count, MAX_PDF_TEXT_PAGES)):  # huge PDFs: only the first pages are read
            # "text" mode always returns a str; str() only quiets the checker about the dispatcher's union type
            text = str(doc[i].get_text("text"))
            words += len(text.split())
            if total < MAX_SEARCH_CHARS:
                texts.append(text)
                total += len(text)

        title = ((doc.metadata or {}).get("title") or "").strip()
        headings = []
        for heading in ([title] if title else []) + [t[1].strip() for t in doc.get_toc() if t[1].strip()]:
            if heading not in headings:
                headings.append(heading)

        return {
            "kind": "pdf",
            "subtype": "PDF",
            "block": None,
            "labels": headings[:MAX_LABELS],
            "label_count": len(headings),
            "words": words,
            "n_pages": doc.page_count,
            "search": " ".join(" ".join(t.split()) for t in texts)[:MAX_SEARCH_CHARS],
            "image": image,
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
def _card_kind(kind):
    """Card kind for a classified file: 'mermaid', 'markdown' or 'drawio'."""
    return "drawio" if kind.startswith("drawio") else kind


def load_entries(path_str):
    """Parse one file into gallery entries — Markdown gives a document card plus one per Mermaid block."""
    path = Path(path_str)
    kind = classify(path)
    stat = path.stat()
    base = {"path": str(path), "name": path.name, "mtime": stat.st_mtime, "size": stat.st_size}

    limit = MAX_PDF_PREVIEW_BYTES if kind == "pdf" else MAX_PREVIEW_BYTES
    if stat.st_size > limit:
        return [{**base, "kind": _card_kind(kind),
                 "subtype": "too large", "block": None, "labels": [], "label_count": 0, "search": "",
                 "error": f"File is larger than {limit // (1024 * 1024)} MB — preview skipped."}]

    try:
        if kind == "mermaid":
            return [{**base, **_mermaid_entry(path, path.read_text(encoding="utf-8", errors="replace"))}]
        if kind == "markdown":
            text = path.read_text(encoding="utf-8", errors="replace")
            blocks = MERMAID_FENCE.findall(text)
            return [{**base, **_markdown_entry(text)}] + [
                {**base, **_mermaid_entry(path, code.strip(), block=i + 1 if len(blocks) > 1 else None)}
                for i, code in enumerate(blocks)]
        if kind in ("docx", "odt"):
            try:
                return [{**base, **_office_entry(path, kind)}]
            except ModuleNotFoundError:
                return [{**base, "kind": kind, "subtype": "missing dependency", "block": None,
                         "labels": [], "label_count": 0, "search": "",
                         "error": "Reading Word / OpenDocument files needs extra packages — "
                                  "run: pip install python-docx odfpy"}]
            except Exception as exc:  # the office parsers raise many exception types on broken files
                return [{**base, "kind": kind, "subtype": "unreadable", "block": None,
                         "labels": [], "label_count": 0, "search": "",
                         "error": f"Could not read document: {exc}"}]
        if kind == "pdf":
            try:
                return [{**base, **_pdf_entry(path)}]
            except ModuleNotFoundError:
                return [{**base, "kind": kind, "subtype": "missing dependency", "block": None,
                         "labels": [], "label_count": 0, "search": "",
                         "error": "Reading PDF files needs an extra package — run: pip install pymupdf"}]
            except Exception as exc:  # PyMuPDF raises its own exception types on broken files
                return [{**base, "kind": kind, "subtype": "unreadable", "block": None,
                         "labels": [], "label_count": 0, "search": "",
                         "error": f"Could not read document: {exc}"}]
        return [{**base, **_drawio_entry(path, kind)}]
    except (OSError, ET.ParseError) as exc:
        return [{**base, "kind": _card_kind(kind), "subtype": "unreadable",
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


def find_pandoc():
    """Return the pandoc executable, or None.

    Also checks the usual install folders, which are missing from PATH when Streamlit is started
    from an IDE or the Dock rather than a login shell.
    """
    candidates = [shutil.which("pandoc"), "/opt/homebrew/bin/pandoc", "/usr/local/bin/pandoc"]
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(os.path.join(os.environ["LOCALAPPDATA"], "Pandoc", "pandoc.exe"))
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def markdown_to_docx(path_str):
    """Convert a Markdown file to .docx with pandoc and return the path of the .docx.

    Same as `pandoc file.md -o file.docx`, but written to a temporary folder so a file.docx next to
    the source is never overwritten. Each version of the source gets its own folder: an unchanged
    file reuses its last conversion, and an edited one opens fresh instead of Word bringing back the
    stale copy it still has open.
    """
    pandoc = find_pandoc()
    if not pandoc:
        raise RuntimeError("pandoc not found. Install it from https://pandoc.org/installing.html")
    src = Path(path_str)
    stat = src.stat()
    tag = hashlib.sha1(str(src).encode()).hexdigest()[:10]
    target = (Path(tempfile.gettempdir()) / "mermaidEd" / "docx" / tag
              / f"{stat.st_mtime_ns:x}-{stat.st_size:x}" / f"{src.stem}.docx")
    if target.is_file():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Run in the source folder so relative image paths resolve as they do on the command line
        proc = subprocess.run([pandoc, str(src), "-o", str(target)], cwd=src.parent,
                              capture_output=True, text=True, timeout=PANDOC_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"pandoc did not finish within {PANDOC_TIMEOUT_S} s") from None
    if proc.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"pandoc failed: {proc.stderr.strip()[:300] or f'exit code {proc.returncode}'}")
    return str(target)


def open_with_default_app(path):
    """Open `path` in the app the OS associates with its type (Word, Pages, LibreOffice … for .docx)."""
    system = platform.system()
    if system == "Windows":
        os.startfile(str(path))
    elif system == "Darwin":
        proc = subprocess.run(["open", str(path)], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"Could not open {path}")
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def trash_file(path_str):
    """Move a file to the system trash so it can be restored. Returns a short past-tense description."""
    path = Path(path_str)
    try:
        from send2trash import send2trash
        send2trash(str(path))
        return "Moved to trash:"
    except ImportError:
        pass
    if platform.system() == "Darwin":
        trash = Path.home() / ".Trash"
        target = trash / path.name
        n = 1
        while target.exists():
            target = trash / f"{path.stem} {n}{''.join(path.suffixes)}"
            n += 1
        shutil.move(str(path), str(target))
        return "Moved to trash:"
    raise RuntimeError("Install `send2trash` (pip install send2trash) to delete files safely.")
