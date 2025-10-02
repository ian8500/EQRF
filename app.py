import os
import json
import time
import glob
from urllib.parse import unquote
from threading import Lock
from functools import lru_cache
from datetime import timedelta

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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
PDF_DIR = os.path.join(BASE_DIR, 'pdfs')              # keep PDFs here (matches your zip)
JPG_DIR = os.path.join(BASE_DIR, 'static', 'jpgs')    # pre-rendered pages

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(JPG_DIR, exist_ok=True)

# Ensure poppler tools are in PATH (for pdf2image under systemd/gunicorn on Pi)
os.environ['PATH'] += os.pathsep + '/usr/bin'

# ---------------------- App ---------------------- #
app = Flask(__name__)
app.secret_key = 'your-strong-secret-key'

# Strong client caching for static assets & JPG images
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=365)

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
should_refresh = False
active_users = 0
user_lock = Lock()

# ---------------------- Helpers: JSON ---------------------- #
EXTRACTS_JSON = os.path.join(DATA_DIR, 'extracts.json')
CHECKLISTS_JSON = os.path.join(DATA_DIR, 'checklists.json')


def _read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


@lru_cache(maxsize=1)
def get_extracts():
    return _read_json(EXTRACTS_JSON, {})


@lru_cache(maxsize=1)
def get_checklists():
    return _read_json(CHECKLISTS_JSON, {})


def save_extracts(extracts):
    _write_json(EXTRACTS_JSON, extracts)
    get_extracts.cache_clear()


def save_checklists(data):
    _write_json(CHECKLISTS_JSON, data)
    get_checklists.cache_clear()

# ---------------------- Helpers: category tree ---------------------- #

def _ensure_node_for_path(root, parts):
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


def _get_node_for_path(root, parts):
    node = root
    for p in parts:
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return None
    return node


def _list_files_in_node(node):
    """Return a list of filenames for either style (list or dict with '__files__')."""
    if isinstance(node, list):
        return list(node)
    if isinstance(node, dict):
        files = node.get('__files__', [])
        if isinstance(files, list):
            return files
    return []


def _set_files_in_node(node, files_list):
    if isinstance(node, list):
        # convert to dict-style to be consistent going forward
        node = {'__files__': list(files_list)}
        return node
    if isinstance(node, dict):
        node['__files__'] = list(files_list)
        return node
    return {'__files__': list(files_list)}


def _delete_category_path(root, parts):
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

def _pdf_base(filename):
    return os.path.splitext(filename)[0]


def _jpg_glob_for_pdf(filename):
    base = _pdf_base(filename)
    pattern = os.path.join(JPG_DIR, f"{base}_page*.jpg")
    return sorted(glob.glob(pattern), key=lambda p: int(os.path.splitext(p)[0].rsplit('_page', 1)[-1]))


def _jpg_names_for_pdf(filename):
    return [os.path.basename(p) for p in _jpg_glob_for_pdf(filename)]


def _ensure_jpgs_for_pdf(pdf_path, filename, dpi=100, quality=68, max_width=1600):
    """Render JPGs if missing. Admin normally does this at upload time, but this is a safety net."""
    jpgs = _jpg_glob_for_pdf(filename)
    if jpgs:
        return [os.path.basename(p) for p in jpgs]

    pages = convert_from_path(pdf_path, dpi=dpi)
    out = []
    for i, page in enumerate(pages, start=1):
        if page.width > max_width:
            scale = max_width / float(page.width)
            new_size = (max_width, int(page.height * scale))
            page = page.resize(new_size, Image.LANCZOS)
        img = page.convert('RGB')
        out_name = f"{_pdf_base(filename)}_page{i}.jpg"
        out_path = os.path.join(JPG_DIR, out_name)
        img.save(out_path, 'JPEG', quality=quality, optimize=True, progressive=True, subsampling=2)
        out.append(out_name)
    return out


def _detect_orientation_from_first_jpg(jpg_names):
    if not jpg_names:
        return 'portrait'
    first = os.path.join(JPG_DIR, jpg_names[0])
    try:
        with Image.open(first) as im:
            return 'landscape' if im.width >= im.height else 'portrait'
    except Exception:
        return 'portrait'

# ---------------------- Auth (lightweight) ---------------------- #

