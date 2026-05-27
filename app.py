import os
import json
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Tuple
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
    if p in {'/static/style.css', '/static/script.js'}:
        resp.headers['Cache-Control'] = 'no-cache'
    elif p.startswith('/static/') or p.startswith('/jpgs/'):
        resp.headers.setdefault('Cache-Control', 'public, max-age=31536000, immutable')
    return resp

# Inject enumerate as a Jinja helper (fixes the missing filter error)
@app.context_processor
def utility_processor():
    return dict(
        enumerate=enumerate,
        file_entry_name=file_entry_name,
    )

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


def _json_clone(data: Any) -> Any:
    """Detach mutable JSON data from the cached object before admin edits."""
    return json.loads(json.dumps(data))


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


def normalise_category_path(path: Any) -> str:
    raw = unquote(str(path or '')).strip().replace('\\', '/')
    if not raw:
        return ''
    if raw == '--':
        return '--'

    raw = raw.strip('/')
    raw_parts = raw.split('/')
    parts: List[str] = []
    for part in raw_parts:
        clean = part.strip()
        if not clean:
            continue
        if clean in {'.', '..'}:
            raise ValueError('Category path contains an unsafe component.')
        parts.append(clean)

    if not parts:
        return ''
    return '/'.join(parts)


def safe_path_parts(path: Any) -> List[str]:
    normalised = normalise_category_path(path)
    if not normalised:
        return []
    return normalised.split('/')


def normalise_file_entry(entry: Any) -> Dict[str, Any]:
    """Return a consistent dict for legacy string and newer dict file entries."""
    if isinstance(entry, dict):
        item = dict(entry)
        item['pdf'] = str(item.get('pdf') or '')
        if not isinstance(item.get('jpgs'), list):
            item['jpgs'] = []
        item.setdefault('orientation', 'portrait')
        return item
    if isinstance(entry, str):
        return {'pdf': entry, 'jpgs': [], 'orientation': 'portrait'}
    return {'pdf': '', 'jpgs': [], 'orientation': 'portrait'}


def file_entry_name(entry: Any) -> str:
    return normalise_file_entry(entry)['pdf']


def file_entry_jpgs(entry: Any) -> List[str]:
    jpgs = normalise_file_entry(entry)['jpgs']
    return [str(jpg) for jpg in jpgs]


def _list_file_entries_in_node(node: Any) -> List[Any]:
    """Return raw file entries for either style without changing their format."""
    if isinstance(node, list):
        return list(node)
    if isinstance(node, dict):
        files = node.get('__files__', [])
        if isinstance(files, list):
            return list(files)
    return []


def _list_files_in_node(node: Any) -> List[str]:
    """Return filenames for either style (strings or dicts in '__files__')."""
    return [name for name in (file_entry_name(entry) for entry in _list_file_entries_in_node(node)) if name]


def _find_file_entry(node: Any, filename: str) -> Optional[Any]:
    for entry in _list_file_entries_in_node(node):
        if file_entry_name(entry) == filename:
            return entry
    return None


def flatten_extract_categories(extracts: Any) -> List[Dict[str, Any]]:
    categories: List[Dict[str, Any]] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            subcategories = [k for k in node.keys() if k != '__files__']
            files = _list_file_entries_in_node(node)
            if path:
                categories.append({
                    'path': path,
                    'node': node,
                    'files': files,
                    'file_count': len([entry for entry in files if file_entry_name(entry)]),
                    'subcategory_count': len(subcategories),
                    'empty': not files and not subcategories,
                })
            for key in subcategories:
                child_path = f'{path}/{key}' if path else key
                visit(node[key], child_path)
        elif isinstance(node, list) and path:
            categories.append({
                'path': path,
                'node': node,
                'files': list(node),
                'file_count': len([entry for entry in node if file_entry_name(entry)]),
                'subcategory_count': 0,
                'empty': not node,
            })

    if isinstance(extracts, dict):
        for key, value in extracts.items():
            if key == '__files__':
                continue
            visit(value, key)
    return categories


