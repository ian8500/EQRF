import re
from types import SimpleNamespace

import pytest

import app as app_module
from app import (
    JPG_DIR,
    AUDIT_LOG_JSON,
    append_audit_log,
    app,
    checklist_items,
    checklist_metadata,
    content_is_expired,
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
    get_audit_log,
    metadata_status_label,
    normalise_file_entry,
    normalise_category_path,
    safe_path_parts,
    upsert_checklist,
    upsert_checklist_with_metadata,
    upsert_pdf_entry,
    validate_content_metadata,
)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'AUDIT_LOG_JSON', tmp_path / 'audit_log.json')
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


def test_audit_log_file_created_and_append_records_entry(tmp_path, monkeypatch):
    audit_path = tmp_path / 'audit_log.json'
    monkeypatch.setattr(app_module, 'AUDIT_LOG_JSON', audit_path)

    assert get_audit_log() == []
    assert audit_path.exists()

    append_audit_log('unit_test', 'test', 'target', 'Unit test audit entry', {'version': '1.0'})
    entries = get_audit_log()

    assert len(entries) == 1
    assert entries[0]['action'] == 'unit_test'
    assert entries[0]['details']['version'] == '1.0'


def test_admin_audit_requires_login_and_displays_entries(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')

    logged_out = client.get('/admin/audit')
    assert logged_out.status_code == 302
    assert '/login' in logged_out.headers['Location']

    client.post('/login', data={'password': 'test-pass', 'next': '/admin/audit'})
    append_audit_log('unit_test', 'test', 'target', 'Visible audit entry')
    response = client.get('/admin/audit')

    assert response.status_code == 200
    assert b'Audit Log' in response.data
    assert b'Visible audit entry' in response.data


def test_admin_dashboard_shows_audit_log_link_and_governance_summary(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    response = client.get('/admin')

    assert response.status_code == 200
    assert b'Audit Log' in response.data
    assert b'Extracts published' in response.data
    assert b'Checklists published' in response.data


def test_admin_edit_pages_require_login(client):
    extract_response = client.get('/admin/extracts/edit?category=AIR&filename=MATS1.pdf')
    checklist_response = client.get('/admin/checklists/edit?path=AIR/Test')

    assert extract_response.status_code == 302
    assert checklist_response.status_code == 302


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
    assert normalise_file_entry('MATS1.pdf')['version'] == 'N/A'
    assert normalise_file_entry('MATS1.pdf')['status'] == 'published'

    entry = {
        'pdf': 'MATS2.pdf',
        'jpgs': ['MATS2_page1.jpg'],
        'orientation': 'landscape',
        'version': '2.1',
        'effective_date': '2026-05-27',
        'expiry_date': 'N/A',
        'review_date': '2026-06-27',
    }
    assert file_entry_name(entry) == 'MATS2.pdf'
    assert file_entry_jpgs(entry) == ['MATS2_page1.jpg']
    assert normalise_file_entry(entry)['version'] == '2.1'
    assert normalise_file_entry(entry)['review_date'] == '2026-06-27'


def test_blank_metadata_fields_normalise_to_na_and_invalid_dates_reject():
    metadata = validate_content_metadata({
        'title': '',
        'version': '',
        'effective_date': '',
        'expiry_date': '',
        'review_date': 'N/A',
        'owner': '',
        'status': 'published',
    })

    assert metadata['version'] == 'N/A'
    assert metadata['effective_date'] == 'N/A'
    assert metadata['owner'] == 'N/A'

    with pytest.raises(ValueError):
        validate_content_metadata({'effective_date': '27/05/2026'})


def test_expired_extract_gets_status_label():
    entry = normalise_file_entry({
        'pdf': 'Expired.pdf',
        'expiry_date': '2020-01-01',
        'status': 'published',
    })

    assert content_is_expired(entry)
    assert metadata_status_label(entry) == 'Expired'


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


def test_checklist_metadata_helpers_convert_and_preserve_lines():
    checklists = {}

    upsert_checklist_with_metadata(
        checklists,
        'A380/Runway 05',
        ['--- Arrival ---', 'Check item'],
        {
            'title': 'Runway 05',
            'version': '1.2',
            'effective_date': '2026-05-27',
            'expiry_date': 'N/A',
            'review_date': '2026-08-01',
            'owner': 'Ops',
            'status': 'published',
            'last_updated': '2026-05-27T12:00:00Z',
        },
    )

    node = checklists['A380']['Runway 05']
    assert node['__type__'] == 'checklist'
    assert checklist_items(node) == ['--- Arrival ---', 'Check item']
    assert checklist_metadata(node)['version'] == '1.2'
    assert count_checklist_items(checklists) == 2


def test_draft_checklist_hidden_from_public_pages(client, isolated_content):
    upsert_checklist_with_metadata(
        isolated_content.checklists,
        'Tower/Draft',
        ['Draft item'],
        {'title': 'Draft', 'status': 'draft'},
    )

    response = client.get('/checklists')

    assert response.status_code == 200
    assert b'Draft' not in response.data


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
    filtered = filtered_checklist_tree(isolated_content.checklists)
    assert checklist_items(filtered['Tower']['Runway 05']) == ['Line up']


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


def test_home_page_does_not_show_public_summary_or_status_panels(client):
    response = client.get('/')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    forbidden = [
        'Checklist count',
        'Extract count',
        'Quick Reference count',
        'Published Content',
        'System posture',
        'Content source',
        'External links',
        'Mode: Operational',
        'STATUS: AVAILABLE',
        'CONTENT: LOCAL EQRF',
        'LINKS: INTERNAL ONLY',
        'LOCAL CONTENT ONLY',
    ]
    for text in forbidden:
        assert text not in html


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

    response = client.get('/extracts/AIR', follow_redirects=True)

    assert response.status_code == 200
    assert b'ValidString' in response.data
    assert b'Valid Dict' in response.data
    assert len(flatten_valid_extract_files(isolated_content.extracts)) == 2


def test_draft_extract_hidden_and_published_extract_visible(client, isolated_content):
    jpgs = isolated_content.publish_pdf('Governed.pdf')
    isolated_content.publish_pdf('Draft.pdf')
    isolated_content.extracts.update({
        'AIR': {
            '__files__': [
                {'pdf': 'Governed.pdf', 'jpgs': jpgs, 'title': 'Governed', 'status': 'published'},
                {'pdf': 'Draft.pdf', 'jpgs': ['Draft_page1.jpg'], 'title': 'Draft Extract', 'status': 'draft'},
            ],
        },
    })

    response = client.get('/extracts/AIR', follow_redirects=True)

    assert response.status_code == 200
    assert b'Governed' in response.data
    assert b'Draft Extract' not in response.data


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


def test_extract_pages_do_not_show_explanatory_tile_text(client, isolated_content):
    isolated_content.publish_pdf('Valid.pdf')
    isolated_content.publish_pdf('Other.pdf')
    isolated_content.extracts.update({'AIR': {'__files__': ['Valid.pdf', 'Other.pdf']}})

    for path in ['/extracts', '/extracts/AIR']:
        response = client.get(path)
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'Open document group' not in html
        assert 'Open rendered document' not in html
        assert 'Open verified local document group' not in html
        assert 'Category' not in html
        assert 'PDF Extract' not in html
        assert 'PDF extract' not in html


def test_checklist_pages_do_not_show_explanatory_tile_text(client, isolated_content):
    isolated_content.checklists.update({'Tower': {'GMC': ['Line up']}})

    for path in ['/checklists', '/checklists/Tower']:
        response = client.get(path)
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'Open operational checklist' not in html
        assert 'Open nested checklist group' not in html
        assert 'Folder' not in html
        assert 'Checklist</span>' not in html


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
