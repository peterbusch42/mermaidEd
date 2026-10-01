import hashlib
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


# --- UI Header ---
st.title("🗂️ Diagram Gallery")
st.markdown(
    "Scan a folder for **Mermaid** (`.mmd`, `.mermaid`, ` ```mermaid ` blocks in `.md`) and "
    "**draw.io** (`.drawio`, `.dio`, `.drawio.svg`, `.drawio.png`) files and see them all at a glance. "
    "**Double-click** a card to open it in the draw.io desktop app — Mermaid diagrams are converted into editable draw.io shapes."
)

col_dir, col_rec, col_btn = st.columns([6, 1.6, 1], vertical_alignment="bottom")
directory = col_dir.text_input("Folder", key="gallery_dir", placeholder="/path/to/your/diagrams",
                               on_change=persisted("gallery_dir", ""))
recursive = col_rec.checkbox("Include subfolders", key="gallery_recursive",
                             on_change=persisted("gallery_recursive", True))
if col_btn.button("🔄 Rescan", width="stretch"):
    load_file.clear()

if not directory.strip():
    st.info("Enter a folder path above to build the overview.")
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
        for entry in load_file(str(f), stat.st_mtime, stat.st_size):
            rel_dir = f.parent.relative_to(root).as_posix()
            entries.append({**entry, "dir": "" if rel_dir == "." else rel_dir})

if truncated:
    st.warning(f"Stopped after {diagram_scan.MAX_FILES} files — pick a more specific folder to see everything.")
if not entries:
    st.info("No Mermaid or draw.io diagrams found in this folder.")
    st.stop()

n_drawio = sum(e["kind"] == "drawio" for e in entries)
st.caption(f"Found **{len(entries)}** diagrams in **{len({e['path'] for e in entries})}** files — "
           f"{len(entries) - n_drawio} Mermaid · {n_drawio} draw.io")

# Changes whenever a file is added, removed or edited, so the browser only rebuilds the grid when needed
signature = hashlib.sha1(
    repr([(e["path"], e["block"], e["mtime"], e["size"]) for e in entries]).encode()
).hexdigest()

result = gallery_view(
    key="diagram_gallery",
    data={"items": entries, "sig": signature, "hasDrawio": diagram_scan.find_drawio_app() is not None},
    on_open_change=lambda: None,
)

# ============================================================
# DOUBLE-CLICK HANDLER
# ============================================================
request = result.open
if request:
    # Only open files that are part of the current scan — never an arbitrary path from the browser
    entry = next((e for e in entries if e["path"] == request.get("path") and e["block"] == request.get("block")), None)
    if entry is None:
        st.toast("That file is no longer part of the scan — try 🔄 Rescan.", icon="⚠️")
    else:
        try:
            diagram_scan.open_in_drawio(diagram_scan.drawio_file_for(entry))
            label = f"{entry['name']} #{entry['block']}" if entry["block"] else entry["name"]
            st.toast(f"Opening **{label}** in draw.io …", icon="🚀")
        except (RuntimeError, OSError) as exc:
            st.toast(str(exc), icon="❌")