def flatten_extract_files(extracts: Any) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    for category in flatten_extract_categories(extracts):
        for entry in category['files']:
            item = normalise_file_entry(entry)
            filename = item.get('pdf', '')
            if filename:
                files.append({
                    'category': category['path'],
                    'entry': entry,
                    'metadata': item,
                    'filename': filename,
                    'title': item.get('title') or _pdf_base(filename),
                    'jpgs': file_entry_jpgs(entry),
                    'pdf_exists': (PDF_DIR / filename).exists(),
                })
    return files


def flatten_checklist_paths(checklists: Any) -> List[Dict[str, Any]]:
    paths: List[Dict[str, Any]] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, list):
            paths.append({
                'path': path,
                'items': node,
                'item_count': len([line for line in node if str(line).strip()]),
            })
        elif isinstance(node, dict):
            for key, value in node.items():
                visit(value, f'{path}/{key}' if path else key)

    visit(checklists, '')
    return paths


def count_checklist_items(checklists: Any) -> int:
    return sum(item['item_count'] for item in flatten_checklist_paths(checklists))


def _count_checklist_categories(checklists: Any) -> int:
    count = 0

    def visit(node: Any) -> None:
        nonlocal count
        if not isinstance(node, dict):
            return
        for value in node.values():
            if isinstance(value, dict):
                count += 1
                visit(value)

    visit(checklists)
    return count


def _find_invalid_checklist_structures(checklists: Any) -> List[Dict[str, str]]:
    invalid: List[Dict[str, str]] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, list):
            return
        if isinstance(node, dict):
            for key, value in node.items():
                visit(value, f'{path}/{key}' if path else key)
            return
        invalid.append({'path': path or '(root)', 'message': 'Checklist node is not a folder or checklist list.'})

    visit(checklists, '')
    return invalid


def find_missing_pdfs(extracts: Any, pdf_dir: Path = PDF_DIR) -> List[Dict[str, Any]]:
    return [
        item for item in flatten_extract_files(extracts)
        if not (pdf_dir / item['filename']).exists()
    ]


def find_missing_jpgs(extracts: Any, jpg_dir: Path = JPG_DIR) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []
    for item in flatten_extract_files(extracts):
        jpgs = item['jpgs']
        missing_names = [jpg for jpg in jpgs if not (jpg_dir / jpg).exists()]
        if not jpgs or missing_names:
            issue = dict(item)
            issue['missing_jpgs'] = missing_names
            missing.append(issue)
    return missing


def find_orphan_jpgs(extracts: Any, jpg_dir: Path = JPG_DIR) -> List[str]:
    referenced = {
        jpg for item in flatten_extract_files(extracts)
        for jpg in item['jpgs']
    }
    return sorted(path.name for path in jpg_dir.glob('*.jpg') if path.name not in referenced)


def find_duplicate_pdf_entries(extracts: Any) -> List[Dict[str, Any]]:
    files = flatten_extract_files(extracts)
    counts = Counter(item['filename'] for item in files)
    return [
        {
            'filename': filename,
            'count': count,
            'categories': [item['category'] for item in files if item['filename'] == filename],
        }
        for filename, count in sorted(counts.items())
        if count > 1
    ]


def find_extract_health_issues(extracts: Any) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for item in find_missing_pdfs(extracts):
        issues.append({
            'type': 'missing_pdf',
            'severity': 'critical',
            'category': item['category'],
            'filename': item['filename'],
            'message': f"Source PDF missing: {item['filename']}",
        })
    for item in find_missing_jpgs(extracts):
        if item['jpgs']:
            detail = ', '.join(item.get('missing_jpgs') or [])
            message = f"Missing rendered JPGs for {item['filename']}: {detail}"
        else:
            message = f"No rendered JPGs registered for {item['filename']}"
        issues.append({
            'type': 'missing_jpg',
            'severity': 'warning',
            'category': item['category'],
            'filename': item['filename'],
            'message': message,
        })
    for duplicate in find_duplicate_pdf_entries(extracts):
        issues.append({
            'type': 'duplicate_pdf',
            'severity': 'warning',
            'filename': duplicate['filename'],
            'message': f"Duplicate PDF registration: {duplicate['filename']} appears {duplicate['count']} times.",
        })
    for category in flatten_extract_categories(extracts):
        if category['empty']:
            issues.append({
                'type': 'empty_extract_category',
                'severity': 'info',
                'category': category['path'],
                'message': f"Empty extract category: {category['path']}",
            })
    return issues


