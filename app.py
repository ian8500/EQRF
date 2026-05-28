import os
import json
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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
    jsonify,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

# ---------------------- Paths & Env ---------------------- #
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
PDF_DIR = BASE_DIR / 'pdfs'              # keep PDFs here (matches your zip)
JPG_DIR = BASE_DIR / 'static' / 'jpgs'   # legacy pre-rendered pages, no longer used by the public viewer

DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
JPG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------- App Configuration ---------------------- #


@dataclass(frozen=True)
class Settings:
    """Runtime configuration sourced from environment variables."""

    secret_key: str = field(default_factory=lambda: os.environ.get('EQRF_SECRET_KEY') or os.environ.get('SECRET_KEY', 'change-me'))
    admin_password: str = field(default_factory=lambda: os.environ.get('EQRF_PASSWORD', 'admin'))
    debug: bool = field(default_factory=lambda: str(os.environ.get('FLASK_DEBUG', '0')).strip().lower() in {'1', 'true'})
    host: str = field(default_factory=lambda: os.environ.get('FLASK_RUN_HOST', '0.0.0.0'))
    port: int = field(default_factory=lambda: int(os.environ.get('FLASK_RUN_PORT', os.environ.get('PORT', '8000'))))
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get('EQRF_MAX_UPLOAD_MB', '100')))


SETTINGS = Settings()

app = Flask(__name__)
app.secret_key = SETTINGS.secret_key
app.debug = False
app.config['MAX_CONTENT_LENGTH'] = SETTINGS.max_upload_mb * 1024 * 1024

# Strong client caching for static assets and locally served PDFs.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=365)
@app.after_request
def add_caching_headers(resp):
    p = request.path or ''
    if p in {'/static/style.css', '/static/script.js'}:
        resp.headers['Cache-Control'] = 'no-cache'
    elif p.startswith('/pdfs/'):
        resp.headers.setdefault('Cache-Control', 'public, max-age=86400')
        resp.headers.setdefault('Accept-Ranges', 'bytes')
    elif p.startswith('/static/'):
        resp.headers.setdefault('Cache-Control', 'public, max-age=31536000, immutable')
    return resp

# Inject enumerate as a Jinja helper (fixes the missing filter error)
@app.context_processor
def utility_processor():
    return dict(
        enumerate=enumerate,
        file_entry_name=file_entry_name,
        is_critical_checklist_line=is_critical_checklist_line,
    )

# ---------------------- Globals (SSE) ---------------------- #
active_users = 0
user_lock = Lock()
refresh_event = Event()

# ---------------------- Helpers: JSON ---------------------- #
EXTRACTS_JSON = DATA_DIR / 'extracts.json'
CHECKLISTS_JSON = DATA_DIR / 'checklists.json'
AUDIT_LOG_JSON = DATA_DIR / 'audit_log.json'
PDF_TEXT_CACHE_JSON = DATA_DIR / 'pdf_text_cache.json'

UNSAFE_SECRET_VALUES = {'', 'change-me', 'change-this', 'change-this-to-a-long-random-string'}
UNSAFE_PASSWORD_VALUES = {'', 'admin', 'change-this', 'change-this-admin-password'}


def production_safety_warnings(settings: Settings = SETTINGS) -> List[str]:
    warnings: List[str] = []
    secret = str(settings.secret_key or '').strip()
    password = str(settings.admin_password or '').strip()
    if secret in UNSAFE_SECRET_VALUES:
        warnings.append('EQRF_SECRET_KEY is not set to a production-safe value.')
    if password in UNSAFE_PASSWORD_VALUES:
        warnings.append('EQRF_PASSWORD is not set to a production-safe value.')
    return warnings


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


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def get_audit_log() -> List[Dict[str, Any]]:
    if not AUDIT_LOG_JSON.exists():
        save_audit_log([])
    entries = _read_json(AUDIT_LOG_JSON, [])
    if not isinstance(entries, list):
        return []
    return entries


def save_audit_log(entries: List[Dict[str, Any]]) -> None:
    _write_json(AUDIT_LOG_JSON, entries)


def _current_audit_user() -> str:
    try:
        return 'admin' if session.get('is_admin') else 'system'
    except RuntimeError:
        return 'system'


