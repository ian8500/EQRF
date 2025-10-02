import os
import json
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache, wraps
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Iterable, List, MutableMapping, Optional
from urllib.parse import unquote

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_from_directory,
    flash,
    Response,
)
from werkzeug.utils import secure_filename

# Optional: used only for admin-time PDF→JPG conversion
from pdf2image import convert_from_path
from PIL import Image

# ---------------------- Paths & Env ---------------------- #
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
PDF_DIR = BASE_DIR / 'pdfs'              # keep PDFs here (matches your zip)
JPG_DIR = BASE_DIR / 'static' / 'jpgs'   # pre-rendered pages

DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
JPG_DIR.mkdir(parents=True, exist_ok=True)

# Ensure poppler tools are in PATH (for pdf2image under systemd/gunicorn on Pi)
os.environ['PATH'] += os.pathsep + '/usr/bin'

# ---------------------- App Configuration ---------------------- #


@dataclass(frozen=True)
class Settings:
    """Runtime configuration sourced from environment variables."""

    secret_key: str = os.environ.get('EQRF_SECRET_KEY') or os.environ.get('SECRET_KEY', 'change-me')
    admin_password: str = os.environ.get('EQRF_PASSWORD', 'admin')
    pdf_dpi: int = int(os.environ.get('PDF_DPI', '100'))
    max_width: int = int(os.environ.get('MAX_WIDTH', '1600'))
    jpeg_quality: int = int(os.environ.get('JPEG_QUALITY', '68'))
    debug: bool = os.environ.get('FLASK_DEBUG', '0') in {'1', 'true', 'True'}
    host: str = os.environ.get('FLASK_RUN_HOST', '0.0.0.0')
    port: int = int(os.environ.get('FLASK_RUN_PORT', os.environ.get('PORT', '8000')))


SETTINGS = Settings()

app = Flask(__name__)
app.secret_key = SETTINGS.secret_key

# Strong client caching for static assets & JPG images
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=365)
app.config.update(
    PDF_DPI=SETTINGS.pdf_dpi,
    MAX_WIDTH=SETTINGS.max_width,
    JPEG_QUALITY=SETTINGS.jpeg_quality,
)

@app.after_request
def add_caching_headers(resp):
    p = request.path or ''
    if p.startswith('/static/') or p.startswith('/jpgs/'):
        resp.headers.setdefault('Cache-Control', 'public, max-age=31536000, immutable')
    return resp

# Inject enumerate as a Jinja helper (fixes the missing filter error)
@app.context_processor
def utility_processor():
    return dict(enumerate=enumerate)

# ---------------------- Globals (SSE) ---------------------- #
active_users = 0
user_lock = Lock()
refresh_event = Event()

# ---------------------- Helpers: JSON ---------------------- #
EXTRACTS_JSON = DATA_DIR / 'extracts.json'
CHECKLISTS_JSON = DATA_DIR / 'checklists.json'


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


@lru_cache(maxsize=1)
def get_extracts() -> Dict[str, Any]:
    return _read_json(EXTRACTS_JSON, {})


@lru_cache(maxsize=1)
def get_checklists() -> Dict[str, Any]:
    return _read_json(CHECKLISTS_JSON, {})


def save_extracts(extracts: Dict[str, Any]) -> None:
    _write_json(EXTRACTS_JSON, extracts)
    get_extracts.cache_clear()


def save_checklists(data: Dict[str, Any]) -> None:
    _write_json(CHECKLISTS_JSON, data)
    get_checklists.cache_clear()

# ---------------------- Helpers: category tree ---------------------- #

def _ensure_node_for_path(root: MutableMapping[str, Any], parts: Iterable[str]) -> MutableMapping[str, Any]:
    """Create intermediate dict nodes for a path like ['AIR','SID'].
    Returns the final node dict.
    Format supports both old (list at key) and new (dict with '__files__').
    """
    node = root
    for p in parts:
        if p not in node or not isinstance(node[p], (dict, list)):
            node[p] = {}
        # If it's a list (legacy), convert to dict layout with '__files__'
        if isinstance(node[p], list):
            node[p] = {'__files__': node[p]}
        node = node[p]
    if isinstance(node, list):
        node = {'__files__': node}
    if isinstance(node, dict) and '__files__' not in node:
        node.setdefault('__files__', [])
    return node