def remove_pdf_entry(extracts: MutableMapping[str, Any], category: str, filename: str) -> bool:
    parts = safe_path_parts(category)
    node = _get_node_for_path(extracts, parts) if parts else extracts
    if node is None:
        return False

    entries = _list_file_entries_in_node(node)
    remaining = [entry for entry in entries if file_entry_name(entry) != filename]
    if len(remaining) == len(entries):
        return False

    if isinstance(node, dict):
        node['__files__'] = remaining
    elif parts:
        parent = extracts
        for part in parts[:-1]:
            parent = parent.get(part, {})
        parent[parts[-1]] = {'__files__': remaining}
    return True


def upsert_pdf_entry(
    extracts: MutableMapping[str, Any],
    category: str,
    metadata: Dict[str, Any],
    replace: bool = False,
) -> None:
    filename = metadata.get('pdf')
    if not filename:
        raise ValueError('Missing PDF filename.')

    parts = safe_path_parts(category)
    if not parts:
        parts = ['MISC']
    node = _ensure_node_for_path(extracts, parts)
    entries = _list_file_entries_in_node(node)

    found = False
    updated: List[Any] = []
    for entry in entries:
        if file_entry_name(entry) == filename:
            if not replace:
                raise ValueError('Duplicate file in category.')
            updated.append(metadata)
            found = True
        else:
            updated.append(entry)

    if not found:
        updated.append(metadata)
    if isinstance(node, dict):
        node['__files__'] = updated


def upsert_checklist(
    checklists: MutableMapping[str, Any],
    path: str,
    lines: Iterable[str],
    overwrite_folder: bool = False,
) -> None:
    parts = safe_path_parts(path)
    if not parts or parts == ['--']:
        raise ValueError('Checklist path is required.')

    cleaned_lines = [str(line).strip() for line in lines if str(line).strip()]
    if not cleaned_lines:
        raise ValueError('Checklist must contain at least one item.')

    node: MutableMapping[str, Any] = checklists
    for part in parts[:-1]:
        current = node.get(part)
        if current is None:
            node[part] = {}
            current = node[part]
        if not isinstance(current, dict):
            raise ValueError('Checklist path conflicts with an existing checklist.')
        node = current

    final = parts[-1]
    current = node.get(final)
    if isinstance(current, dict) and not overwrite_folder:
        raise ValueError('Checklist path points to a folder.')
    node[final] = cleaned_lines


def delete_checklist(checklists: MutableMapping[str, Any], path: str) -> bool:
    parts = safe_path_parts(path)
    if not parts or parts == ['--']:
        return False

    node: Any = checklists
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    if not isinstance(node, dict):
        return False
    if not isinstance(node.get(parts[-1]), list):
        return False
    del node[parts[-1]]
    return True


def _category_paths(node: Any, prefix: str = '') -> List[str]:
    if not isinstance(node, dict):
        return []

    paths: List[str] = []
    for key, value in node.items():
        if key in {'__files__', '--'}:
            continue
        path = f'{prefix}/{key}' if prefix else key
        paths.append(path)
        paths.extend(_category_paths(value, path))
    return paths


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


def _detect_orientation_from_jpg_path(path: Path) -> str:
    try:
        with Image.open(path) as im:
            return 'landscape' if im.width >= im.height else 'portrait'
    except Exception:
        return 'portrait'


def _render_pdf_to_jpg_dir(pdf_path: Path, output_dir: Path, filename: str) -> Tuple[List[str], str]:
    pages = convert_from_path(str(pdf_path), dpi=SETTINGS.pdf_dpi)
    jpgs: List[str] = []
    base = _pdf_base(filename)
    for i, page in enumerate(pages, start=1):
        if page.width > SETTINGS.max_width:
            scale = SETTINGS.max_width / float(page.width)
            page = page.resize((SETTINGS.max_width, int(page.height * scale)), Image.LANCZOS)
        img = page.convert('RGB')
        out_name = f'{base}_page{i}.jpg'
        img.save(
            str(output_dir / out_name),
            'JPEG',
            quality=SETTINGS.jpeg_quality,
            optimize=True,
            progressive=True,
            subsampling=2,
        )
        jpgs.append(out_name)

    orientation = 'portrait'
    if jpgs:
        orientation = _detect_orientation_from_jpg_path(output_dir / jpgs[0])
    return jpgs, orientation