def is_logged_in():
    return bool(session.get('logged_in'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        expected = os.environ.get('EQRF_PASSWORD', 'admin')
        if password == expected:
            session['logged_in'] = True
            flash('Logged in.', 'success')
            return redirect(url_for('admin_panel'))
        flash('Invalid password.', 'error')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('home'))

# ---------------------- SSE Refresh ---------------------- #

@app.route('/stream')
def stream():
    global active_users

    def event_stream():
        global should_refresh, active_users
        try:
            with user_lock:
                active_users += 1
            counter = 0
            # Initial retry suggestion in case of network hiccups
            yield 'retry: 2000\n\n'
            while True:
                time.sleep(1)
                counter += 1
                if should_refresh:
                    should_refresh = False
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
    global should_refresh
    should_refresh = True
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

    pdf_path = os.path.join(PDF_DIR, filename)
    if not os.path.exists(pdf_path):
        return f"PDF not found on disk: {filename}", 404

    jpgs = _jpg_names_for_pdf(filename)
    if not jpgs:
        # Safety net: render if missing (normally done at upload time)
        jpgs = _ensure_jpgs_for_pdf(pdf_path, filename)

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
def admin_panel():
    if not is_logged_in():
        return redirect(url_for('login'))
    extracts = get_extracts()
    return render_template('admin_register.html', extracts=extracts)


@app.route('/admin/upload_pdf', methods=['POST'])
def upload_pdf():
    """Register a PDF under a (possibly nested) category, convert to JPGs, update extracts.json."""
    if not is_logged_in():
        return redirect(url_for('login'))

    file = request.files.get('file')
    category = (request.form.get('category') or '').strip().strip('/')

    if not file or not file.filename.lower().endswith('.pdf'):
        flash('Please choose a PDF file.', 'error')
        return redirect(url_for('admin_panel'))

    safe_name = secure_filename(file.filename)
    pdf_path = os.path.join(PDF_DIR, safe_name)
    file.save(pdf_path)

    # Convert to JPGs at admin time (faster viewing later)
    try:
        pages = convert_from_path(pdf_path, dpi=int(os.environ.get('PDF_DPI', '100')))
        for i, page in enumerate(pages, start=1):
            # keep files compact for Pi decode speed
            max_width = int(os.environ.get('MAX_WIDTH', '1600'))
            if page.width > max_width:
                scale = max_width / float(page.width)
                new_size = (max_width, int(page.height * scale))
                page = page.resize(new_size, Image.LANCZOS)
            img = page.convert('RGB')
            out_name = f"{_pdf_base(safe_name)}_page{i}.jpg"
            out_path = os.path.join(JPG_DIR, out_name)
            img.save(out_path, 'JPEG', quality=int(os.environ.get('JPEG_QUALITY', '68')), optimize=True, progressive=True, subsampling=2)
    except Exception as e:
        flash(f'PDF conversion failed: {e}', 'error')
        return redirect(url_for('admin_panel'))

    # Update extracts.json
    extracts = get_extracts().copy()
    parts = [unquote(p) for p in category.split('/') if p] if category else []
    if parts:
        node = _ensure_node_for_path(extracts, parts)
    else:
        # Files at root level: keep a top-level list (legacy) under a special key if desired.
        # We'll store them under a pseudo-root named 'MISC' if not specified.
        node = _ensure_node_for_path(extracts, ['MISC'])
        category = 'MISC'

    files = _list_files_in_node(node)
    if safe_name not in files:
        files.append(safe_name)
    # Ensure back into node
    if isinstance(node, dict):
        node['__files__'] = files
    else:
        # convert legacy list to dict
        parent = extracts
        for p in [unquote(p) for p in category.split('/') if p][:-1]:
            parent = parent[p]
        parent[parts[-1]] = {'__files__': files}

    save_extracts(extracts)

    # Trigger clients to refresh
    global should_refresh
    should_refresh = True

    flash(f'Uploaded and registered {safe_name} under {category or "root"}.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_pdf', methods=['POST'])
def delete_pdf():
    if not is_logged_in():
        return redirect(url_for('login'))

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
        else:
            # legacy -> convert
            node = {'__files__': files}
        # Remove JPGs (keep original PDF unless you want it deleted too)
        for jpg in _jpg_names_for_pdf(filename):
            try:
                os.remove(os.path.join(JPG_DIR, jpg))
            except FileNotFoundError:
                pass
        save_extracts(extracts)
        global should_refresh
        should_refresh = True
        flash(f'Deleted {filename} from {category}.', 'info')
    else:
        flash('File not found in category.', 'error')

    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_category', methods=['POST'])
def delete_category():
    if not is_logged_in():
        return redirect(url_for('login'))

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
                    os.remove(os.path.join(JPG_DIR, jpg))
                except FileNotFoundError:
                    pass
        save_extracts(extracts)
        global should_refresh
        should_refresh = True
        flash(f'Deleted category {category}.', 'info')
    else:
        flash('Failed to delete category.', 'error')

    return redirect(url_for('admin_panel'))

# ---------------------- Admin: checklists (view/edit via simple text) ---------------------- #

@app.route('/admin/checklists')
def admin_checklists():
    if not is_logged_in():
        return redirect(url_for('login'))
    data = get_checklists()
    return render_template('admin_checklists.html', data=data)


@app.route('/admin/checklists/edit', methods=['GET', 'POST'])
def admin_checklist_edit():
    if not is_logged_in():
        return redirect(url_for('login'))

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
            global should_refresh
            should_refresh = True
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
    app.run(debug=True, host='0.0.0.0', port=8000)