def _get_node_for_path(root: MutableMapping[str, Any], parts: Iterable[str]) -> Optional[Any]:
    node = root
    for p in parts:
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return None
    return node


def _list_files_in_node(node: Any) -> List[str]:
    """Return a list of filenames for either style (list or dict with '__files__')."""
    if isinstance(node, list):
        return list(node)
    if isinstance(node, dict):
        files = node.get('__files__', [])
        if isinstance(files, list):
            return files
    return []


def _delete_category_path(root: MutableMapping[str, Any], parts: Iterable[str]) -> bool:
    if not parts:
        return False
    node = root
    for i, p in enumerate(parts):
        if not isinstance(node, dict) or p not in node:
            return False
        if i == len(parts) - 1:
            del node[p]
            return True
        node = node[p]

# ---------------------- Helpers: files & images ---------------------- #

def _pdf_base(filename: str) -> str:
    return Path(filename).stem


def _jpg_glob_for_pdf(filename: str) -> List[Path]:
    base = _pdf_base(filename)
    pattern = f"{base}_page*.jpg"

    def _sort_key(path: Path) -> int:
        try:
            suffix = path.stem.rsplit('_page', 1)[-1]
            return int(suffix)
        except (ValueError, IndexError):
            return 0

    return sorted(JPG_DIR.glob(pattern), key=_sort_key)


def _jpg_names_for_pdf(filename: str) -> List[str]:
    return [path.name for path in _jpg_glob_for_pdf(filename)]


def _ensure_jpgs_for_pdf(
    pdf_path: Path,
    filename: str,
    *,
    dpi: int,
    quality: int,
    max_width: int,
) -> List[str]:
    """Render JPGs if missing. Admin normally does this at upload time, but this is a safety net."""
    jpgs = _jpg_glob_for_pdf(filename)
    if jpgs:
        return [path.name for path in jpgs]

    pages = convert_from_path(str(pdf_path), dpi=dpi)
    out: List[str] = []
    for i, page in enumerate(pages, start=1):
        if page.width > max_width:
            scale = max_width / float(page.width)
            new_size = (max_width, int(page.height * scale))
            page = page.resize(new_size, Image.LANCZOS)
        img = page.convert('RGB')
        out_name = f"{_pdf_base(filename)}_page{i}.jpg"
        out_path = JPG_DIR / out_name
        img.save(str(out_path), 'JPEG', quality=quality, optimize=True, progressive=True, subsampling=2)
        out.append(out_name)
    return out


def _detect_orientation_from_first_jpg(jpg_names: List[str]) -> str:
    if not jpg_names:
        return 'portrait'
    first = JPG_DIR / jpg_names[0]
    try:
        with Image.open(first) as im:
            return 'landscape' if im.width >= im.height else 'portrait'
    except Exception:
        return 'portrait'

# ---------------------- Auth (lightweight) ---------------------- #

def is_logged_in():
    return bool(session.get('logged_in'))


def _safe_redirect_target(target: Optional[str]) -> str:
    """Prevent open redirects by only allowing intra-site destinations."""

    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return url_for('admin_panel')


