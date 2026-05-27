import re
from types import SimpleNamespace

import pytest

import app as app_module
from app import (
    JPG_DIR,
    app,
    count_checklist_items,
    delete_checklist,
    file_entry_jpgs,
    file_entry_name,
    find_duplicate_pdf_entries,
    find_missing_jpgs,
    find_missing_pdfs,
    find_orphan_jpgs,
    filtered_checklist_tree,
    filtered_extract_tree,
    flatten_valid_extract_files,
    normalise_category_path,
    safe_path_parts,
    upsert_checklist,
    upsert_pdf_entry,
)


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture()
def isolated_content(monkeypatch, tmp_path):
    pdf_dir = tmp_path / 'pdfs'
    jpg_dir = tmp_path / 'jpgs'
    pdf_dir.mkdir()
    jpg_dir.mkdir()
    checklists = {}
    extracts = {}

    monkeypatch.setattr(app_module, 'PDF_DIR', pdf_dir)
    monkeypatch.setattr(app_module, 'JPG_DIR', jpg_dir)
    monkeypatch.setattr(app_module, 'get_checklists', lambda: checklists)
    monkeypatch.setattr(app_module, 'get_extracts', lambda: extracts)

    def publish_pdf(filename, jpgs=None):
        (pdf_dir / filename).write_bytes(b'%PDF-1.4\n% test\n')
        names = jpgs or [filename.replace('.pdf', '_page1.jpg')]
        for jpg in names:
            (jpg_dir / jpg).write_bytes(b'jpg')
        return names

    return SimpleNamespace(
        pdf_dir=pdf_dir,
        jpg_dir=jpg_dir,
        checklists=checklists,
        extracts=extracts,
        publish_pdf=publish_pdf,
    )


def test_home_page_loads(client):
    response = client.get('/')
    assert response.status_code == 200


def test_checklists_page_loads(client):
    response = client.get('/checklists')
    assert response.status_code == 200


def test_extracts_page_loads(client):
    response = client.get('/extracts')
    assert response.status_code == 200


def test_login_page_loads(client):
    response = client.get('/login')
    assert response.status_code == 200


