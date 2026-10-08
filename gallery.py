import hashlib
import json
from pathlib import Path

import streamlit as st

import diagram_scan

HERE = Path(__file__).parent

# Bidirectional component: renders thumbnails in the browser and reports
# double-clicks back to Python, which can then launch the draw.io desktop app.
gallery_view = st.components.v2.component(
    "diagram_gallery",
    css=(HERE / "gallery_view.css").read_text(encoding="utf-8"),
    js=(HERE / "gallery_view.js").read_text(encoding="utf-8"),
    isolate_styles=False,  # the draw.io viewer injects global CSS that must reach the thumbnails
)


@st.cache_data(show_spinner=False, max_entries=5000)
def load_file(path, mtime, size):
    """Parse a file once per (mtime, size) — edited files are re-read automatically."""
    return diagram_scan.load_entries(path)


def persisted(widget_key, default):
    """Keep a widget's value when the user switches pages (Streamlit drops widget state otherwise)."""
    store_key = f"_{widget_key}_value"
    if store_key not in st.session_state:
        st.session_state[store_key] = default
    st.session_state[widget_key] = st.session_state[store_key]
    return lambda: st.session_state.__setitem__(store_key, st.session_state[widget_key])


RECENT_FILE = Path.home() / ".mermaidEd" / "recent_folders.json"
MAX_RECENT = 8
MAX_SUBFOLDERS = 300


def load_recent():
    try:
        return [p for p in json.loads(RECENT_FILE.read_text(encoding="utf-8")) if Path(p).is_dir()][:MAX_RECENT]
    except (OSError, ValueError):
        return []


def remember_folder(path):
    recent = [p for p in load_recent() if p != str(path)]
    try:
        RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECENT_FILE.write_text(json.dumps([str(path), *recent][:MAX_RECENT]), encoding="utf-8")
    except OSError:
        pass  # recents are a convenience only


def browse_to(path):
    st.session_state["_browse_path"] = str(path)


def choose_folder(path):
    st.session_state["_gallery_dir_value"] = str(path)  # picked up by persisted() on the rerun
    remember_folder(path)
    st.rerun()


def queue_delete():
    """Keep a card's delete request for this run's scan — handled before the grid is drawn, so the card is gone."""
    st.session_state["_gallery_delete"] = st.session_state["diagram_gallery"].get("delete")


@st.dialog("Choose a folder", width="large")
def folder_picker():
    start = Path(st.session_state.get("_gallery_dir_value", "")).expanduser()
    current = Path(st.session_state.setdefault("_browse_path", str(start if start.is_dir() else Path.home())))
    if not current.is_dir():
        current = Path.home()

    # Quick places and recent folders
    home = Path.home()
    places = [("🏠 Home", home), ("🖥️ Desktop", home / "Desktop"), ("📄 Documents", home / "Documents"),
              ("⬇️ Downloads", home / "Downloads"), ("💽 Volumes", Path("/Volumes"))]
    places = [(label, p) for label, p in places if p.is_dir()]
    cols = st.columns(len(places))
    for col, (label, p) in zip(cols, places):
        col.button(label, key=f"place_{p}", width="stretch", on_click=browse_to, args=(p,))
    recent = load_recent()
    if recent:
        with st.expander(f"🕘 Recent folders ({len(recent)})"):
            for p in recent:
                st.button(f"{Path(p).name or p}  —  {p}", key=f"recent_{p}", width="stretch",
                          on_click=browse_to, args=(p,))

    # Address bar: breadcrumbs are replaced by an editable path + "up"
    col_up, col_path = st.columns([1, 7], vertical_alignment="bottom")
    col_up.button("⬆️ Up", key="browse_up", width="stretch", disabled=current.parent == current,
                  on_click=browse_to, args=(current.parent,))
    typed = col_path.text_input("Location", value=str(current), key=f"browse_addr_{current}")
    if typed != str(current):
        target = Path(typed).expanduser()
        if target.is_dir():
            browse_to(target)
            st.rerun(scope="fragment")
        else:
            st.caption(f":red[Not a folder: {typed}]")

    show_hidden = st.toggle("Show hidden folders", key="browse_hidden")
    name_filter = st.text_input("Filter folders", key="browse_filter", placeholder="Type to filter…",
                                label_visibility="collapsed")
    try:
        subdirs = sorted((p for p in current.iterdir() if p.is_dir() and (show_hidden or not p.name.startswith("."))),
                         key=lambda p: p.name.lower())
    except OSError as exc:
        subdirs = []
        st.warning(f"Cannot read this folder: {exc.strerror or exc}")
    if name_filter:
        subdirs = [p for p in subdirs if name_filter.lower() in p.name.lower()]

    with st.container(height=300, border=True):
        if not subdirs:
            st.caption("No subfolders here.")
        for p in subdirs[:MAX_SUBFOLDERS]:
            st.button(f"📁 {p.name}", key=f"dir_{p}", width="stretch", on_click=browse_to, args=(p,))
        if len(subdirs) > MAX_SUBFOLDERS:
            st.caption(f"Showing the first {MAX_SUBFOLDERS} of {len(subdirs)} — use the filter.")

    n_found = sum(1 for f in current.glob("*") if f.is_file() and diagram_scan.classify(f)) if current.is_dir() else 0
    st.caption(f"**{current}** — {n_found} diagram file{'s' if n_found != 1 else ''} directly in this folder")
    if st.button("✅ Use this folder", type="primary", width="stretch"):
        choose_folder(current)