def login_required(func):
    """Decorator that redirects unauthenticated users to the login page."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if is_logged_in():
            return func(*args, **kwargs)

        flash('Please log in to continue.', 'error')
        next_url = request.full_path.rstrip('?') if request.method == 'GET' else (request.referrer or url_for('admin_panel'))
        return redirect(url_for('login', next=next_url))

    return wrapper


def trigger_client_refresh() -> None:
    """Signal all connected browsers to refresh via Server Sent Events."""

    refresh_event.set()


@app.route('/login', methods=['GET', 'POST'])
def login():
    next_target = request.args.get('next', '')
    if request.method == 'POST':
        password = request.form.get('password', '')
        expected = SETTINGS.admin_password
        if password == expected:
            session['logged_in'] = True
            flash('Logged in.', 'success')
            next_url = _safe_redirect_target(request.form.get('next'))
            return redirect(next_url)
        flash('Invalid password.', 'error')
        next_target = request.form.get('next', next_target)
    return render_template('login.html', next=next_target)


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('home'))

# ---------------------- SSE Refresh ---------------------- #

@app.route('/stream')
def stream():
    def event_stream():
        global active_users
        try:
            with user_lock:
                active_users += 1
            counter = 0
            # Initial retry suggestion in case of network hiccups
            yield 'retry: 2000\n\n'
            while True:
                triggered = refresh_event.wait(timeout=1)
                counter += 1
                if triggered:
                    refresh_event.clear()
                    yield 'event: refresh\n'
                    yield 'data: true\n\n'
                elif counter % 15 == 0:
                    # heartbeat to keep the connection alive on Pi networks
                    yield ': keep-alive\n\n'
        finally:
            with user_lock:
                active_users = max(0, active_users - 1)

    return Response(event_stream(), mimetype='text/event-stream')


@app.route('/trigger-refresh', methods=['POST'])
@app.route('/trigger_refresh', methods=['POST'])  # legacy alias
def trigger_refresh():
    trigger_client_refresh()
    return 'OK'

# ---------------------- Home ---------------------- #

@app.route('/')
def home():
    extracts = get_extracts()
    # Home quick refs stored either as list at '--', or dict {'__files__':[...]} depending on historical format
    home_node = extracts.get('--', [])
    if isinstance(home_node, dict):
        home_pdfs = home_node.get('__files__', [])
    else:
        home_pdfs = home_node
    return render_template('home.html', home_pdfs=home_pdfs)

# ---------------------- Checklists ---------------------- #

@app.route('/checklists')
def checklist_index():
    data = get_checklists()
    return render_template('checklists.html', categories=data.keys())


@app.route('/checklists/<path:category>')
def checklist_category(category):
    data = get_checklists()
    parts = [unquote(p) for p in category.split('/') if p]
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return f"Checklist category not found: {part}", 404
    if isinstance(current, dict):
        return render_template('checklist_list.html', parent=category, subcategories=current)
    elif isinstance(current, list):
        return render_template('checklist_view.html', parent=category, checklist=current)
    else:
        return f"Invalid checklist structure at: {category}", 500

# ---------------------- Extracts (categories & viewer) ---------------------- #

@app.route('/extracts')
def extracts_index():
    extracts = get_extracts()
    categories = {k: v for k, v in extracts.items() if k != '--'}
    return render_template('extracts_index.html', extracts=categories)


# Legacy single-level route kept for compatibility
@app.route('/extracts/<category>')
def extracts_category_legacy(category):
    return extracts_category(category)


# Preferred nested route
@app.route('/extracts/<path:subpath>')
def extracts_category(subpath):
    extracts = get_extracts()
    parts = [unquote(p) for p in (subpath or '').split('/') if p]
    node = _get_node_for_path(extracts, parts)
    if node is None:
        return f"Extracts category not found: {subpath}", 404

    files = _list_files_in_node(node)
    if len(files) == 1:
        # Auto-open the single file in this category
        return redirect(url_for('extracts_viewer', category=subpath, filename=files[0]))

    return render_template('extracts_category.html', category=subpath, files=files, node=node)


# Viewer by explicit filename (canonical)
@app.route('/viewer/<path:category>/<path:filename>')
def extracts_viewer(category, filename):
    extracts = get_extracts()
    parts = [unquote(p) for p in (category or '').split('/') if p]
    node = _get_node_for_path(extracts, parts)
    if node is None:
        return f"Category not found: {category}", 404

    files = _list_files_in_node(node)
    if filename not in files:
        return f"File not found in category: {filename}", 404

    pdf_path = PDF_DIR / filename
    if not pdf_path.exists():
        return f"PDF not found on disk: {filename}", 404

    jpgs = _jpg_names_for_pdf(filename)
    if not jpgs:
        # Safety net: render if missing (normally done at upload time)
        jpgs = _ensure_jpgs_for_pdf(
            pdf_path,
            filename,
            dpi=SETTINGS.pdf_dpi,
            quality=SETTINGS.jpeg_quality,
            max_width=SETTINGS.max_width,
        )

    orientation = _detect_orientation_from_first_jpg(jpgs)

    item = {
        'pdf': filename,
        'jpgs': jpgs,
        'orientation': orientation,
    }
    return render_template('extracts_viewer.html', item=item)


# Legacy viewer by index, e.g. /viewer/AIR/SID/2  (1-based index)
@app.route('/viewer/<path:category>/<int:index>')
def extracts_viewer_by_index(category, index):
    extracts = get_extracts()
    parts = [unquote(p) for p in (category or '').split('/') if p]
    node = _get_node_for_path(extracts, parts)
    if node is None:
        return f"Category not found: {category}", 404

    files = _list_files_in_node(node)
    if not files:
        return f"No files registered for: {category}", 404
    if index < 1 or index > len(files):
        return f"Index out of range for {category}: {index}", 404
    return redirect(url_for('extracts_viewer', category=category, filename=files[index - 1]))

# ---------------------- Static file senders ---------------------- #

@app.route('/jpgs/<path:filename>')
def send_jpg(filename):
    return send_from_directory(JPG_DIR, filename)


@app.route('/pdfs/<path:filename>')
def send_pdf(filename):
    return send_from_directory(PDF_DIR, filename)

# ---------------------- Admin: register & manage extracts ---------------------- #

@app.route('/admin', methods=['GET'])
@login_required
def admin_panel():
    extracts = get_extracts()
    return render_template('admin_register.html', extracts=extracts)


@app.route('/admin/upload_pdf', methods=['POST'])
@login_required
def upload_pdf():
    """Register a PDF under a (possibly nested) category, convert to JPGs, update extracts.json."""

    file = request.files.get('file')
    category = (request.form.get('category') or '').strip().strip('/')

    if not file or not file.filename.lower().endswith('.pdf'):
        flash('Please choose a PDF file.', 'error')
        return redirect(url_for('admin_panel'))

    safe_name = secure_filename(file.filename)
    pdf_path = PDF_DIR / safe_name
    file.save(str(pdf_path))

    # Convert to JPGs at admin time (faster viewing later)
    try:
        pages = convert_from_path(str(pdf_path), dpi=SETTINGS.pdf_dpi)
        for i, page in enumerate(pages, start=1):
            # keep files compact for Pi decode speed
            if page.width > SETTINGS.max_width:
                scale = SETTINGS.max_width / float(page.width)
                new_size = (SETTINGS.max_width, int(page.height * scale))
                page = page.resize(new_size, Image.LANCZOS)
            img = page.convert('RGB')
            out_name = f"{_pdf_base(safe_name)}_page{i}.jpg"
            out_path = JPG_DIR / out_name
            img.save(
                str(out_path),
                'JPEG',
                quality=SETTINGS.jpeg_quality,
                optimize=True,
                progressive=True,
                subsampling=2,
            )
    except Exception as e:
        flash(f'PDF conversion failed: {e}', 'error')
        return redirect(url_for('admin_panel'))

    # Update extracts.json
    extracts = get_extracts().copy()
    parts = [unquote(p) for p in category.split('/') if p] if category else []
    if not parts:
        # Files at root level: keep a top-level list (legacy) under a special key if desired.
        parts = ['MISC']
        category = 'MISC'

    node = _ensure_node_for_path(extracts, parts)

    files = _list_files_in_node(node)
    if safe_name not in files:
        files.append(safe_name)
    # Ensure back into node
    if isinstance(node, dict):
        node['__files__'] = files

    save_extracts(extracts)

    trigger_client_refresh()

    flash(f'Uploaded and registered {safe_name} under {category or "root"}.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_pdf', methods=['POST'])
@login_required
def delete_pdf():

    category = (request.form.get('category') or '').strip().strip('/')
    filename = request.form.get('filename')
    if not filename:
        flash('Missing filename.', 'error')
        return redirect(url_for('admin_panel'))

    extracts = get_extracts().copy()
    parts = [unquote(p) for p in category.split('/') if p] if category else []
    node = _get_node_for_path(extracts, parts) if parts else extracts

    if node is None:
        flash('Category not found.', 'error')
        return redirect(url_for('admin_panel'))

    files = _list_files_in_node(node)
    if filename in files:
        files.remove(filename)
        if isinstance(node, dict):
            node['__files__'] = files
        elif parts:
            # legacy -> convert and reassign on parent structure
            parent = extracts
            for part in parts[:-1]:
                parent = parent.get(part, {})
            parent[parts[-1]] = {'__files__': files}
        # Remove JPGs (keep original PDF unless you want it deleted too)
        for jpg in _jpg_names_for_pdf(filename):
            try:
                (JPG_DIR / jpg).unlink()
            except FileNotFoundError:
                pass
        save_extracts(extracts)
        trigger_client_refresh()
        flash(f'Deleted {filename} from {category}.', 'info')
    else:
        flash('File not found in category.', 'error')

    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_category', methods=['POST'])
@login_required
def delete_category():

    category = (request.form.get('category') or '').strip().strip('/')
    parts = [unquote(p) for p in category.split('/') if p]
    extracts = get_extracts().copy()

    # Collect JPGs to remove for every file under this subtree
    def _collect_files(node, bag):
        if isinstance(node, dict):
            bag.extend(node.get('__files__', []))
            for k, v in node.items():
                if k == '__files__':
                    continue
                _collect_files(v, bag)
        elif isinstance(node, list):
            bag.extend(node)

    node = _get_node_for_path(extracts, parts)
    if node is None:
        flash('Category not found.', 'error')
        return redirect(url_for('admin_panel'))

    all_files = []
    _collect_files(node, all_files)

    if _delete_category_path(extracts, parts):
        # Remove JPGs (keep original PDFs)
        for fn in all_files:
            for jpg in _jpg_names_for_pdf(fn):
                try:
                    (JPG_DIR / jpg).unlink()
                except FileNotFoundError:
                    pass
        save_extracts(extracts)
        trigger_client_refresh()
        flash(f'Deleted category {category}.', 'info')
    else:
        flash('Failed to delete category.', 'error')

    return redirect(url_for('admin_panel'))

# ---------------------- Admin: checklists (view/edit via simple text) ---------------------- #

@app.route('/admin/checklists')
@login_required
def admin_checklists():
    data = get_checklists()
    return render_template('admin_checklists.html', data=data)


@app.route('/admin/checklists/edit', methods=['GET', 'POST'])
@login_required
def admin_checklist_edit():

    path = (request.values.get('path') or '').strip().strip('/')
    parts = [unquote(p) for p in path.split('/') if p]
    data = get_checklists().copy()

    # Traverse
    current = data
    parent = None
    last_key = None
    for p in parts:
        parent = current
        last_key = p
        if isinstance(current, dict) and p in current:
            current = current[p]
        else:
            current = None
            break

    if request.method == 'POST':
        text = request.form.get('text', '')
        # Save only if current is a list endpoint
        new_list = [line.strip() for line in text.splitlines() if line.strip()]
        if parent is not None and last_key is not None:
            parent[last_key] = new_list
            save_checklists(data)
            flash('Checklist saved.', 'success')
            trigger_client_refresh()
            return redirect(url_for('admin_checklists'))
        else:
            flash('Invalid checklist path.', 'error')
            return redirect(url_for('admin_checklists'))

    # GET view
    if isinstance(current, list):
        current_text = "\n".join(current)
    else:
        current_text = ''
    return render_template('admin_checklist_edit.html', path=path, text=current_text)

# ---------------------- Main ---------------------- #

if __name__ == '__main__':
    app.run(debug=SETTINGS.debug, host=SETTINGS.host, port=SETTINGS.port)