def test_admin_redirects_to_login_when_logged_out(client):
    response = client.get('/admin')
    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_login_works_with_eqrf_password(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')

    response = client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin')
    with client.session_transaction() as session:
        assert session['logged_in'] is True
        assert session['is_admin'] is True
    admin_response = client.get('/admin')
    assert admin_response.status_code == 200
    assert b'Content management console' in admin_response.data


def test_admin_link_hidden_when_logged_out(client):
    response = client.get('/')

    assert response.status_code == 200
    assert b'>Admin<' not in response.data


def test_admin_link_appears_after_admin_login(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')

    client.post('/login', data={'password': 'test-pass', 'next': '/'})
    response = client.get('/')

    assert response.status_code == 200
    assert b'>Admin<' in response.data


def test_known_static_jpg_route_works_if_available(client):
    jpgs = sorted(JPG_DIR.glob('*.jpg'))
    if not jpgs:
        pytest.skip('No JPG assets available.')

    response = client.get(f'/jpgs/{jpgs[0].name}')
    assert response.status_code == 200
    assert response.content_type.startswith('image/jpeg')


def test_normalise_category_path_collapses_and_strips():
    assert normalise_category_path(' /AIR//SID/ ') == 'AIR/SID'
    assert safe_path_parts('A380/Runway 05') == ['A380', 'Runway 05']
    assert normalise_category_path('') == ''


def test_normalise_category_path_rejects_dangerous_paths():
    with pytest.raises(ValueError):
        normalise_category_path('../AIR')
    with pytest.raises(ValueError):
        normalise_category_path('AIR/./SID')


def test_nested_category_creation():
    extracts = {}

    upsert_pdf_entry(extracts, 'AIR/SID', {'pdf': 'MATS1.pdf', 'jpgs': ['MATS1_page1.jpg']})

    assert extracts['AIR']['SID']['__files__'][0]['pdf'] == 'MATS1.pdf'


def test_file_entry_helpers_support_string_and_dict_entries():
    assert file_entry_name('MATS1.pdf') == 'MATS1.pdf'
    assert file_entry_jpgs('MATS1.pdf') == []

    entry = {'pdf': 'MATS2.pdf', 'jpgs': ['MATS2_page1.jpg'], 'orientation': 'landscape'}
    assert file_entry_name(entry) == 'MATS2.pdf'
    assert file_entry_jpgs(entry) == ['MATS2_page1.jpg']


def test_duplicate_pdf_detection_supports_mixed_entries():
    extracts = {
        'AIR': {'__files__': ['MATS1.pdf']},
        'GROUND': {'__files__': [{'pdf': 'MATS1.pdf', 'jpgs': []}]},
    }

    duplicates = find_duplicate_pdf_entries(extracts)

    assert duplicates == [{'filename': 'MATS1.pdf', 'count': 2, 'categories': ['AIR', 'GROUND']}]


def test_checklist_helpers_create_edit_and_delete_nested_checklist():
    checklists = {}

    upsert_checklist(checklists, 'A380/Runway 05', ['--- Arrival ---', 'CAT A ONLY'])
    assert checklists['A380']['Runway 05'] == ['--- Arrival ---', 'CAT A ONLY']
    assert count_checklist_items(checklists) == 2

    upsert_checklist(checklists, 'A380/Runway 05', ['Landing clearance'])
    assert checklists['A380']['Runway 05'] == ['Landing clearance']

    assert delete_checklist(checklists, 'A380/Runway 05') is True
    assert 'Runway 05' not in checklists['A380']


def test_checklist_helper_rejects_empty_checklist():
    with pytest.raises(ValueError):
        upsert_checklist({}, 'A380/Runway 05', ['', '   '])


def test_extract_health_helpers_detect_missing_assets_and_orphans(tmp_path):
    extracts = {
        'AIR': {
            '__files__': [
                {'pdf': 'Missing.pdf', 'jpgs': ['Missing_page1.jpg']},
            ],
        },
    }

    assert find_missing_pdfs(extracts, pdf_dir=tmp_path)[0]['filename'] == 'Missing.pdf'
    assert find_missing_jpgs(extracts, jpg_dir=tmp_path)[0]['filename'] == 'Missing.pdf'

    (tmp_path / 'Missing_page1.jpg').write_bytes(b'referenced')
    (tmp_path / 'orphan.jpg').write_bytes(b'orphan')
    assert find_orphan_jpgs(extracts, jpg_dir=tmp_path) == ['orphan.jpg']


def test_empty_checklist_folders_are_not_shown(client, isolated_content):
    isolated_content.checklists.update({
        'Empty': {'Nothing': []},
        'Tower': {'Runway 05': ['Line up']},
    })

    response = client.get('/checklists')

    assert response.status_code == 200
    assert b'Tower' in response.data
    assert b'Empty' not in response.data
    assert filtered_checklist_tree(isolated_content.checklists) == {'Tower': {'Runway 05': ['Line up']}}


def test_home_hides_checklists_card_when_no_valid_checklists(client, isolated_content):
    isolated_content.checklists.update({'Empty': {'Nothing': []}})

    response = client.get('/')

    assert response.status_code == 200
    assert b'<strong>Checklists</strong>' not in response.data
    assert b'No active EQRF content is currently published.' in response.data


def test_home_page_does_not_show_command_labels(client):
    response = client.get('/')

    assert response.status_code == 200
    assert b'COMMAND 01' not in response.data
    assert b'COMMAND 02' not in response.data
    assert b'COMMAND 03' not in response.data


def test_empty_extract_folders_and_missing_assets_are_hidden(client, isolated_content):
    isolated_content.extracts.update({
        'Empty': {},
        'AIR': {'__files__': ['Missing.pdf']},
        'GROUND': {'__files__': [{'pdf': 'NoJpg.pdf', 'jpgs': ['NoJpg_page1.jpg']}]},
    })
    (isolated_content.pdf_dir / 'NoJpg.pdf').write_bytes(b'%PDF-1.4\n')

    response = client.get('/extracts')

    assert response.status_code == 200
    assert b'No published EQRF extracts available.' in response.data
    assert b'Empty' not in response.data
    assert b'Missing.pdf' not in response.data
    assert b'NoJpg.pdf' not in response.data
    assert filtered_extract_tree(isolated_content.extracts) == {}


def test_valid_string_and_dict_extract_entries_show(client, isolated_content):
    isolated_content.publish_pdf('ValidString.pdf')
    isolated_content.publish_pdf('ValidDict.pdf', ['ValidDict_page1.jpg'])
    isolated_content.extracts.update({
        'AIR': {
            '__files__': [
                'ValidString.pdf',
                {
                    'pdf': 'ValidDict.pdf',
                    'title': 'Valid Dict',
                    'jpgs': ['ValidDict_page1.jpg'],
                    'orientation': 'landscape',
                },
            ],
        },
    })

    response = client.get('/extracts/AIR')

    assert response.status_code == 200
    assert b'ValidString' in response.data
    assert b'Valid Dict' in response.data
    assert len(flatten_valid_extract_files(isolated_content.extracts)) == 2


def test_home_hides_extracts_card_when_no_valid_extracts(client, isolated_content):
    isolated_content.extracts.update({'AIR': {'__files__': ['Missing.pdf']}})

    response = client.get('/')

    assert response.status_code == 200
    assert b'<strong>Extracts</strong>' not in response.data


def test_invalid_checklist_path_returns_friendly_unavailable_page(client, isolated_content):
    response = client.get('/checklists/Nope')

    assert response.status_code == 404
    assert b'Checklist unavailable' in response.data
    assert b'Back to Checklists' in response.data


def test_invalid_extract_path_returns_friendly_unavailable_page(client, isolated_content):
    response = client.get('/extracts/Nope')

    assert response.status_code == 404
    assert b'Extract category unavailable' in response.data
    assert b'Back to Extracts' in response.data


def test_viewer_rejects_missing_local_assets_gracefully(client, isolated_content):
    isolated_content.extracts.update({'AIR': {'__files__': ['Missing.pdf']}})

    response = client.get('/viewer/AIR/Missing.pdf')

    assert response.status_code == 404
    assert b'Extract unavailable' in response.data
    assert b'local source PDF is missing' in response.data


def test_checklist_cat_a_min_line_renders_with_critical_class(client, isolated_content):
    isolated_content.checklists.update({
        'Tower': {
            'GMC': {
                'Runway Change': ['[CAT A MIN] Runway occupancy confirmed'],
            },
        },
    })

    response = client.get('/checklists/Tower/GMC/Runway%20Change')

    assert response.status_code == 200
    assert b'cat-a-critical' in response.data
    assert b'[CAT A MIN] Runway occupancy confirmed' in response.data


def test_extract_viewer_renders_breadcrumb_for_nested_category(client, isolated_content):
    jpgs = isolated_content.publish_pdf('Valid.pdf')
    isolated_content.extracts.update({
        'Tower': {
            'GMC': {
                '__files__': [{'pdf': 'Valid.pdf', 'jpgs': jpgs, 'title': 'Valid Extract'}],
            },
        },
    })

    response = client.get('/viewer/Tower/GMC/Valid.pdf')

    assert response.status_code == 200
    assert b'Extracts' in response.data
    assert b'Tower' in response.data
    assert b'GMC' in response.data
    assert b'Valid Extract' in response.data


def test_checklist_viewer_renders_breadcrumb_for_nested_category(client, isolated_content):
    isolated_content.checklists.update({
        'Tower': {
            'GMC': {
                'Runway Change': ['Runway selected'],
            },
        },
    })

    response = client.get('/checklists/Tower/GMC/Runway%20Change')

    assert response.status_code == 200
    assert b'Checklists' in response.data
    assert b'Tower' in response.data
    assert b'GMC' in response.data
    assert b'Runway Change' in response.data


def test_day_night_toggle_js_uses_localstorage():
    script = (app_module.BASE_DIR / 'static' / 'script.js').read_text(encoding='utf-8')

    assert 'localStorage' in script
    assert 'eqrf-theme' in script
    assert 'theme-day' in script
    assert 'theme-night' in script
    assert 'Day Mode' in script
    assert 'Night Mode' in script


def test_public_pages_contain_no_emoji_icons(client):
    emoji_pattern = re.compile('[\U0001f300-\U0001faff\u2600-\u27bf]')

    for path in ['/', '/checklists', '/extracts']:
        response = client.get(path)
        assert response.status_code == 200
        assert emoji_pattern.search(response.get_data(as_text=True)) is None


def test_public_pages_do_not_emit_external_or_invalid_content_links(client, isolated_content):
    isolated_content.checklists.update({
        'Tower': {'Runway 05': ['Line up']},
        'Empty': {'Nothing': []},
    })
    isolated_content.publish_pdf('Valid.pdf')
    isolated_content.extracts.update({
        'AIR': {'__files__': ['Valid.pdf', 'Missing.pdf']},
        'EmptyExtract': {},
    })

    pages = ['/', '/checklists', '/checklists/Tower', '/extracts', '/extracts/AIR']
    for path in pages:
        response = client.get(path)
        assert response.status_code in {200, 302}
        html = response.get_data(as_text=True)
        assert 'http://' not in html
        assert 'https://' not in html
        hrefs = re.findall(r'href="([^"]+)"', html)
        assert all('Missing.pdf' not in href for href in hrefs)
        assert all('/checklists/Empty' not in href for href in hrefs)
        assert all('/extracts/EmptyExtract' not in href for href in hrefs)