# --- UI Header ---
st.title("🗂️ GalleryEd · Gallery")
st.markdown(
    "Scan a folder for **Mermaid** (`.mmd`, `.mermaid`, ` ```mermaid ` blocks in `.md`), "
    "**draw.io** (`.drawio`, `.dio`, `.drawio.svg`, `.drawio.png`) and **Markdown** (`.md`) files and see them all at a glance. "
    "**Double-click** a card to open it: diagrams in the draw.io desktop app — Mermaid diagrams are converted into "
    "editable draw.io shapes — and Markdown documents as Word files (`.docx`, converted with pandoc)."
)

col_dir, col_browse, col_rec, col_btn = st.columns([6, 1.3, 1.6, 1], vertical_alignment="bottom")
directory = col_dir.text_input("Folder", key="gallery_dir", placeholder="/path/to/your/diagrams",
                               on_change=persisted("gallery_dir", ""))
if col_browse.button("📂 Browse…", width="stretch"):
    st.session_state.pop("_browse_path", None)  # reopen at the current folder
    folder_picker()
recursive = col_rec.checkbox("Include subfolders", key="gallery_recursive",
                             on_change=persisted("gallery_recursive", True))
if col_btn.button("🔄 Rescan", width="stretch"):
    load_file.clear()

if not directory.strip():
    st.info("Enter a folder path above or click **📂 Browse…** to build the overview.")
    st.stop()

root = Path(directory.strip()).expanduser()
if not root.is_dir():
    st.error(f"Folder not found: `{root}`")
    st.stop()

# ============================================================
# SCAN
# ============================================================
with st.spinner(f"Scanning {root} …"):
    files, truncated = diagram_scan.find_diagram_files(root, recursive)
    entries = []
    for f in files:
        try:
            stat = f.stat()
        except OSError:
            continue
        is_markdown = diagram_scan.classify(f) == "markdown"
        for entry in load_file(str(f), stat.st_mtime, stat.st_size):
            rel_dir = f.parent.relative_to(root).as_posix()
            entries.append({**entry, "dir": "" if rel_dir == "." else rel_dir,
                            # Deleting trashes the whole file: a Markdown document only from its own card,
                            # never from a card for one of the diagrams inside it
                            "deletable": not is_markdown or entry["kind"] == "markdown"})

# ============================================================
# DELETE HANDLER (request from a card's trash button, see queue_delete)
# ============================================================
doomed = st.session_state.pop("_gallery_delete", None)
delete_failed = None  # the failed request's timestamp — tells the browser to make the card usable again
if doomed:
    # Only delete files that are part of the current scan — never an arbitrary path from the browser
    entry = next((e for e in entries if e["path"] == doomed.get("path") and e["deletable"]), None)
    if entry is None:
        st.toast("That file is no longer part of the scan — try 🔄 Rescan.", icon="⚠️")
    else:
        try:
            how = diagram_scan.trash_file(entry["path"])
            entries = [e for e in entries if e["path"] != entry["path"]]
            st.toast(f"{how} **{entry['name']}**", icon="🗑️")
        except (RuntimeError, OSError) as exc:
            delete_failed = doomed.get("t")
            st.toast(f"Could not delete {entry['name']}: {exc}", icon="❌")

if truncated:
    st.warning(f"Stopped after {diagram_scan.MAX_FILES} files — pick a more specific folder to see everything.")
if not entries:
    st.info("No diagrams or Markdown documents found in this folder.")
    st.stop()

n_drawio = sum(e["kind"] == "drawio" for e in entries)
n_docs = sum(e["kind"] == "markdown" for e in entries)
st.caption(f"Found **{len(entries) - n_docs}** diagrams and **{n_docs}** Markdown documents in "
           f"**{len({e['path'] for e in entries})}** files — "
           f"{len(entries) - n_drawio - n_docs} Mermaid · {n_drawio} draw.io")

# Changes whenever a file is added, removed or edited, so the browser only rebuilds the grid when needed
signature = hashlib.sha1(
    repr([(e["path"], e["block"], e["mtime"], e["size"]) for e in entries]).encode()
).hexdigest()

result = gallery_view(
    key="diagram_gallery",
    data={"items": entries, "sig": signature, "hasDrawio": diagram_scan.find_drawio_app() is not None,
          "hasPandoc": diagram_scan.find_pandoc() is not None, "deleteFailed": delete_failed},
    on_open_change=lambda: None,
    on_delete_change=queue_delete,
)

# ============================================================
# DOUBLE-CLICK HANDLER
# ============================================================
request = result.open
if request:
    # Only open files that are part of the current scan — never an arbitrary path from the browser.
    # Kind matters too: a Markdown file has a document card and (block None) a card for its only Mermaid block.
    entry = next((e for e in entries if e["path"] == request.get("path") and e["block"] == request.get("block")
                  and e["kind"] == request.get("kind")), None)
    if entry is None:
        st.toast("That file is no longer part of the scan — try 🔄 Rescan.", icon="⚠️")
    elif entry["kind"] == "markdown":
        try:
            diagram_scan.open_with_default_app(diagram_scan.markdown_to_docx(entry["path"]))
            st.toast(f"Opening **{entry['name']}** as a Word document …", icon="📄")
        except (RuntimeError, OSError) as exc:
            st.toast(str(exc), icon="❌")
    else:
        try:
            diagram_scan.open_in_drawio(diagram_scan.drawio_file_for(entry))
            label = f"{entry['name']} #{entry['block']}" if entry["block"] else entry["name"]
            st.toast(f"Opening **{label}** in draw.io …", icon="🚀")
        except (RuntimeError, OSError) as exc:
            st.toast(str(exc), icon="❌")