def _remove_jpg_files(names: Iterable[str]) -> None:
    for name in names:
        try:
            (JPG_DIR / name).unlink()
        except FileNotFoundError:
            pass


def _issue_lookup(issues: List[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    lookup: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for issue in issues:
        category = issue.get('category')
        filename = issue.get('filename')
        if category and filename:
            lookup.setdefault((category, filename), []).append(issue)
    return lookup


def _json_status() -> Dict[str, Dict[str, Any]]:
    status: Dict[str, Dict[str, Any]] = {}
    for label, path in {'extracts': EXTRACTS_JSON, 'checklists': CHECKLISTS_JSON}.items():
        readable = True
        message = 'OK'
        try:
            with path.open('r', encoding='utf-8') as f:
                json.load(f)
        except FileNotFoundError:
            readable = False
            message = 'File missing'
        except Exception as exc:
            readable = False
            message = str(exc)
        status[label] = {
            'path': str(path.relative_to(BASE_DIR)),
            'readable': readable,
            'writable': os.access(path, os.W_OK) if path.exists() else os.access(path.parent, os.W_OK),
            'message': message,
        }
    return status


def _admin_context() -> Dict[str, Any]:
    extracts = get_extracts()
    checklists = get_checklists()
    extract_categories = flatten_extract_categories(extracts)
    extract_files = flatten_extract_files(extracts)
    checklist_paths = flatten_checklist_paths(checklists)
    extract_issues = find_extract_health_issues(extracts)
    invalid_checklists = _find_invalid_checklist_structures(checklists)
    orphan_jpgs = find_orphan_jpgs(extracts)
    missing_pdfs = find_missing_pdfs(extracts)
    missing_jpgs = find_missing_jpgs(extracts)
    duplicate_pdfs = find_duplicate_pdf_entries(extracts)

    all_issues = list(extract_issues)
    all_issues.extend({
        'type': 'orphan_jpg',
        'severity': 'info',
        'filename': jpg,
        'message': f'Unreferenced JPG: {jpg}',
    } for jpg in orphan_jpgs)
    all_issues.extend({
        'type': 'invalid_checklist',
        'severity': 'critical',
        'category': issue['path'],
        'message': issue['message'],
    } for issue in invalid_checklists)

    lookup = _issue_lookup(extract_issues)
    category_rows: List[Dict[str, Any]] = []
    for category in extract_categories:
        files = []
        for entry in category['files']:
            item = normalise_file_entry(entry)
            filename = item.get('pdf', '')
            if not filename:
                continue
            file_issues = lookup.get((category['path'], filename), [])
            files.append({
                'filename': filename,
                'title': item.get('title') or _pdf_base(filename),
                'metadata': item,
                'jpgs': file_entry_jpgs(entry),
                'issue_count': len(file_issues),
                'issues': file_issues,
                'pdf_exists': (PDF_DIR / filename).exists(),
            })
        category_rows.append({**category, 'files': files})

    return {
        'extracts': extracts,
        'checklists': checklists,
        'extract_paths': _category_paths(extracts),
        'extract_categories': category_rows,
        'extract_files': extract_files,
        'checklist_paths': checklist_paths,
        'overview': {
            'extract_categories': len(extract_categories),
            'registered_pdfs': len(extract_files),
            'checklist_categories': _count_checklist_categories(checklists),
            'checklist_items': count_checklist_items(checklists),
            'warning_count': len(all_issues),
        },
        'health': {
            'issues': all_issues,
            'missing_pdfs': missing_pdfs,
            'missing_jpgs': missing_jpgs,
            'orphan_jpgs': orphan_jpgs,
            'empty_categories': [category for category in extract_categories if category['empty']],
            'invalid_checklists': invalid_checklists,
            'duplicate_pdfs': duplicate_pdfs,
            'json_status': _json_status(),
        },
    }

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
        expected = os.environ.get('EQRF_PASSWORD', SETTINGS.admin_password)
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
    return render_template('checklists.html', categories=data)


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
        return render_template('checklist_view.html', category=category, items=current)
    else:
        return f"Invalid checklist structure at: {category}", 500

# ---------------------- Extracts (categories & viewer) ---------------------- #

@app.route('/extracts')
def extracts_index():
    extracts = get_extracts()
    categories = {k: v for k, v in extracts.items() if k not in {'--', '__files__'}}
    root_files = _list_files_in_node(extracts)
    return render_template('extracts_index.html', categories=categories, root_files=root_files)


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

    entries = _list_file_entries_in_node(node)
    files = _list_files_in_node(node)

    subcategories = {}
    if isinstance(node, dict):
        subcategories = {k: v for k, v in node.items() if k != '__files__'}

    if len(files) == 1 and not subcategories:
        # Auto-open leaf categories that contain exactly one file.
        return redirect(url_for('extracts_viewer', category=subpath, filename=files[0]))

    items = [
        {'filename': file_entry_name(entry), 'label': _pdf_base(file_entry_name(entry))}
        for entry in entries
        if file_entry_name(entry)
    ]
    return render_template(
        'extracts_category.html',
        category=subpath,
        files=files,
        items=items,
        node=node,
        subcategories=subcategories,
    )


# Viewer by explicit filename (canonical)
@app.route('/viewer/<path:category>/<path:filename>')
def extracts_viewer(category, filename):
    extracts = get_extracts()
    parts = [unquote(p) for p in (category or '').split('/') if p]
    node = _get_node_for_path(extracts, parts)
    if node is None:
        return f"Category not found: {category}", 404

    entry = _find_file_entry(node, filename)
    if entry is None:
        return f"File not found in category: {filename}", 404

    pdf_path = PDF_DIR / filename
    if not pdf_path.exists():
        return f"PDF not found on disk: {filename}", 404

    item = normalise_file_entry(entry)
    jpgs = file_entry_jpgs(entry) or _jpg_names_for_pdf(filename)
    if not jpgs:
        # Safety net: render if missing (normally done at upload time)
        jpgs = _ensure_jpgs_for_pdf(
            pdf_path,
            filename,
            dpi=SETTINGS.pdf_dpi,
            quality=SETTINGS.jpeg_quality,
            max_width=SETTINGS.max_width,
        )

    orientation = item.get('orientation') or _detect_orientation_from_first_jpg(jpgs)

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
    return render_template('admin_dashboard.html', **_admin_context())


@app.route('/admin/upload_pdf', methods=['POST'])
@login_required
def upload_pdf():
    """Register a PDF only after the source and generated pages are complete."""

    upload = request.files.get('file')
    replace = request.form.get('replace') == 'true'
    orientation_mode = request.form.get('orientation', 'auto')
    raw_category = request.form.get('new_category') or request.form.get('category') or request.form.get('existing_category') or ''

    try:
        if not upload or not upload.filename:
            raise ValueError('Missing file.')
        safe_name = secure_filename(upload.filename)
        if not safe_name:
            raise ValueError('Missing file name.')
        if not safe_name.lower().endswith('.pdf'):
            raise ValueError('Not a PDF.')
        if orientation_mode not in {'auto', 'portrait', 'landscape'}:
            raise ValueError('Invalid orientation selection.')

        category = normalise_category_path(raw_category)
        if not category:
            category = 'MISC'

        extracts = _json_clone(get_extracts())
        parts = safe_path_parts(category)
        node = _get_node_for_path(extracts, parts)
        if node is not None and safe_name in _list_files_in_node(node) and not replace:
            raise ValueError('Duplicate file in category. Enable replace to overwrite the existing registration.')
        if (PDF_DIR / safe_name).exists() and not replace:
            raise ValueError('A PDF with that filename already exists. Enable replace to publish a new version.')

        with tempfile.TemporaryDirectory(prefix='eqrf-upload-', dir=BASE_DIR) as tmp:
            tmp_dir = Path(tmp)
            tmp_pdf = tmp_dir / safe_name
            upload.save(str(tmp_pdf))
            if tmp_pdf.stat().st_size == 0:
                raise ValueError('Missing file.')
            with tmp_pdf.open('rb') as f:
                if f.read(4) != b'%PDF':
                    raise ValueError('Not a PDF.')

            tmp_jpg_dir = tmp_dir / 'jpgs'
            tmp_jpg_dir.mkdir()
            try:
                jpgs, detected_orientation = _render_pdf_to_jpg_dir(tmp_pdf, tmp_jpg_dir, safe_name)
            except Exception as exc:
                raise ValueError(f'Conversion failed: {exc}') from exc
            if not jpgs:
                raise ValueError('Conversion failed: no JPG pages were generated.')

            orientation = detected_orientation if orientation_mode == 'auto' else orientation_mode
            existing_entry = _find_file_entry(node, safe_name) if node is not None else None
            old_jpgs = file_entry_jpgs(existing_entry) if existing_entry else _jpg_names_for_pdf(safe_name)
            metadata = {
                'pdf': safe_name,
                'title': _pdf_base(safe_name),
                'jpgs': jpgs,
                'orientation': orientation,
                'page_count': len(jpgs),
                'uploaded_at': datetime.now(timezone.utc).isoformat(),
                'source': 'admin',
            }

            tmp_pdf.replace(PDF_DIR / safe_name)
            _remove_jpg_files(old_jpgs)
            for jpg in jpgs:
                (tmp_jpg_dir / jpg).replace(JPG_DIR / jpg)

        upsert_pdf_entry(extracts, category, metadata, replace=replace)
        save_extracts(extracts)
        trigger_client_refresh()
        flash(f'Uploaded {safe_name} to {category}. {len(jpgs)} pages converted.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_pdf', methods=['POST'])
@login_required
def delete_pdf():
    category = request.values.get('category') or ''
    filename = request.values.get('filename')
    if not filename:
        flash('Missing filename.', 'error')
        return redirect(url_for('admin_panel'))
    try:
        category = normalise_category_path(category)
        extracts = _json_clone(get_extracts())
        node = _get_node_for_path(extracts, safe_path_parts(category))
        entry = _find_file_entry(node, filename) if node is not None else None
        if remove_pdf_entry(extracts, category, filename):
            if entry:
                _remove_jpg_files(file_entry_jpgs(entry))
            else:
                _remove_jpg_files(_jpg_names_for_pdf(filename))
            save_extracts(extracts)
            trigger_client_refresh()
            flash(f'Deleted {filename} from {category}.', 'info')
        else:
            flash('File not found in category.', 'error')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin_panel'))


@app.route('/admin/regenerate_pdf', methods=['POST'])
@login_required
def regenerate_pdf():
    category = request.form.get('category') or ''
    filename = request.form.get('filename') or ''
    if not filename:
        flash('Missing file.', 'error')
        return redirect(url_for('admin_panel'))

    try:
        category = normalise_category_path(category)
        extracts = _json_clone(get_extracts())
        node = _get_node_for_path(extracts, safe_path_parts(category))
        entry = _find_file_entry(node, filename) if node is not None else None
        if entry is None:
            raise ValueError('File not found in category.')
        pdf_path = PDF_DIR / filename
        if not pdf_path.exists():
            raise ValueError('Missing file.')

        with tempfile.TemporaryDirectory(prefix='eqrf-regen-', dir=BASE_DIR) as tmp:
            tmp_jpg_dir = Path(tmp)
            try:
                jpgs, detected_orientation = _render_pdf_to_jpg_dir(pdf_path, tmp_jpg_dir, filename)
            except Exception as exc:
                raise ValueError(f'Conversion failed: {exc}') from exc
            if not jpgs:
                raise ValueError('Conversion failed: no JPG pages were generated.')

            item = normalise_file_entry(entry)
            _remove_jpg_files(file_entry_jpgs(entry) or _jpg_names_for_pdf(filename))
            for jpg in jpgs:
                (tmp_jpg_dir / jpg).replace(JPG_DIR / jpg)

        item.update({
            'pdf': filename,
            'title': item.get('title') or _pdf_base(filename),
            'jpgs': jpgs,
            'orientation': item.get('orientation') or detected_orientation,
            'page_count': len(jpgs),
            'regenerated_at': datetime.now(timezone.utc).isoformat(),
            'source': item.get('source') or 'admin',
        })
        upsert_pdf_entry(extracts, category, item, replace=True)
        save_extracts(extracts)
        trigger_client_refresh()
        flash(f'Regenerated {len(jpgs)} JPG pages for {filename}.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_category', methods=['POST'])
@login_required
def delete_category():
    try:
        category = normalise_category_path(request.values.get('category') or '')
        if not category:
            raise ValueError('Category invalid.')
        parts = safe_path_parts(category)
        extracts = _json_clone(get_extracts())
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_panel'))

    # Collect JPGs to remove for every file under this subtree
    def _collect_files(node, bag):
        if isinstance(node, dict):
            bag.extend(_list_files_in_node(node))
            for k, v in node.items():
                if k == '__files__':
                    continue
                _collect_files(v, bag)
        elif isinstance(node, list):
            bag.extend(_list_files_in_node(node))

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
    checklists = get_checklists()
    return render_template(
        'admin_checklists.html',
        checklist_paths=flatten_checklist_paths(checklists),
        invalid_checklists=_find_invalid_checklist_structures(checklists),
    )


@app.route('/admin/checklists/new', methods=['GET', 'POST'])
@login_required
def admin_checklist_new():
    if request.method == 'POST':
        path = request.form.get('path') or ''
        text = request.form.get('lines') or ''
        lines = text.splitlines()
        overwrite_folder = request.form.get('overwrite_folder') == 'true'
        try:
            checklists = _json_clone(get_checklists())
            upsert_checklist(checklists, path, lines, overwrite_folder=overwrite_folder)
            save_checklists(checklists)
            trigger_client_refresh()
            flash(f'Checklist saved: {normalise_category_path(path)}', 'success')
            return redirect(url_for('admin_checklists'))
        except ValueError as exc:
            flash(str(exc), 'error')
            return render_template(
                'admin_checklist_edit.html',
                mode='new',
                path=path,
                original_path='',
                text=text,
            )
    return render_template('admin_checklist_edit.html', mode='new', path='', original_path='', text='')


@app.route('/admin/checklists/edit', methods=['GET', 'POST'])
@login_required
def admin_checklist_edit():
    if request.method == 'POST':
        path = request.form.get('path') or ''
        text = request.form.get('lines') or ''
        original_path = request.form.get('original_path') or path
        overwrite_folder = request.form.get('overwrite_folder') == 'true'
        try:
            normalised_path = normalise_category_path(path)
            normalised_original = normalise_category_path(original_path)
            checklists = _json_clone(get_checklists())
            upsert_checklist(checklists, normalised_path, text.splitlines(), overwrite_folder=overwrite_folder)
            if normalised_original != normalised_path:
                delete_checklist(checklists, normalised_original)
            save_checklists(checklists)
            trigger_client_refresh()
            flash(f'Checklist saved: {normalised_path}', 'success')
            return redirect(url_for('admin_checklists'))
        except ValueError as exc:
            flash(str(exc), 'error')
            return render_template('admin_checklist_edit.html', mode='edit', path=path, original_path=original_path, text=text)

    path = request.args.get('path') or ''
    try:
        normalised = normalise_category_path(path)
        current = _get_node_for_path(get_checklists(), safe_path_parts(normalised))
        if not isinstance(current, list):
            flash('Checklist not found.', 'error')
            return redirect(url_for('admin_checklists'))
        return render_template(
            'admin_checklist_edit.html',
            mode='edit',
            path=normalised,
            original_path=normalised,
            text='\n'.join(current),
        )
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_checklists'))


@app.route('/admin/checklists/delete', methods=['POST'])
@login_required
def admin_checklist_delete():
    path = request.form.get('path') or ''
    try:
        checklists = _json_clone(get_checklists())
        if delete_checklist(checklists, path):
            save_checklists(checklists)
            trigger_client_refresh()
            flash(f'Deleted checklist: {normalise_category_path(path)}', 'info')
        else:
            flash('Checklist not found.', 'error')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin_checklists'))


@app.route('/admin/checklists/preview')
@login_required
def admin_checklist_preview():
    path = request.args.get('path') or ''
    try:
        normalised = normalise_category_path(path)
        current = _get_node_for_path(get_checklists(), safe_path_parts(normalised))
        if not isinstance(current, list):
            flash('Checklist not found.', 'error')
            return redirect(url_for('admin_checklists'))
        return render_template('checklist_view.html', category=normalised, items=current)
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_checklists'))

# ---------------------- Main ---------------------- #

if __name__ == '__main__':
    app.run(debug=SETTINGS.debug, host=SETTINGS.host, port=SETTINGS.port)