def append_audit_log(
    action: str,
    target_type: str,
    target_path: str,
    summary: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    entries = get_audit_log()
    entries.append({
        'timestamp': _utc_timestamp(),
        'user': _current_audit_user(),
        'action': action,
        'target_type': target_type,
        'target_path': target_path,
        'summary': summary,
        'details': details or {},
    })
    save_audit_log(entries)


def latest_audit_entries(limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    entries = list(reversed(get_audit_log()))
    if limit is None:
        return entries
    return entries[:limit]


CONTENT_STATUSES = {'published', 'draft', 'hidden', 'archived'}


def is_na(value: Any) -> bool:
    return str(value or '').strip().upper() in {'', 'N/A', 'NA'}


def _normalise_na(value: Any) -> str:
    text = str(value or '').strip()
    return 'N/A' if is_na(text) else text


def default_content_metadata(title: Optional[str] = None) -> Dict[str, str]:
    return {
        'title': _normalise_na(title) if title else 'N/A',
        'version': 'N/A',
        'effective_date': 'N/A',
        'expiry_date': 'N/A',
        'review_date': 'N/A',
        'owner': 'N/A',
        'status': 'published',
        'last_updated': 'N/A',
    }


def normalise_content_metadata(metadata: Optional[Dict[str, Any]], title: Optional[str] = None) -> Dict[str, str]:
    data = default_content_metadata(title)
    if isinstance(metadata, dict):
        data.update({key: metadata.get(key) for key in data.keys() if key in metadata})
    for key in {'title', 'version', 'effective_date', 'expiry_date', 'review_date', 'owner', 'last_updated'}:
        data[key] = _normalise_na(data.get(key))
    status = str(data.get('status') or 'published').strip().lower()
    data['status'] = status if status in CONTENT_STATUSES else 'published'
    return data


def parse_optional_date(value: Any) -> Optional[date]:
    if is_na(value):
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f'Invalid date: {text}. Use YYYY-MM-DD or N/A.') from exc


def validate_content_metadata(metadata: Dict[str, Any]) -> Dict[str, str]:
    data = normalise_content_metadata(metadata, metadata.get('title') if isinstance(metadata, dict) else None)
    for key in {'effective_date', 'expiry_date', 'review_date'}:
        parse_optional_date(data.get(key))
    if data['status'] not in CONTENT_STATUSES:
        raise ValueError('Invalid status.')
    return data


def content_is_published(metadata: Dict[str, Any]) -> bool:
    return normalise_content_metadata(metadata).get('status') == 'published'


def content_is_effective(metadata: Dict[str, Any], today: Optional[date] = None) -> bool:
    effective = parse_optional_date(normalise_content_metadata(metadata).get('effective_date'))
    return effective is None or effective <= (today or date.today())


def content_is_expired(metadata: Dict[str, Any], today: Optional[date] = None) -> bool:
    expiry = parse_optional_date(normalise_content_metadata(metadata).get('expiry_date'))
    return bool(expiry and expiry < (today or date.today()))


def content_review_due(metadata: Dict[str, Any], today: Optional[date] = None) -> bool:
    review = parse_optional_date(normalise_content_metadata(metadata).get('review_date'))
    return bool(review and review <= (today or date.today()))


def metadata_is_public(metadata: Dict[str, Any], today: Optional[date] = None) -> bool:
    try:
        return content_is_published(metadata) and content_is_effective(metadata, today)
    except ValueError:
        return False


def metadata_status_label(metadata: Dict[str, Any]) -> str:
    data = normalise_content_metadata(metadata)
    status = data.get('status', 'published')
    if status != 'published':
        return status.title()
    try:
        if not content_is_effective(data):
            return 'Not yet effective'
        if content_is_expired(data):
            return 'Expired'
        if content_review_due(data):
            return 'Review due'
    except ValueError:
        return 'Invalid metadata'
    return 'Published'


def metadata_status_state(metadata: Dict[str, Any]) -> str:
    return metadata_status_label(metadata).lower().replace(' ', '-')


def metadata_from_form(default_title: Optional[str] = None, *, touch: bool = True) -> Dict[str, str]:
    data = validate_content_metadata({
        'title': request.form.get('title') or default_title or 'N/A',
        'version': request.form.get('version') or 'N/A',
        'effective_date': request.form.get('effective_date') or 'N/A',
        'expiry_date': request.form.get('expiry_date') or 'N/A',
        'review_date': request.form.get('review_date') or 'N/A',
        'owner': request.form.get('owner') or 'N/A',
        'status': request.form.get('status') or 'published',
        'last_updated': request.form.get('last_updated') or 'N/A',
    })
    if touch:
        data['last_updated'] = _utc_timestamp()
    return data

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


def normalise_orientation(value: Any, default: str = 'portrait') -> str:
    text = str(value or '').strip().lower()
    if text in {'landscape', 'portrait'}:
        return text
    return default if default in {'landscape', 'portrait'} else 'portrait'


def normalise_file_entry(entry: Any) -> Dict[str, Any]:
    """Return a consistent dict for legacy string and newer dict file entries."""
    if isinstance(entry, dict):
        item = dict(entry)
        item['pdf'] = str(item.get('pdf') or '')
        if not isinstance(item.get('jpgs'), list):
            item['jpgs'] = []
        item['orientation'] = normalise_orientation(item.get('orientation'))
        metadata = normalise_content_metadata(item, item.get('title') or Path(item['pdf']).stem or None)
        item.update(metadata)
        return item
    if isinstance(entry, str):
        filename = entry
        return {
            'pdf': filename,
            'jpgs': [],
            'orientation': 'portrait',
            **normalise_content_metadata({}, Path(filename).stem or None),
        }
    return {'pdf': '', 'jpgs': [], 'orientation': 'portrait', **normalise_content_metadata({})}


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


def is_checklist_leaf(node: Any) -> bool:
    return isinstance(node, list) or (
        isinstance(node, dict)
        and node.get('__type__') == 'checklist'
        and isinstance(node.get('items'), list)
    )


def normalise_checklist_node(node: Any, title: Optional[str] = None) -> Dict[str, Any]:
    if isinstance(node, dict) and node.get('__type__') == 'checklist':
        items = node.get('items') if isinstance(node.get('items'), list) else []
        metadata = normalise_content_metadata(node.get('metadata'), title or node.get('metadata', {}).get('title'))
        return {'__type__': 'checklist', 'metadata': metadata, 'items': items}
    if isinstance(node, list):
        return {
            '__type__': 'checklist',
            'metadata': normalise_content_metadata({}, title),
            'items': node,
        }
    return {
        '__type__': 'checklist',
        'metadata': normalise_content_metadata({}, title),
        'items': [],
    }


def checklist_items(node: Any) -> List[str]:
    if isinstance(node, list):
        return [str(line).strip() for line in node if str(line).strip()]
    if isinstance(node, dict) and node.get('__type__') == 'checklist':
        return [str(line).strip() for line in node.get('items', []) if str(line).strip()]
    return []


def checklist_metadata(node: Any, title: Optional[str] = None) -> Dict[str, str]:
    return normalise_checklist_node(node, title).get('metadata', normalise_content_metadata({}, title))


def is_valid_checklist_node(node: Any) -> bool:
    if not is_checklist_leaf(node):
        return False
    return bool(checklist_items(node) and metadata_is_public(checklist_metadata(node)))


def checklist_group_has_content(node: Any) -> bool:
    if is_valid_checklist_node(node):
        return True
    if isinstance(node, dict):
        return any(checklist_group_has_content(child) for child in node.values())
    return False


def filtered_checklist_tree(node: Any) -> Any:
    if is_valid_checklist_node(node):
        normalised = normalise_checklist_node(node)
        normalised['items'] = checklist_items(node)
        return normalised
    if isinstance(node, dict):
        filtered: Dict[str, Any] = {}
        for key, value in node.items():
            if key in {'__type__', 'metadata', 'items'}:
                continue
            child = filtered_checklist_tree(value)
            if checklist_group_has_content(child):
                filtered[key] = child
        return filtered
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


def pdf_is_registered(filename: str, extracts: Optional[Any] = None) -> bool:
    try:
        filename = _safe_pdf_filename(filename)
    except ValueError:
        return False
    data = get_extracts() if extracts is None else extracts

    def visit(node: Any) -> bool:
        if any(file_entry_name(entry) == filename for entry in _list_file_entries_in_node(node)):
            return True
        if isinstance(node, dict):
            return any(visit(value) for key, value in node.items() if key != '__files__')
        if isinstance(node, list):
            return any(file_entry_name(entry) == filename for entry in node)
        return False

    return visit(data)


def extract_entry_is_valid(entry: Any) -> bool:
    filename = file_entry_name(entry)
    metadata = normalise_file_entry(entry)
    return bool(
        filename
        and metadata_is_public(metadata)
        and local_pdf_exists(filename)
    )


def extract_group_has_content(node: Any) -> bool:
    if isinstance(node, list):
        return any(extract_entry_is_valid(entry) for entry in node)
    if isinstance(node, dict):
        if any(extract_entry_is_valid(entry) for entry in _list_file_entries_in_node(node)):
            return True
        return any(extract_group_has_content(child) for key, child in node.items() if key != '__files__')
    return False


def filtered_extract_tree(node: Any) -> Any:
    if isinstance(node, list):
        entries = [entry for entry in node if extract_entry_is_valid(entry)]
        return entries if entries else None
    if isinstance(node, dict):
        filtered: Dict[str, Any] = {}
        files = [entry for entry in _list_file_entries_in_node(node) if extract_entry_is_valid(entry)]
        if files:
            filtered['__files__'] = files
        for key, value in node.items():
            if key == '__files__':
                continue
            child = filtered_extract_tree(value)
            if extract_group_has_content(child):
                filtered[key] = child
        return filtered
    return None


def flatten_extract_paths(node: Any) -> List[str]:
    paths: List[str] = []

    def visit(current: Any, prefix: str) -> None:
        if not isinstance(current, dict):
            return
        if prefix and extract_group_has_content(current):
            paths.append(prefix)
        for key, value in current.items():
            if key == '__files__':
                continue
            visit(value, f'{prefix}/{key}' if prefix else key)

    visit(node, '')
    return paths


def flatten_valid_extract_files(node: Any) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []

    def visit(current: Any, category: str) -> None:
        if isinstance(current, list):
            for entry in current:
                if extract_entry_is_valid(entry):
                    filename = file_entry_name(entry)
                    files.append({
                        'category': category,
                        'entry': entry,
                        'filename': filename,
                        'title': get_display_title_for_pdf(entry),
                        'jpgs': file_entry_jpgs(entry),
                        'metadata': normalise_file_entry(entry),
                    })
            return
        if not isinstance(current, dict):
            return
        for entry in _list_file_entries_in_node(current):
            if extract_entry_is_valid(entry):
                filename = file_entry_name(entry)
                files.append({
                    'category': category,
                    'entry': entry,
                    'filename': filename,
                    'title': get_display_title_for_pdf(entry),
                    'jpgs': file_entry_jpgs(entry),
                    'metadata': normalise_file_entry(entry),
                })
        for key, value in current.items():
            if key == '__files__':
                continue
            visit(value, f'{category}/{key}' if category else key)

    visit(node, '')
    return files


def flatten_checklist_paths(checklists: Any) -> List[Dict[str, Any]]:
    paths: List[Dict[str, Any]] = []

    def visit(node: Any, path: str) -> None:
        if is_valid_checklist_node(node):
            cleaned = checklist_items(node)
            metadata = checklist_metadata(node, Path(path).name if path else None)
            paths.append({
                'path': path,
                'items': cleaned,
                'item_count': len(cleaned),
                'metadata': metadata,
                'status_label': metadata_status_label(metadata),
                'status_state': metadata_status_state(metadata),
            })
        elif isinstance(node, dict):
            for key, value in node.items():
                if key in {'__type__', 'metadata', 'items'}:
                    continue
                visit(value, f'{path}/{key}' if path else key)

    visit(checklists, '')
    return paths


def is_critical_checklist_line(line: Any) -> bool:
    return bool(re.search(r'CAT\s*A\s*(MIN|MINIMUM|ONLY)', str(line or ''), re.IGNORECASE))


def _flatten_all_checklist_paths(checklists: Any) -> List[Dict[str, Any]]:
    paths: List[Dict[str, Any]] = []

    def visit(node: Any, path: str) -> None:
        if is_checklist_leaf(node):
            metadata = checklist_metadata(node, Path(path).name if path else None)
            item_count = len(checklist_items(node))
            public_visible = item_count > 0 and metadata_is_public(metadata)
            if public_visible:
                visibility_label = metadata_status_label(metadata)
                visibility_state = metadata_status_state(metadata)
            elif item_count == 0:
                visibility_label = 'Hidden: empty checklist'
                visibility_state = 'hidden'
            else:
                visibility_label = f'Hidden: {metadata_status_label(metadata)}'
                visibility_state = metadata_status_state(metadata)
            paths.append({
                'path': path,
                'items': checklist_items(node),
                'item_count': item_count,
                'metadata': metadata,
                'public_visible': public_visible,
                'visibility_label': visibility_label,
                'visibility_state': visibility_state,
            })
        elif isinstance(node, dict):
            for key, value in node.items():
                if key in {'__type__', 'metadata', 'items'}:
                    continue
                visit(value, f'{path}/{key}' if path else key)

    visit(checklists, '')
    return paths


def count_checklist_items(checklists: Any) -> int:
    return sum(item['item_count'] for item in flatten_checklist_paths(checklists))


def _count_checklist_categories(checklists: Any) -> int:
    count = 0

    def visit(node: Any) -> None:
        nonlocal count
        if not isinstance(node, dict) or is_checklist_leaf(node):
            return
        for value in node.values():
            if isinstance(value, dict) and not is_checklist_leaf(value):
                count += 1
                visit(value)

    visit(checklists)
    return count


def _find_invalid_checklist_structures(checklists: Any) -> List[Dict[str, str]]:
    invalid: List[Dict[str, str]] = []

    def visit(node: Any, path: str) -> None:
        if is_checklist_leaf(node):
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {'__type__', 'metadata', 'items'}:
                    continue
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
    return []


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


def upsert_checklist_with_metadata(
    checklists: MutableMapping[str, Any],
    path: str,
    lines: Iterable[str],
    metadata: Dict[str, Any],
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
        if is_checklist_leaf(current):
            raise ValueError('Checklist path conflicts with an existing checklist.')
        if not isinstance(current, dict):
            raise ValueError('Checklist path conflicts with an invalid node.')
        node = current

    final = parts[-1]
    current = node.get(final)
    if isinstance(current, dict) and not is_checklist_leaf(current) and not overwrite_folder:
        raise ValueError('Checklist path points to a folder.')

    saved_metadata = validate_content_metadata({
        **metadata,
        'title': metadata.get('title') or final,
        'last_updated': metadata.get('last_updated') or _utc_timestamp(),
    })
    node[final] = {
        '__type__': 'checklist',
        'metadata': saved_metadata,
        'items': cleaned_lines,
    }


def update_checklist_metadata(checklists: MutableMapping[str, Any], path: str, metadata: Dict[str, Any]) -> None:
    parts = safe_path_parts(path)
    current = _get_node_for_path(checklists, parts)
    if not is_checklist_leaf(current):
        raise ValueError('Checklist not found.')
    upsert_checklist_with_metadata(checklists, path, checklist_items(current), metadata, overwrite_folder=True)


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
    if not is_checklist_leaf(node.get(parts[-1])):
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


def _safe_pdf_filename(filename: Any) -> str:
    value = str(filename or '').strip()
    if not value or value != Path(value).name or '/' in value or '\\' in value:
        raise ValueError('Invalid filename.')
    if not value.lower().endswith('.pdf'):
        raise ValueError('Invalid filename.')
    return value


def local_pdf_exists(filename: str) -> bool:
    return bool(filename and (PDF_DIR / filename).is_file())


def get_pdf_page_count(filename: str) -> int:
    try:
        filename = _safe_pdf_filename(filename)
        pdf_path = PDF_DIR / filename
        if not pdf_path.is_file():
            return 0
        from pypdf import PdfReader
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0


def detect_pdf_orientation_from_path(pdf_path: Path) -> str:
    try:
        if not pdf_path.is_file():
            return 'portrait'
        from pypdf import PdfReader
        page = PdfReader(str(pdf_path)).pages[0]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        return 'landscape' if width >= height else 'portrait'
    except Exception:
        return 'portrait'


def get_valid_jpgs_for_pdf(filename: str, metadata_jpgs: Optional[Iterable[str]] = None) -> List[str]:
    valid: List[str] = []
    seen = set()
    for jpg in metadata_jpgs or []:
        jpg_name = str(jpg)
        if jpg_name and jpg_name not in seen and (JPG_DIR / jpg_name).is_file():
            valid.append(jpg_name)
            seen.add(jpg_name)

    if valid:
        return valid

    for jpg_name in _jpg_names_for_pdf(filename):
        if jpg_name not in seen and (JPG_DIR / jpg_name).is_file():
            valid.append(jpg_name)
            seen.add(jpg_name)
    return valid


def load_pdf_text_cache() -> Dict[str, Any]:
    cache = _read_json(PDF_TEXT_CACHE_JSON, {})
    return cache if isinstance(cache, dict) else {}


def save_pdf_text_cache(cache: Dict[str, Any]) -> None:
    _write_json(PDF_TEXT_CACHE_JSON, cache)


def get_pdf_text_cache_key(filename: str) -> Dict[str, Any]:
    filename = _safe_pdf_filename(filename)
    pdf_path = PDF_DIR / filename
    stat = pdf_path.stat()
    return {
        'filename': filename,
        'mtime': stat.st_mtime,
        'size': stat.st_size,
    }


def extract_pdf_text_pages(filename: str) -> List[Dict[str, Any]]:
    filename = _safe_pdf_filename(filename)
    pdf_path = PDF_DIR / filename
    if not pdf_path.is_file():
        raise ValueError('PDF not found.')
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError('PDF text search is not available.') from exc

    pages: List[Dict[str, Any]] = []
    reader = PdfReader(str(pdf_path))
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ''
        pages.append({'page': index, 'text': text})
    return pages


def get_cached_pdf_text_pages(filename: str) -> List[Dict[str, Any]]:
    key = get_pdf_text_cache_key(filename)
    cache = load_pdf_text_cache()
    cached = cache.get(filename)
    if (
        isinstance(cached, dict)
        and cached.get('mtime') == key['mtime']
        and cached.get('size') == key['size']
        and isinstance(cached.get('pages'), list)
    ):
        return cached['pages']

    pages = extract_pdf_text_pages(filename)
    cache[filename] = {**key, 'pages': pages}
    save_pdf_text_cache(cache)
    return pages


def _search_snippet(text: str, start: int, end: int, radius: int = 72) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = ' '.join(text[left:right].split())
    if left > 0:
        snippet = '... ' + snippet
    if right < len(text):
        snippet += ' ...'
    return snippet


def search_pdf_text(filename: str, query: Any, limit: int = 100) -> List[Dict[str, Any]]:
    q = str(query or '').strip()
    if not q:
        return []
    q_lower = q.lower()
    results: List[Dict[str, Any]] = []
    for page in get_cached_pdf_text_pages(filename):
        text = str(page.get('text') or '')
        if not text:
            continue
        search_from = 0
        lower_text = text.lower()
        while len(results) < limit:
            match_at = lower_text.find(q_lower, search_from)
            if match_at < 0:
                break
            results.append({
                'page': int(page.get('page') or 0),
                'snippet': _search_snippet(text, match_at, match_at + len(q)),
            })
            search_from = match_at + len(q_lower)
    return results


def local_jpgs_exist_for_pdf(filename: str) -> bool:
    return bool(get_valid_jpgs_for_pdf(filename))


def get_display_title_for_pdf(entry: Any) -> str:
    item = normalise_file_entry(entry)
    title = str(item.get('title') or '').strip()
    if title:
        return title
    filename = item.get('pdf', '')
    return _pdf_base(filename) if filename else 'Untitled PDF'


def public_extract_item(entry: Any, category: str) -> Optional[Dict[str, Any]]:
    if not extract_entry_is_valid(entry):
        return None
    filename = file_entry_name(entry)
    metadata = normalise_file_entry(entry)
    page_count = int(metadata.get('page_count') or 0) or get_pdf_page_count(filename) or 1
    orientation = normalise_orientation(metadata.get('orientation'))
    return {
        'category': category,
        'entry': entry,
        'filename': filename,
        'label': get_display_title_for_pdf(entry),
        'title': get_display_title_for_pdf(entry),
        'jpgs': file_entry_jpgs(entry),
        'page_count': page_count,
        'orientation': orientation,
        'metadata': metadata,
        'status_label': metadata_status_label(metadata),
        'status_state': metadata_status_state(metadata),
    }


def public_extract_items_for_node(node: Any, category: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in _list_file_entries_in_node(node):
        item = public_extract_item(entry, category)
        if item:
            items.append(item)
    return items


def general_reference_entry_is_valid(entry: Any) -> bool:
    return extract_entry_is_valid(entry)


def get_general_reference_display_title(entry: Any) -> str:
    return get_display_title_for_pdf(entry)


def _misc_is_general_reference_only(node: Any) -> bool:
    if isinstance(node, list):
        return True
    if not isinstance(node, dict):
        return False
    return all(key == '__files__' for key in node.keys())


def is_general_reference_category(category: Any) -> bool:
    try:
        normalised = normalise_category_path(category)
    except ValueError:
        return False
    if normalised == '--':
        return True
    if normalised == 'MISC':
        return _misc_is_general_reference_only(get_extracts().get('MISC'))
    return False


def entry_source_category(entry: Any) -> str:
    item = normalise_file_entry(entry)
    return str(item.get('_source_category') or item.get('source_category') or '--')


def get_general_reference_entries() -> List[Dict[str, Any]]:
    extracts = get_extracts()
    entries: List[Dict[str, Any]] = []
    seen = set()

    def add_entries(node: Any, source_category: str) -> None:
        for entry in _list_file_entries_in_node(node):
            if not general_reference_entry_is_valid(entry):
                continue
            filename = file_entry_name(entry)
            key = (source_category, filename)
            if key in seen:
                continue
            seen.add(key)
            item = public_extract_item(entry, source_category)
            if item:
                item['source_category'] = source_category
                item['label'] = get_general_reference_display_title(entry)
                item['title'] = item['label']
                entries.append(item)

    if isinstance(extracts, dict):
        add_entries(extracts, '--')
        add_entries(extracts.get('--'), '--')
        misc_node = extracts.get('MISC')
        if _misc_is_general_reference_only(misc_node):
            add_entries(misc_node, 'MISC')

    return sorted(entries, key=lambda item: item['title'].lower())


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
    for label, path in {'extracts': EXTRACTS_JSON, 'checklists': CHECKLISTS_JSON, 'audit_log': AUDIT_LOG_JSON}.items():
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
            'path': str(path.relative_to(BASE_DIR) if path.is_relative_to(BASE_DIR) else path),
            'readable': readable,
            'writable': os.access(path, os.W_OK) if path.exists() else os.access(path.parent, os.W_OK),
            'message': message,
        }
    return status


def _governance_summary(extract_files: List[Dict[str, Any]], checklist_paths: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {
        'extracts_published': 0,
        'extracts_non_public': 0,
        'extracts_expired_or_review_due': 0,
        'checklists_published': 0,
        'checklists_non_public': 0,
        'checklists_expired_or_review_due': 0,
    }

    for item in extract_files:
        metadata = normalise_content_metadata(item.get('metadata', {}), item.get('title'))
        if metadata_is_public(metadata):
            summary['extracts_published'] += 1
        else:
            summary['extracts_non_public'] += 1
        try:
            dated_attention = content_is_expired(metadata) or content_review_due(metadata)
        except ValueError:
            dated_attention = True
        if dated_attention:
            summary['extracts_expired_or_review_due'] += 1

    for item in checklist_paths:
        metadata = normalise_content_metadata(item.get('metadata', {}), item.get('path'))
        if item.get('item_count', 0) > 0 and metadata_is_public(metadata):
            summary['checklists_published'] += 1
        else:
            summary['checklists_non_public'] += 1
        try:
            dated_attention = content_is_expired(metadata) or content_review_due(metadata)
        except ValueError:
            dated_attention = True
        if dated_attention:
            summary['checklists_expired_or_review_due'] += 1

    return summary


def _admin_context() -> Dict[str, Any]:
    extracts = get_extracts()
    checklists = get_checklists()
    extract_categories = flatten_extract_categories(extracts)
    extract_files = flatten_extract_files(extracts)
    checklist_paths = _flatten_all_checklist_paths(checklists)
    empty_checklists = [item for item in checklist_paths if not item['public_visible']]
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
    all_issues.extend({
        'type': 'empty_checklist',
        'severity': 'info',
        'category': item['path'],
        'message': f"Empty checklist hidden from public navigation: {item['path']}",
    } for item in empty_checklists)

    lookup = _issue_lookup(extract_issues)
    category_rows: List[Dict[str, Any]] = []
    for category in extract_categories:
        files = []
        for entry in category['files']:
            item = normalise_file_entry(entry)
            filename = item.get('pdf', '')
            if not filename:
                continue
            public_visible = extract_entry_is_valid(entry)
            if public_visible:
                visibility_label = metadata_status_label(item)
                visibility_state = metadata_status_state(item)
            elif not metadata_is_public(item):
                visibility_label = f'Hidden: {metadata_status_label(item)}'
                visibility_state = metadata_status_state(item)
            elif not local_pdf_exists(filename):
                visibility_label = 'Hidden: missing PDF'
                visibility_state = 'missing-pdf'
            else:
                visibility_label = 'Hidden: invalid metadata'
                visibility_state = 'hidden'
            file_issues = lookup.get((category['path'], filename), [])
            files.append({
                'filename': filename,
                'title': item.get('title') or _pdf_base(filename),
                'metadata': item,
                'status_label': metadata_status_label(item),
                'status_state': metadata_status_state(item),
                'jpgs': file_entry_jpgs(entry),
                'valid_jpgs': get_valid_jpgs_for_pdf(filename, item.get('jpgs', [])),
                'issue_count': len(file_issues),
                'issues': file_issues,
                'pdf_exists': (PDF_DIR / filename).exists(),
                'public_visible': public_visible,
                'visibility_label': visibility_label,
                'visibility_state': visibility_state,
            })
        category_rows.append({**category, 'files': files})

    governance = _governance_summary(extract_files, checklist_paths)

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
        'governance': governance,
        'recent_audit_entries': latest_audit_entries(10),
        'now_date': date.today().isoformat(),
        'health': {
            'issues': all_issues,
            'missing_pdfs': missing_pdfs,
            'missing_jpgs': missing_jpgs,
            'orphan_jpgs': orphan_jpgs,
            'empty_categories': [category for category in extract_categories if category['empty']],
            'invalid_checklists': invalid_checklists,
            'empty_checklists': empty_checklists,
            'duplicate_pdfs': duplicate_pdfs,
            'json_status': _json_status(),
            'production_warnings': production_safety_warnings(),
        },
    }

# ---------------------- Auth (lightweight) ---------------------- #

def is_logged_in():
    return bool(session.get('logged_in'))


def is_admin():
    return bool(session.get('is_admin'))


def _safe_redirect_target(target: Optional[str]) -> str:
    """Prevent open redirects by only allowing intra-site destinations."""

    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return url_for('admin_panel')


def login_required(func):
    """Decorator that redirects unauthenticated users to the login page."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if is_admin():
            return func(*args, **kwargs)

        flash('Please log in to continue.', 'error')
        next_url = request.full_path.rstrip('?') if request.method == 'GET' else (request.referrer or url_for('admin_panel'))
        return redirect(url_for('login', next=next_url))

    return wrapper


def trigger_client_refresh() -> None:
    """Signal all connected browsers to refresh via Server Sent Events."""

    refresh_event.set()


def is_safe_local_path(path: Any) -> bool:
    value = unquote(str(path or '')).strip()
    if not value or not value.startswith('/') or value.startswith('//'):
        return False
    lowered = value.lower()
    if lowered.startswith(('http://', 'https://')):
        return False
    path_only = value.split('?', 1)[0].split('#', 1)[0].replace('\\', '/')
    return all(part not in {'.', '..'} for part in path_only.split('/'))


def _clean_refresh_path(path: Any) -> str:
    value = unquote(str(path or '').strip())
    return value.split('?', 1)[0].split('#', 1)[0] or '/'


def _viewer_parts_for_refresh(path: str) -> Tuple[str, str]:
    raw = _clean_refresh_path(path)
    if not raw.startswith('/viewer/'):
        return '', ''
    parts = [part for part in raw.removeprefix('/viewer/').split('/') if part]
    if len(parts) < 2:
        return '', ''
    return '/'.join(parts[:-1]), parts[-1]


def path_exists_for_refresh(path: Any) -> bool:
    if not is_safe_local_path(path):
        return False
    current = _clean_refresh_path(path).rstrip('/') or '/'
    if current == '/':
        return True
    if current == '/admin':
        return is_admin()
    if current == '/checklists':
        return True
    if current.startswith('/checklists/'):
        try:
            subpath = normalise_category_path(current.removeprefix('/checklists/'))
        except ValueError:
            return False
        tree = filtered_checklist_tree(get_checklists()) or {}
        node = _get_node_for_path(tree, safe_path_parts(subpath))
        return is_valid_checklist_node(node) or (isinstance(node, dict) and checklist_group_has_content(node))
    if current == '/extracts':
        return True
    if current.startswith('/extracts/'):
        try:
            subpath = normalise_category_path(current.removeprefix('/extracts/'))
        except ValueError:
            return False
        tree = filtered_extract_tree(get_extracts()) or {}
        node = _get_node_for_path(tree, safe_path_parts(subpath))
        return extract_group_has_content(node)
    if current.startswith('/viewer/'):
        try:
            category, filename = _viewer_parts_for_refresh(current)
            category = normalise_category_path(category)
        except ValueError:
            return False
        if not category or not filename:
            return False
        node = _get_node_for_path(get_extracts(), safe_path_parts(category))
        entry = _find_file_entry(node, filename) if node is not None else None
        return bool(entry and extract_entry_is_valid(entry))
    return False


def parent_refresh_candidates(path: Any) -> List[str]:
    if not is_safe_local_path(path):
        return ['/']
    current = _clean_refresh_path(path).rstrip('/') or '/'
    if current in {'/', '/admin', '/checklists', '/extracts'}:
        return [current]

    if current.startswith('/viewer/'):
        category, filename = _viewer_parts_for_refresh(current)
        candidates = [current]
        parts = safe_path_parts(category)
        for index in range(len(parts), 0, -1):
            candidates.append('/extracts/' + '/'.join(parts[:index]))
        candidates.append('/extracts')
        return candidates

    for prefix in ('/checklists/', '/extracts/'):
        if current.startswith(prefix):
            base = prefix.rstrip('/')
            subpath = current.removeprefix(prefix)
            parts = safe_path_parts(subpath)
            candidates = [current]
            for index in range(len(parts) - 1, 0, -1):
                candidates.append(base + '/' + '/'.join(parts[:index]))
            candidates.append(base)
            return candidates

    return ['/']


def resolve_refresh_target(path: Any) -> str:
    for candidate in parent_refresh_candidates(path):
        if path_exists_for_refresh(candidate):
            return candidate
    return '/'


def _breadcrumb_items(root_label: str, root_endpoint: str, path: str, endpoint: str, param_name: str) -> List[Dict[str, Optional[str]]]:
    items = [{'label': root_label, 'url': url_for(root_endpoint)}]
    parts = safe_path_parts(path)
    current: List[str] = []
    for index, part in enumerate(parts):
        current.append(part)
        items.append({
            'label': part,
            'url': None if index == len(parts) - 1 else url_for(endpoint, **{param_name: '/'.join(current)}),
        })
    return items


def _breadcrumb_with_leaf(items: List[Dict[str, Optional[str]]], label: str) -> List[Dict[str, Optional[str]]]:
    return [*items, {'label': label, 'url': None}]


def _checklist_sibling_items(tree: Any, parent_path: str) -> List[Dict[str, str]]:
    parent = _get_node_for_path(tree, safe_path_parts(parent_path)) if parent_path else tree
    if not isinstance(parent, dict):
        return []

    siblings: List[Dict[str, str]] = []
    for name, value in parent.items():
        if is_valid_checklist_node(value):
            siblings.append({
                'label': name,
                'kind': 'Checklist',
                'url': url_for('checklist_category', category=f'{parent_path}/{name}'.strip('/')),
            })
        elif isinstance(value, dict) and checklist_group_has_content(value):
            siblings.append({
                'label': name,
                'kind': 'Group',
                'url': url_for('checklist_category', category=f'{parent_path}/{name}'.strip('/')),
            })
    return siblings


def _extract_sibling_items(tree: Any, category: str) -> List[Dict[str, str]]:
    node = _get_node_for_path(tree, safe_path_parts(category))
    if not isinstance(node, dict):
        return []

    siblings: List[Dict[str, str]] = []
    for name, value in node.items():
        if name == '__files__':
            continue
        if extract_group_has_content(value):
            siblings.append({
                'label': name,
                'kind': 'Category',
                'url': url_for('extracts_category', subpath=f'{category}/{name}'.strip('/')),
            })
    for item in public_extract_items_for_node(node, category):
        siblings.append({
            'label': item['title'],
            'kind': 'PDF',
            'url': url_for('extracts_viewer', category=category, filename=item['filename']),
        })
    return siblings


def _registered_extract_entry(category: str, filename: str) -> Optional[Any]:
    filename = _safe_pdf_filename(filename)
    normalised_category = normalise_category_path(category)
    node = _get_node_for_path(get_extracts(), safe_path_parts(normalised_category))
    if normalised_category == '--' and _find_file_entry(node, filename) is None:
        root_entry = _find_file_entry(get_extracts(), filename)
        if root_entry is not None:
            return root_entry
    if node is None:
        return None
    return _find_file_entry(node, filename)


def _unavailable(title: str, message: str, back_label: str, back_endpoint: str, status: int = 404):
    return render_template(
        'unavailable.html',
        title=title,
        message=message,
        back_label=back_label,
        back_url=url_for(back_endpoint),
    ), status


@app.errorhandler(404)
def handle_not_found(_error):
    return render_template(
        'unavailable.html',
        title='Page unavailable',
        message='The requested EQRF page is not available.',
        back_label='Home',
        back_url=url_for('home'),
    ), 404


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    return render_template(
        'unavailable.html',
        title='Upload too large',
        message=f'The uploaded file exceeds the {SETTINGS.max_upload_mb} MB EQRF upload limit.',
        back_label='Back to Admin',
        back_url=url_for('admin_panel') if is_admin() else url_for('login'),
    ), 413


@app.errorhandler(500)
def handle_server_error(_error):
    return render_template(
        'unavailable.html',
        title='Server error',
        message='EQRF could not complete that request.',
        back_label='Home',
        back_url=url_for('home'),
    ), 500


@app.route('/login', methods=['GET', 'POST'])
def login():
    next_target = request.args.get('next', '')
    if request.method == 'POST':
        password = request.form.get('password', '')
        expected = os.environ.get('EQRF_PASSWORD', SETTINGS.admin_password)
        if password == expected:
            session['logged_in'] = True
            session['is_admin'] = True
            append_audit_log('admin_login_success', 'auth', 'admin', 'Admin login successful')
            flash('Logged in.', 'success')
            next_url = _safe_redirect_target(request.form.get('next'))
            return redirect(next_url)
        flash('Invalid password.', 'error')
        next_target = request.form.get('next', next_target)
    return render_template('login.html', next=next_target)


@app.route('/logout')
def logout():
    if is_admin():
        append_audit_log('admin_logout', 'auth', 'admin', 'Admin logged out')
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
@login_required
def trigger_refresh():
    trigger_client_refresh()
    append_audit_log('trigger_refresh', 'system', 'clients', 'Triggered client refresh')
    if request.accept_mimetypes.best == 'application/json' or request.is_json:
        return jsonify({'ok': True})
    flash('Refresh triggered for connected clients.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/resolve-refresh-target')
def resolve_refresh_target_route():
    current = request.args.get('current') or '/'
    return jsonify({'target': resolve_refresh_target(current)})

# ---------------------- Home ---------------------- #

@app.route('/')
def home():
    checklist_tree = filtered_checklist_tree(get_checklists()) or {}
    extract_tree = filtered_extract_tree(get_extracts()) or {}
    non_home_extracts = {
        key: value for key, value in extract_tree.items()
        if key not in {'--', '__files__'} and not (key == 'MISC' and _misc_is_general_reference_only(value))
    } if isinstance(extract_tree, dict) else {}
    general_reference_pdfs = get_general_reference_entries()
    checklist_count = len(flatten_checklist_paths(checklist_tree))
    extract_count = len(flatten_valid_extract_files(non_home_extracts))
    return render_template(
        'home.html',
        general_reference_pdfs=general_reference_pdfs,
        has_checklists=checklist_group_has_content(checklist_tree),
        has_extracts=extract_group_has_content(non_home_extracts),
        checklist_count=checklist_count,
        extract_count=extract_count,
        quick_ref_count=len(general_reference_pdfs),
    )


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'app': 'EQRF',
        'mode': 'development' if app.debug else 'production',
    })


# ---------------------- Checklists ---------------------- #

@app.route('/checklists')
def checklist_index():
    data = filtered_checklist_tree(get_checklists()) or {}
    return render_template(
        'checklists.html',
        categories=data,
        checklist_count=len(flatten_checklist_paths(data)),
    )


@app.route('/checklists/<path:category>')
def checklist_category(category):
    try:
        normalised = normalise_category_path(category)
    except ValueError:
        return _unavailable('Checklist unavailable', 'This checklist path is not available in the published EQRF set.', 'Back to Checklists', 'checklist_index')

    data = filtered_checklist_tree(get_checklists()) or {}
    current = _get_node_for_path(data, safe_path_parts(normalised))
    if is_valid_checklist_node(current):
        parts = safe_path_parts(normalised)
        parent_path = '/'.join(parts[:-1]) if len(parts) > 1 else ''
        metadata = checklist_metadata(current, parts[-1] if parts else None)
        return render_template(
            'checklist_view.html',
            category=normalised,
            items=checklist_items(current),
            metadata=metadata,
            status_label=metadata_status_label(metadata),
            status_state=metadata_status_state(metadata),
            breadcrumbs=_breadcrumb_items('Checklists', 'checklist_index', normalised, 'checklist_category', 'category'),
            parent_path=parent_path,
            sibling_items=_checklist_sibling_items(data, parent_path),
        )
    if isinstance(current, dict) and checklist_group_has_content(current):
        parts = safe_path_parts(normalised)
        parent_path = '/'.join(parts[:-1]) if len(parts) > 1 else ''
        return render_template(
            'checklist_list.html',
            parent=normalised,
            subcategories=current,
            breadcrumbs=_breadcrumb_items('Checklists', 'checklist_index', normalised, 'checklist_category', 'category'),
            checklist_count=len(flatten_checklist_paths(current)),
            sibling_items=_checklist_sibling_items(data, parent_path),
        )
    return _unavailable('Checklist unavailable', 'This checklist is not currently published in the local EQRF content set.', 'Back to Checklists', 'checklist_index')

# ---------------------- Extracts (categories & viewer) ---------------------- #

@app.route('/extracts')
def extracts_index():
    extracts = filtered_extract_tree(get_extracts()) or {}
    categories = {
        k: v for k, v in extracts.items()
        if k not in {'--', '__files__'}
        and not (k == 'MISC' and _misc_is_general_reference_only(v))
        and extract_group_has_content(v)
    } if isinstance(extracts, dict) else {}
    return render_template(
        'extracts_index.html',
        categories=categories,
        root_files=[],
        extract_count=len(flatten_valid_extract_files(categories)),
    )


@app.route('/viewer-search')
def viewer_search():
    category = request.args.get('category') or ''
    filename = request.args.get('filename') or ''
    query = request.args.get('q') or ''
    try:
        filename = _safe_pdf_filename(filename)
        normalised_category = normalise_category_path(category)
        if not is_general_reference_category(normalised_category):
            return jsonify({'error': 'PDF search is only available for General Reference documents.', 'results': [], 'result_count': 0}), 403
        entry = _registered_extract_entry(normalised_category, filename)
        if entry is None or not extract_entry_is_valid(entry):
            return jsonify({'error': 'PDF not found.', 'results': [], 'result_count': 0}), 404
        if not local_pdf_exists(filename):
            return jsonify({'error': 'PDF not found.', 'results': [], 'result_count': 0}), 404
        results = search_pdf_text(filename, query)
        return jsonify({
            'query': str(query or '').strip(),
            'filename': filename,
            'result_count': len(results),
            'results': results,
        })
    except ValueError:
        return jsonify({'error': 'Invalid PDF search request.', 'results': [], 'result_count': 0}), 400
    except RuntimeError:
        return jsonify({'error': 'This PDF could not be text searched.', 'results': [], 'result_count': 0}), 200
    except Exception:
        return jsonify({'error': 'This PDF could not be text searched.', 'results': [], 'result_count': 0}), 200


# Legacy single-level route kept for compatibility
@app.route('/extracts/<category>')
def extracts_category_legacy(category):
    return extracts_category(category)


# Preferred nested route
@app.route('/extracts/<path:subpath>')
def extracts_category(subpath):
    try:
        normalised = normalise_category_path(subpath)
    except ValueError:
        return _unavailable('Extract category unavailable', 'This extract category is not available in the published EQRF set.', 'Back to Extracts', 'extracts_index')
    if normalised == 'MISC' and _misc_is_general_reference_only(get_extracts().get('MISC')):
        return _unavailable('Extract category unavailable', 'This extract category has no valid local EQRF documents published.', 'Back to Extracts', 'extracts_index')

    extracts = filtered_extract_tree(get_extracts()) or {}
    node = _get_node_for_path(extracts, safe_path_parts(normalised))
    if not extract_group_has_content(node):
        return _unavailable('Extract category unavailable', 'This extract category has no valid local EQRF documents published.', 'Back to Extracts', 'extracts_index')

    subcategories = {
        k: v for k, v in node.items()
        if k != '__files__' and extract_group_has_content(v)
    } if isinstance(node, dict) else {}

    items = public_extract_items_for_node(node, normalised)
    if len(items) == 1 and not subcategories:
        # Auto-open leaf categories that contain exactly one file.
        return redirect(url_for('extracts_viewer', category=normalised, filename=items[0]['filename']))

    return render_template(
        'extracts_category.html',
        category=normalised,
        items=items,
        node=node,
        subcategories=subcategories,
        breadcrumbs=_breadcrumb_items('Extracts', 'extracts_index', normalised, 'extracts_category', 'subpath'),
        sibling_items=_extract_sibling_items(extracts, normalised),
    )


# Viewer by explicit filename (canonical)
@app.route('/viewer/<path:category>/<path:filename>')
def extracts_viewer(category, filename):
    extracts = get_extracts()
    if '/' in filename:
        extra_parts = [part for part in filename.split('/') if part]
        if len(extra_parts) > 1:
            category = f"{category}/{'/'.join(extra_parts[:-1])}"
            filename = extra_parts[-1]
    try:
        normalised_category = normalise_category_path(category)
    except ValueError:
        return _unavailable('Extract unavailable', 'This extract path is not available in the published EQRF set.', 'Back to Extracts', 'extracts_index')

    node = _get_node_for_path(extracts, safe_path_parts(normalised_category))
    if normalised_category == '--' and _find_file_entry(node, filename) is None:
        root_entry = _find_file_entry(extracts, filename)
        if root_entry is not None:
            node = extracts
    if node is None:
        return _unavailable('Extract unavailable', 'This extract category is not registered in local EQRF data.', 'Back to Extracts', 'extracts_index')

    entry = _find_file_entry(node, filename)
    if entry is None:
        return _unavailable('Extract unavailable', 'This PDF is not registered in the selected EQRF category.', 'Back to Extracts', 'extracts_index')

    item = normalise_file_entry(entry)
    if not metadata_is_public(item):
        return _unavailable('Extract unavailable', 'This PDF is not currently published in the local EQRF content set.', 'Back to Extracts', 'extracts_index')

    pdf_path = PDF_DIR / filename
    if not pdf_path.exists():
        return _unavailable('Extract unavailable', 'The local source PDF is missing and must be repaired in Admin.', 'Back to Extracts', 'extracts_index')

    page_count = int(item.get('page_count') or 0) or get_pdf_page_count(filename) or 1
    orientation = normalise_orientation(item.get('orientation'))

    item = {
        'pdf': filename,
        'title': get_display_title_for_pdf(entry),
        'jpgs': file_entry_jpgs(entry),
        'orientation': orientation,
        'page_count': page_count,
        'category': normalised_category,
        'pdf_url': url_for('send_pdf', filename=filename),
        'metadata': item,
        'status_label': metadata_status_label(item),
        'status_state': metadata_status_state(item),
    }
    parent_path = normalised_category
    if normalised_category == '--':
        breadcrumbs = [
            {'label': 'Extracts', 'url': url_for('extracts_index')},
            {'label': item['title'], 'url': None},
        ]
    else:
        breadcrumbs = _breadcrumb_items('Extracts', 'extracts_index', normalised_category, 'extracts_category', 'subpath')
        if breadcrumbs:
            breadcrumbs[-1]['url'] = url_for('extracts_category', subpath=normalised_category)
        breadcrumbs = _breadcrumb_with_leaf(breadcrumbs, item['title'])
    return render_template(
        'extracts_viewer.html',
        item=item,
        breadcrumbs=breadcrumbs,
        parent_path=parent_path,
        can_search_pdf=is_general_reference_category(normalised_category),
        sibling_items=_extract_sibling_items(filtered_extract_tree(extracts) or {}, normalised_category),
    )


# Legacy viewer by index, e.g. /viewer/AIR/SID/2  (1-based index)
@app.route('/viewer/<path:category>/<int:index>')
def extracts_viewer_by_index(category, index):
    try:
        normalised = normalise_category_path(category)
    except ValueError:
        return _unavailable('Extract unavailable', 'This extract path is not available in the published EQRF set.', 'Back to Extracts', 'extracts_index')
    extracts = filtered_extract_tree(get_extracts()) or {}
    node = _get_node_for_path(extracts, safe_path_parts(normalised))
    items = public_extract_items_for_node(node, normalised)
    if index < 1 or index > len(items):
        return _unavailable('Extract unavailable', 'This document index is not available in the published EQRF set.', 'Back to Extracts', 'extracts_index')
    return redirect(url_for('extracts_viewer', category=normalised, filename=items[index - 1]['filename']))

@app.route('/pdfs/<path:filename>')
def send_pdf(filename):
    try:
        filename = _safe_pdf_filename(filename)
    except ValueError:
        return _unavailable('PDF unavailable', 'This PDF path is not valid.', 'Back to Extracts', 'extracts_index')

    if not pdf_is_registered(filename) or not local_pdf_exists(filename):
        return _unavailable('PDF unavailable', 'This PDF is not registered in local EQRF data.', 'Back to Extracts', 'extracts_index')
    return send_from_directory(PDF_DIR, filename, conditional=True, mimetype='application/pdf')

# ---------------------- Admin: register & manage extracts ---------------------- #

@app.route('/admin', methods=['GET'])
@login_required
def admin_panel():
    return render_template('admin_dashboard.html', **_admin_context())


@app.route('/admin/audit')
@login_required
def admin_audit():
    limit_arg = request.args.get('limit', '50')
    if limit_arg == 'all':
        limit = None
    else:
        try:
            limit = int(limit_arg)
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 500))

    action_filter = (request.args.get('action') or '').strip().lower()
    target_filter = (request.args.get('target_type') or '').strip().lower()
    search = (request.args.get('q') or '').strip().lower()
    entries = latest_audit_entries(limit)
    if action_filter:
        entries = [entry for entry in entries if str(entry.get('action', '')).lower() == action_filter]
    if target_filter:
        entries = [entry for entry in entries if str(entry.get('target_type', '')).lower() == target_filter]
    if search:
        entries = [
            entry for entry in entries
            if search in json.dumps(entry, ensure_ascii=False).lower()
        ]

    all_entries = get_audit_log()
    return render_template(
        'admin_audit.html',
        entries=entries,
        actions=sorted({str(entry.get('action', '')) for entry in all_entries if entry.get('action')}),
        target_types=sorted({str(entry.get('target_type', '')) for entry in all_entries if entry.get('target_type')}),
        selected_action=action_filter,
        selected_target_type=target_filter,
        query=search,
        limit_arg=limit_arg,
    )


@app.route('/admin/upload_pdf', methods=['POST'])
@login_required
def upload_pdf():
    """Register a PDF only after the source file has been validated."""

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
            category = '--'
        governance_metadata = metadata_from_form(_pdf_base(safe_name))
        if is_na(request.form.get('version')):
            governance_metadata['version'] = '1.0'
        if is_na(request.form.get('effective_date')):
            governance_metadata['effective_date'] = date.today().isoformat()

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

            orientation = detect_pdf_orientation_from_path(tmp_pdf) if orientation_mode == 'auto' else normalise_orientation(orientation_mode)
            existing_entry = _find_file_entry(node, safe_name) if node is not None else None
            existing_metadata = normalise_file_entry(existing_entry) if existing_entry else {}
            page_count = 0
            try:
                from pypdf import PdfReader
                page_count = len(PdfReader(str(tmp_pdf)).pages)
            except Exception:
                page_count = int(existing_metadata.get('page_count') or 0)
            metadata = {
                'pdf': safe_name,
                'jpgs': file_entry_jpgs(existing_entry) if existing_entry else [],
                'orientation': orientation,
                'page_count': page_count,
                'uploaded_at': _utc_timestamp(),
                'source': 'admin',
                **governance_metadata,
            }

            tmp_pdf.replace(PDF_DIR / safe_name)

        upsert_pdf_entry(extracts, category, metadata, replace=replace)
        save_extracts(extracts)
        trigger_client_refresh()
        append_audit_log(
            'upload_extract',
            'extract',
            f'{category}/{safe_name}',
            f'Uploaded extract {safe_name} to {category}',
            {
                'version': metadata.get('version'),
                'effective_date': metadata.get('effective_date'),
                'expiry_date': metadata.get('expiry_date'),
                'review_date': metadata.get('review_date'),
                'status': metadata.get('status'),
            },
        )
        flash(f'Uploaded {safe_name} to {category}.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin_panel'))


@app.route('/admin/extracts/edit', methods=['GET', 'POST'])
@login_required
def admin_extract_edit():
    category = request.values.get('category') or ''
    filename = request.values.get('filename') or ''
    try:
        category = normalise_category_path(category)
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_panel'))

    extracts = _json_clone(get_extracts())
    node = _get_node_for_path(extracts, safe_path_parts(category))
    entry = _find_file_entry(node, filename) if node is not None else None
    if entry is None:
        flash('File not found in category.', 'error')
        return redirect(url_for('admin_panel'))

    item = normalise_file_entry(entry)
    if request.method == 'POST':
        try:
            metadata = metadata_from_form(item.get('title') or _pdf_base(filename))
            orientation = normalise_orientation(request.form.get('orientation') or item.get('orientation'))
            if orientation not in {'portrait', 'landscape'}:
                raise ValueError('Invalid orientation selection.')
            item.update(metadata)
            item['orientation'] = orientation
            upsert_pdf_entry(extracts, category, item, replace=True)
            save_extracts(extracts)
            trigger_client_refresh()
            append_audit_log(
                'update_extract_metadata',
                'extract',
                f'{category}/{filename}',
                f'Updated extract metadata for {filename}',
                {
                    'version': item.get('version'),
                    'effective_date': item.get('effective_date'),
                    'expiry_date': item.get('expiry_date'),
                    'review_date': item.get('review_date'),
                    'status': item.get('status'),
                },
            )
            flash(f'Updated metadata for {filename}.', 'success')
            return redirect(url_for('admin_panel'))
        except ValueError as exc:
            flash(str(exc), 'error')
            item.update(normalise_content_metadata({
                'title': request.form.get('title'),
                'version': request.form.get('version'),
                'effective_date': request.form.get('effective_date'),
                'expiry_date': request.form.get('expiry_date'),
                'review_date': request.form.get('review_date'),
                'owner': request.form.get('owner'),
                'status': request.form.get('status'),
                'last_updated': item.get('last_updated'),
            }, item.get('title')))

    return render_template(
        'admin_extract_edit.html',
        category=category,
        filename=filename,
        item=item,
        statuses=sorted(CONTENT_STATUSES),
    )


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
        if remove_pdf_entry(extracts, category, filename):
            save_extracts(extracts)
            trigger_client_refresh()
            append_audit_log('delete_extract', 'extract', f'{category}/{filename}', f'Deleted extract {filename} from {category}')
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

        item = normalise_file_entry(entry)
        page_count = get_pdf_page_count(filename) or int(item.get('page_count') or 0)

        item.update({
            'pdf': filename,
            'title': item.get('title') or _pdf_base(filename),
            'page_count': page_count,
            'refreshed_at': _utc_timestamp(),
            'last_updated': _utc_timestamp(),
            'source': item.get('source') or 'admin',
        })
        upsert_pdf_entry(extracts, category, item, replace=True)
        save_extracts(extracts)
        trigger_client_refresh()
        append_audit_log('refresh_extract_metadata', 'extract', f'{category}/{filename}', f'Refreshed PDF metadata for {filename}')
        flash(f'Refreshed metadata for {filename}.', 'success')
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

    if _delete_category_path(extracts, parts):
        save_extracts(extracts)
        trigger_client_refresh()
        append_audit_log('delete_extract_category', 'extract_category', category, f'Deleted extract category {category}')
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
        checklist_paths=_flatten_all_checklist_paths(checklists),
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
            normalised_path = normalise_category_path(path)
            metadata = metadata_from_form(Path(normalised_path).name if normalised_path else None)
            upsert_checklist_with_metadata(checklists, normalised_path, lines, metadata, overwrite_folder=overwrite_folder)
            save_checklists(checklists)
            trigger_client_refresh()
            append_audit_log('create_checklist', 'checklist', normalised_path, f'Created checklist {normalised_path}', metadata)
            flash(f'Checklist saved: {normalised_path}', 'success')
            return redirect(url_for('admin_checklists'))
        except ValueError as exc:
            flash(str(exc), 'error')
            return render_template(
                'admin_checklist_edit.html',
                mode='new',
                path=path,
                original_path='',
                text=text,
                metadata=normalise_content_metadata({
                    'title': request.form.get('title'),
                    'version': request.form.get('version'),
                    'effective_date': request.form.get('effective_date'),
                    'expiry_date': request.form.get('expiry_date'),
                    'review_date': request.form.get('review_date'),
                    'owner': request.form.get('owner'),
                    'status': request.form.get('status'),
                }, None),
                statuses=sorted(CONTENT_STATUSES),
            )
    return render_template(
        'admin_checklist_edit.html',
        mode='new',
        path='',
        original_path='',
        text='',
        metadata={**default_content_metadata(''), 'version': '1.0', 'effective_date': date.today().isoformat()},
        statuses=sorted(CONTENT_STATUSES),
    )


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
            metadata = metadata_from_form(Path(normalised_path).name if normalised_path else None)
            upsert_checklist_with_metadata(checklists, normalised_path, text.splitlines(), metadata, overwrite_folder=overwrite_folder)
            if normalised_original != normalised_path:
                delete_checklist(checklists, normalised_original)
            save_checklists(checklists)
            trigger_client_refresh()
            append_audit_log('edit_checklist', 'checklist', normalised_path, f'Updated checklist {normalised_path}', metadata)
            append_audit_log('update_checklist_metadata', 'checklist', normalised_path, f'Updated checklist metadata for {normalised_path}', metadata)
            flash(f'Checklist saved: {normalised_path}', 'success')
            return redirect(url_for('admin_checklists'))
        except ValueError as exc:
            flash(str(exc), 'error')
            return render_template(
                'admin_checklist_edit.html',
                mode='edit',
                path=path,
                original_path=original_path,
                text=text,
                metadata=normalise_content_metadata({
                    'title': request.form.get('title'),
                    'version': request.form.get('version'),
                    'effective_date': request.form.get('effective_date'),
                    'expiry_date': request.form.get('expiry_date'),
                    'review_date': request.form.get('review_date'),
                    'owner': request.form.get('owner'),
                    'status': request.form.get('status'),
                }, None),
                statuses=sorted(CONTENT_STATUSES),
            )

    path = request.args.get('path') or ''
    try:
        normalised = normalise_category_path(path)
        current = _get_node_for_path(get_checklists(), safe_path_parts(normalised))
        if not is_checklist_leaf(current):
            flash('Checklist not found.', 'error')
            return redirect(url_for('admin_checklists'))
        return render_template(
            'admin_checklist_edit.html',
            mode='edit',
            path=normalised,
            original_path=normalised,
            text='\n'.join(checklist_items(current)),
            metadata=checklist_metadata(current, Path(normalised).name),
            statuses=sorted(CONTENT_STATUSES),
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
            append_audit_log('delete_checklist', 'checklist', normalise_category_path(path), f'Deleted checklist {normalise_category_path(path)}')
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
        if not is_valid_checklist_node(current):
            flash('Checklist is not published because it is empty or invalid.', 'error')
            return redirect(url_for('admin_checklists'))
        parts = safe_path_parts(normalised)
        metadata = checklist_metadata(current, parts[-1] if parts else None)
        return render_template(
            'checklist_view.html',
            category=normalised,
            items=checklist_items(current),
            metadata=metadata,
            status_label=metadata_status_label(metadata),
            status_state=metadata_status_state(metadata),
            breadcrumbs=_breadcrumb_items('Checklists', 'checklist_index', normalised, 'checklist_category', 'category'),
            parent_path='/'.join(parts[:-1]) if len(parts) > 1 else '',
            sibling_items=_checklist_sibling_items(filtered_checklist_tree(get_checklists()) or {}, '/'.join(parts[:-1]) if len(parts) > 1 else ''),
        )
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_checklists'))

# ---------------------- Main ---------------------- #

if __name__ == '__main__':
    for warning in production_safety_warnings():
        print(f'WARNING: {warning}')
    print('WARNING: python app.py starts the Flask development server. Use Gunicorn with wsgi:application for operational local-network use.')
    app.run(debug=SETTINGS.debug, host=SETTINGS.host, port=SETTINGS.port)
