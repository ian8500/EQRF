import io
import importlib
import re
from types import SimpleNamespace

import pytest

import app as app_module
from app import (
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
    is_critical_checklist_line,
    metadata_status_label,
    normalise_file_entry,
    normalise_category_path,
    production_safety_warnings,
    get_general_reference_entries,
    get_cached_pdf_text_pages,
    search_pdf_text,
    safe_path_parts,
    Settings,
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


def test_health_page_returns_ok_json(client):
    response = client.get('/health')

    assert response.status_code == 200
    assert response.get_json() == {'status': 'ok', 'app': 'EQRF'}


def test_admin_redirects_to_login_when_logged_out(client):
    response = client.get('/admin')
    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_wsgi_exports_application_and_app():
    module = importlib.import_module('wsgi')

    assert module.application is app_module.app
    assert module.app is app_module.app


def test_app_import_does_not_start_dev_server():
    assert app_module.app.name == 'app'
    assert app_module.app.debug is False


def test_production_config_defaults_debug_off_and_respects_env(monkeypatch):
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    assert Settings().debug is False

    monkeypatch.setenv('FLASK_DEBUG', '1')
    assert Settings().debug is True


def test_production_safety_warnings_for_default_secrets():
    warnings = production_safety_warnings(Settings(secret_key='change-me', admin_password='admin'))

    assert any('EQRF_SECRET_KEY' in warning for warning in warnings)
    assert any('EQRF_PASSWORD' in warning for warning in warnings)


def test_admin_health_shows_production_safety_warnings(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    response = client.get('/admin')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Production safety' in html
    assert 'EQRF_SECRET_KEY' in html
    assert 'EQRF_PASSWORD' in html


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
    assert b'<h2 class="page-title">Admin</h2>' in admin_response.data


def test_admin_link_hidden_when_logged_out(client):
    response = client.get('/')

    assert response.status_code == 200
    assert b'>Admin<' not in response.data
    assert b'Admin Login' in response.data
    assert b'>Login<' not in response.data


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


def test_admin_dashboard_shows_audit_log_link_and_sections(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    response = client.get('/admin')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Audit Log' in html
    assert 'Manage local PDF extracts, checklist content' not in html
    for removed in ['Checklist count', 'Extract count', 'Quick Reference count', 'System posture', 'Extracts published', 'Checklists published']:
        assert removed not in html
    assert html.index('id="admin-extracts"') < html.index('id="admin-checklists"')


def test_admin_edit_pages_require_login(client):
    extract_response = client.get('/admin/extracts/edit?category=AIR&filename=MATS1.pdf')
    checklist_response = client.get('/admin/checklists/edit?path=AIR/Test')

    assert extract_response.status_code == 302
    assert checklist_response.status_code == 302


def test_admin_upload_includes_general_reference_option(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    response = client.get('/admin')

    assert response.status_code == 200
    assert b'General Reference' in response.data


def test_blank_category_upload_stores_as_general_reference(client, isolated_content, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    def fake_save(extracts):
        isolated_content.extracts.clear()
        isolated_content.extracts.update(extracts)

    monkeypatch.setattr(app_module, 'save_extracts', fake_save)

    response = client.post(
        '/admin/upload_pdf',
        data={
            'file': (io.BytesIO(b'%PDF-1.4\nblank category'), 'General.pdf'),
            'existing_category': '',
            'new_category': '',
            'orientation': 'auto',
            'title': 'General Upload',
        },
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '--' in isolated_content.extracts
    entry = isolated_content.extracts['--']['__files__'][0]
    assert file_entry_name(entry) == 'General.pdf'
    assert normalise_file_entry(entry)['jpgs'] == []


def test_requirements_no_longer_include_jpg_rendering_stack():
    requirements = (app_module.BASE_DIR / 'requirements.txt').read_text(encoding='utf-8')

    assert 'pdf2image' not in requirements
    assert 'Pillow' not in requirements
    assert 'pypdf' in requirements


def test_admin_can_see_general_reference_pdf(client, isolated_content, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    isolated_content.publish_pdf('AdminRef.pdf')
    isolated_content.extracts.update({'--': {'__files__': [{'pdf': 'AdminRef.pdf', 'title': 'Admin Ref'}]}})
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    response = client.get('/admin')

    assert response.status_code == 200
    assert b'General Reference' in response.data
    assert b'Admin Ref' in response.data


def test_trigger_refresh_requires_admin(client):
    response = client.post('/trigger-refresh')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_admin_trigger_refresh_redirects_to_admin_flashes_and_audits(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    response = client.post('/trigger-refresh', follow_redirects=True)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Refresh triggered for connected clients.' in html
    assert '<h2 class="page-title">Admin</h2>' in html
    assert any(entry['action'] == 'trigger_refresh' for entry in get_audit_log())


def test_trigger_refresh_json_response_for_api_clients(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    response = client.post('/trigger-refresh', headers={'Accept': 'application/json'})

    assert response.status_code == 200
    assert response.get_json() == {'ok': True}


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
    assert find_missing_jpgs(extracts, jpg_dir=tmp_path) == []

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
    assert b'GROUND' in response.data
    assert b'Empty' not in response.data
    assert b'Missing.pdf' not in response.data
    assert filtered_extract_tree(isolated_content.extracts) == {'GROUND': {'__files__': [{'pdf': 'NoJpg.pdf', 'jpgs': ['NoJpg_page1.jpg']}]}}


def test_extracts_index_hides_general_reference_sources(client, isolated_content):
    isolated_content.publish_pdf('HomeRef.pdf')
    isolated_content.publish_pdf('RootRef.pdf')
    isolated_content.publish_pdf('MiscRef.pdf')
    isolated_content.publish_pdf('Sid.pdf')
    isolated_content.extracts.update({
        '--': {'__files__': [{'pdf': 'HomeRef.pdf', 'title': 'Home Ref'}]},
        '__files__': [{'pdf': 'RootRef.pdf', 'title': 'Root Ref'}],
        'MISC': {'__files__': [{'pdf': 'MiscRef.pdf', 'title': 'Misc Ref'}]},
        'AIR': {'SID': {'__files__': [{'pdf': 'Sid.pdf', 'title': 'SID Ref'}]}},
    })

    response = client.get('/extracts')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'AIR' in html
    assert 'Home Ref' not in html
    assert 'Root Ref' not in html
    assert 'Misc Ref' not in html
    assert 'MISC' not in html


def test_extracts_index_still_shows_operational_misc_subcategories(client, isolated_content):
    isolated_content.publish_pdf('Ground.pdf')
    isolated_content.extracts.update({
        'MISC': {'GROUND': {'__files__': [{'pdf': 'Ground.pdf', 'title': 'Ground Ref'}]}},
    })

    response = client.get('/extracts')

    assert response.status_code == 200
    assert b'MISC' in response.data


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


def test_home_shows_general_reference_for_uncategorised_pdfs(client, isolated_content):
    isolated_content.publish_pdf('MATS1.pdf')
    isolated_content.extracts.update({'--': {'__files__': [{'pdf': 'MATS1.pdf', 'title': 'MATS 1'}]}})

    response = client.get('/')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'General Reference' in html
    assert 'MATS 1' in html
    assert '/viewer/--/MATS1.pdf' in html


def test_home_hides_general_reference_when_none_exist(client, isolated_content):
    response = client.get('/')

    assert response.status_code == 200
    assert b'General Reference' not in response.data


def test_general_reference_home_section_has_no_old_search_form(client, isolated_content):
    isolated_content.publish_pdf('MATS1.pdf')
    isolated_content.extracts.update({'--': {'__files__': [{'pdf': 'MATS1.pdf', 'title': 'MATS 1'}]}})

    response = client.get('/')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'General Reference' in html
    assert 'Search General Reference' not in html
    assert '/general-reference/search' not in html


def test_general_reference_collects_root_and_misc_entries(client, isolated_content):
    isolated_content.publish_pdf('RootRef.pdf')
    isolated_content.publish_pdf('MiscRef.pdf')
    isolated_content.extracts.update({
        '__files__': [{'pdf': 'RootRef.pdf', 'title': 'Root Ref'}],
        'MISC': {'__files__': [{'pdf': 'MiscRef.pdf', 'title': 'Misc Ref'}]},
    })

    entries = get_general_reference_entries()

    assert [entry['title'] for entry in entries] == ['Misc Ref', 'Root Ref']
    assert {entry['source_category'] for entry in entries} == {'--', 'MISC'}


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


@pytest.mark.parametrize('line', [
    'CAT A minimum',
    '[CAT A minimum]',
    '{CAT A MINIMUM}',
    'CAT A MIN',
    '[CAT A MIN]',
    'CAT A ONLY',
])
def test_critical_checklist_helper_supports_minimum_variants(line):
    assert is_critical_checklist_line(line)


def test_admin_checklist_preview_js_uses_shared_critical_pattern(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin/checklists/new'})

    response = client.get('/admin/checklists/new')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert r'CAT\s*A\s*(MIN|MINIMUM|ONLY)' in html
    assert 'preview-row preview-cat-a critical-strip cat-a-critical' in html


def test_critical_lines_are_not_removed_from_saved_checklist_items():
    checklists = {}
    lines = ['CAT A minimum', '[CAT A minimum]', 'CAT A MINIMUM', 'CAT A ONLY']

    upsert_checklist(checklists, 'Tower/GMC/Critical', lines)

    assert checklists['Tower']['GMC']['Critical'] == lines


def test_public_checklist_renders_cat_a_minimum_variants_as_critical(client, isolated_content):
    lines = ['CAT A minimum', '[CAT A minimum]', 'CAT A MINIMUM', 'CAT A ONLY']
    isolated_content.checklists.update({'Tower': {'GMC': {'Critical': lines}}})

    response = client.get('/checklists/Tower/GMC/Critical')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    for line in lines:
        assert line in html
    assert html.count('cat-a-critical') == len(lines)


def test_checklist_view_removes_clear_completed_and_keeps_reset(client, isolated_content):
    isolated_content.checklists.update({'Tower': {'GMC': {'Compact': ['Line up', 'CAT A ONLY']}}})

    response = client.get('/checklists/Tower/GMC/Compact')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Clear Completed' not in html
    assert 'clearCompletedChecklist' not in html
    assert 'Reset Checklist' in html
    assert 'resetChecklist()' in html


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


def test_extract_viewer_uses_professional_pdf_viewer_controls(client, isolated_content):
    isolated_content.publish_pdf('Viewer.pdf')
    isolated_content.extracts.update({
        'Tower': {
            'GMC': {
                '__files__': [{'pdf': 'Viewer.pdf', 'jpgs': ['Legacy_page1.jpg'], 'title': 'Viewer Extract'}],
            },
        },
    })

    response = client.get('/viewer/Tower/GMC/Viewer.pdf')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    for text in ['Fit Width', 'Fit Height', 'Rotate', 'Reset']:
        assert text in html
    assert 'pdf-viewer-shell' in html
    assert 'pdf-toolbar' in html
    assert 'pdf-scroll' in html
    assert 'pdf-page-stack' in html
    assert 'data-pdf-url="/pdfs/Viewer.pdf"' in html
    assert 'pdfjs/pdf.js' in html
    assert 'pdf-page-image extract-image' not in html


def test_extract_viewer_uses_pdfjs_stack_without_static_jpg_images(client, isolated_content):
    jpgs = isolated_content.publish_pdf('Multi.pdf', ['Multi_page1.jpg', 'Multi_page2.jpg'])
    isolated_content.extracts.update({'Tower': {'__files__': [{'pdf': 'Multi.pdf', 'jpgs': jpgs, 'title': 'Multi'}]}})

    response = client.get('/viewer/Tower/Multi.pdf')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="pdf-page-stack"' in html
    assert '/jpgs/' not in html
    assert '<img' not in html


def test_pdf_viewer_script_handles_fit_modes_rotation_and_layout():
    script = (app_module.BASE_DIR / 'static' / 'script.js').read_text(encoding='utf-8')

    assert 'mode: "custom"' in script
    assert 'state.scale = 1' in script
    assert 'state.mode = "height"' in script
    assert 'state.mode = "custom"' in script
    assert 'state.rotation = (state.rotation + 90) % 360' in script
    assert 'pdfjsLib.getDocument' in script
    assert 'page.render' in script
    assert 'state.mode === "width"' in script
    assert 'state.mode === "height"' in script
    assert 'scale(${viewerZoom})' not in script


def test_quick_reference_viewer_includes_pdf_search_controls(client, isolated_content):
    isolated_content.publish_pdf('Searchable.pdf')
    isolated_content.extracts.update({'--': {'__files__': [{'pdf': 'Searchable.pdf', 'title': 'Searchable'}]}})

    response = client.get('/viewer/--/Searchable.pdf')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="pdf-search-input"' in html
    assert 'id="pdf-search-prev"' in html
    assert 'id="pdf-search-next"' in html
    assert 'id="pdf-search-clear"' in html


def test_categorised_extract_viewer_hides_pdf_search_controls(client, isolated_content):
    isolated_content.publish_pdf('Searchable.pdf')
    isolated_content.extracts.update({'Tower': {'__files__': [{'pdf': 'Searchable.pdf', 'title': 'Searchable'}]}})

    response = client.get('/viewer/Tower/Searchable.pdf')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="pdf-search-input"' not in html
    assert 'id="pdf-search-prev"' not in html


def test_viewer_search_validates_registered_pdf_and_returns_json(client, isolated_content, monkeypatch):
    isolated_content.publish_pdf('Searchable.pdf')
    isolated_content.extracts.update({'--': {'__files__': [{'pdf': 'Searchable.pdf', 'title': 'Searchable'}]}})
    monkeypatch.setattr(app_module, 'search_pdf_text', lambda filename, query: [{'page': 1, 'snippet': 'wake turbulence'}])

    response = client.get('/viewer-search?category=--&filename=Searchable.pdf&q=wake')
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['filename'] == 'Searchable.pdf'
    assert payload['query'] == 'wake'
    assert payload['result_count'] == 1
    assert payload['results'][0]['page'] == 1


def test_viewer_search_returns_empty_results_for_no_match(client, isolated_content, monkeypatch):
    isolated_content.publish_pdf('Searchable.pdf')
    isolated_content.extracts.update({'--': {'__files__': [{'pdf': 'Searchable.pdf'}]}})
    monkeypatch.setattr(app_module, 'search_pdf_text', lambda filename, query: [])

    response = client.get('/viewer-search?category=--&filename=Searchable.pdf&q=absent')

    assert response.status_code == 200
    assert response.get_json()['result_count'] == 0


def test_viewer_search_rejects_categorised_pdfs(client, isolated_content):
    isolated_content.publish_pdf('Searchable.pdf')
    isolated_content.extracts.update({'Tower': {'__files__': [{'pdf': 'Searchable.pdf'}]}})

    response = client.get('/viewer-search?category=Tower&filename=Searchable.pdf&q=wake')

    assert response.status_code == 403


def test_viewer_search_rejects_unsafe_or_unregistered_files(client, isolated_content):
    isolated_content.publish_pdf('Registered.pdf')
    isolated_content.extracts.update({'--': {'__files__': ['Registered.pdf']}})

    unsafe_filename = client.get('/viewer-search?category=--&filename=../secret.pdf&q=x')
    unsafe_category = client.get('/viewer-search?category=../admin&filename=Registered.pdf&q=x')
    unregistered = client.get('/viewer-search?category=--&filename=Other.pdf&q=x')

    assert unsafe_filename.status_code == 400
    assert unsafe_category.status_code == 400
    assert unregistered.status_code == 404


def test_search_pdf_text_finds_known_term_from_cached_pages(monkeypatch):
    monkeypatch.setattr(app_module, 'get_cached_pdf_text_pages', lambda filename: [
        {'page': 1, 'text': 'Departure clearance only.'},
        {'page': 2, 'text': 'Wake turbulence separation applies.'},
    ])

    results = search_pdf_text('Any.pdf', 'wake')

    assert results == [{'page': 2, 'snippet': 'Wake turbulence separation applies.'}]


def test_pdf_text_cache_invalidates_when_file_changes(tmp_path, monkeypatch):
    pdf_dir = tmp_path / 'pdfs'
    pdf_dir.mkdir()
    cache_file = tmp_path / 'pdf_text_cache.json'
    pdf_path = pdf_dir / 'Cache.pdf'
    pdf_path.write_bytes(b'%PDF old')
    monkeypatch.setattr(app_module, 'PDF_DIR', pdf_dir)
    monkeypatch.setattr(app_module, 'PDF_TEXT_CACHE_JSON', cache_file)
    calls = []

    def fake_extract(filename):
      calls.append(filename)
      return [{'page': 1, 'text': f'extracted {len(calls)}'}]

    monkeypatch.setattr(app_module, 'extract_pdf_text_pages', fake_extract)

    first = get_cached_pdf_text_pages('Cache.pdf')
    second = get_cached_pdf_text_pages('Cache.pdf')
    pdf_path.write_bytes(b'%PDF changed and bigger')
    third = get_cached_pdf_text_pages('Cache.pdf')

    assert first == second
    assert third != first
    assert calls == ['Cache.pdf', 'Cache.pdf']


def test_pdf_viewer_script_calls_viewer_search_and_highlights_pages():
    script = (app_module.BASE_DIR / 'static' / 'script.js').read_text(encoding='utf-8')

    assert '/viewer-search?' in script
    assert 'data-page-number' in script
    assert 'search-current' in script
    assert 'search-hit' in script


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


def test_public_extract_viewer_hides_governance_metadata(client, isolated_content):
    jpgs = isolated_content.publish_pdf('Governed.pdf')
    isolated_content.extracts.update({
        'Tower': {
            'GMC': {
                '__files__': [{
                    'pdf': 'Governed.pdf',
                    'jpgs': jpgs,
                    'title': 'Governed Extract',
                    'version': '3.2',
                    'effective_date': '2026-05-27',
                    'expiry_date': '2026-12-31',
                    'review_date': '2026-10-01',
                    'owner': 'Ops',
                    'status': 'published',
                }],
            },
        },
    })

    response = client.get('/viewer/Tower/GMC/Governed.pdf')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    for text in ['Version', 'Effective', 'Expiry', 'Review', 'Owner', 'Published', '3.2', '2026-05-27']:
        assert text not in html


def test_public_checklist_view_hides_governance_metadata(client, isolated_content):
    upsert_checklist_with_metadata(
        isolated_content.checklists,
        'Tower/GMC/Runway Change',
        ['Line up'],
        {
            'title': 'Runway Change',
            'version': '2.0',
            'effective_date': '2026-05-27',
            'expiry_date': '2026-12-31',
            'review_date': '2026-10-01',
            'owner': 'Ops',
            'status': 'published',
        },
    )

    response = client.get('/checklists/Tower/GMC/Runway%20Change')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    for text in ['Version', 'Effective', 'Expiry', 'Review', 'Owner', 'Published', '2.0', '2026-05-27']:
        assert text not in html


def test_admin_pages_still_show_metadata_fields(client, monkeypatch):
    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    dashboard = client.get('/admin').get_data(as_text=True)
    checklist_edit = client.get('/admin/checklists/new').get_data(as_text=True)

    for html in [dashboard, checklist_edit]:
        assert 'Version' in html
        assert 'Effective date' in html or 'Effective' in html
        assert 'Expiry date' in html or 'Expiry' in html
        assert 'Review date' in html or 'Review' in html
        assert 'Owner' in html


def test_public_viewer_pages_render_single_breadcrumb(client, isolated_content):
    jpgs = isolated_content.publish_pdf('Valid.pdf')
    isolated_content.extracts.update({'Tower': {'GMC': {'__files__': [{'pdf': 'Valid.pdf', 'jpgs': jpgs, 'title': 'Valid Extract'}]}}})
    isolated_content.checklists.update({'Tower': {'GMC': {'Runway Change': ['Line up']}}})

    extract_html = client.get('/viewer/Tower/GMC/Valid.pdf').get_data(as_text=True)
    checklist_html = client.get('/checklists/Tower/GMC/Runway%20Change').get_data(as_text=True)

    assert extract_html.count('class="breadcrumb"') == 1
    assert checklist_html.count('class="breadcrumb"') == 1
    assert 'Tower / GMC / Valid Extract' not in extract_html
    assert 'Tower / GMC / Runway Change' not in checklist_html


def test_send_pdf_serves_registered_local_pdf(client, isolated_content):
    isolated_content.publish_pdf('Served.pdf')
    isolated_content.extracts.update({'AIR': {'__files__': ['Served.pdf']}})

    response = client.get('/pdfs/Served.pdf')

    assert response.status_code == 200
    assert response.content_type.startswith('application/pdf')
    assert response.headers.get('Accept-Ranges') == 'bytes'


def test_send_pdf_rejects_unsafe_or_unregistered_files(client, isolated_content):
    isolated_content.publish_pdf('Registered.pdf')
    isolated_content.extracts.update({'AIR': {'__files__': ['Registered.pdf']}})

    unsafe = client.get('/pdfs/../secret.pdf')
    unregistered = client.get('/pdfs/Other.pdf')

    assert unsafe.status_code == 404
    assert unregistered.status_code == 404


def test_legacy_jpg_route_is_not_public_viewer_surface(client):
    response = client.get('/jpgs/anything.jpg')

    assert response.status_code == 404


def test_resolve_refresh_target_home_and_admin(client, monkeypatch):
    assert client.get('/resolve-refresh-target?current=/').get_json()['target'] == '/'
    assert client.get('/resolve-refresh-target?current=/admin').get_json()['target'] == '/'

    monkeypatch.setenv('EQRF_PASSWORD', 'test-pass')
    client.post('/login', data={'password': 'test-pass', 'next': '/admin'})

    assert client.get('/resolve-refresh-target?current=/admin').get_json()['target'] == '/admin'


def test_resolve_refresh_target_valid_and_missing_checklist_paths(client, isolated_content):
    isolated_content.checklists.update({'TOWER': {'GMC': {'Runway 23': ['Line up']}}})

    valid = client.get('/resolve-refresh-target?current=/checklists/TOWER/GMC/Runway%2023')
    missing = client.get('/resolve-refresh-target?current=/checklists/TOWER/GMC/Missing')

    assert valid.get_json()['target'] == '/checklists/TOWER/GMC/Runway 23'
    assert missing.get_json()['target'] == '/checklists/TOWER/GMC'


def test_resolve_refresh_target_missing_extract_child_uses_parent(client, isolated_content):
    isolated_content.publish_pdf('Parking.pdf')
    isolated_content.extracts.update({'TOWER': {'GMC': {'__files__': ['Parking.pdf']}}})

    response = client.get('/resolve-refresh-target?current=/extracts/TOWER/GMC/Missing')

    assert response.get_json()['target'] == '/extracts/TOWER/GMC'


def test_resolve_refresh_target_missing_viewer_file_uses_extract_parent(client, isolated_content):
    isolated_content.publish_pdf('Parking.pdf')
    isolated_content.extracts.update({'TOWER': {'GMC': {'__files__': ['Parking.pdf']}}})

    response = client.get('/resolve-refresh-target?current=/viewer/TOWER/GMC/Missing.pdf')

    assert response.get_json()['target'] == '/extracts/TOWER/GMC'


def test_resolve_refresh_target_rejects_unsafe_external_paths(client):
    external = client.get('/resolve-refresh-target?current=https%3A%2F%2Fexample.com%2Fchecklists')
    protocol_relative = client.get('/resolve-refresh-target?current=%2F%2Fexample.com%2Fchecklists')
    traversal = client.get('/resolve-refresh-target?current=/checklists/../admin')

    assert external.get_json()['target'] == '/'
    assert protocol_relative.get_json()['target'] == '/'
    assert traversal.get_json()['target'] == '/'


def test_day_night_toggle_js_uses_localstorage():
    script = (app_module.BASE_DIR / 'static' / 'script.js').read_text(encoding='utf-8')

    assert 'localStorage' in script
    assert 'eqrf-theme' in script
    assert 'theme-day' in script
    assert 'theme-night' in script
    assert 'Day Mode' in script
    assert 'Night Mode' in script
    assert 'mode === "day" ? "Night Mode" : "Day Mode"' in script
    assert 'clearCompletedChecklist' not in script


def test_refresh_js_resolves_target_without_home_redirect():
    script = (app_module.BASE_DIR / 'static' / 'script.js').read_text(encoding='utf-8')
    refresh_block = script[script.index('addEventListener("refresh"'):]

    assert '/resolve-refresh-target' in refresh_block
    assert 'encodeURIComponent(current)' in refresh_block
    assert 'window.location.href = data.target' in refresh_block
    assert 'window.location.href = "/"' not in refresh_block


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


def test_extract_category_pages_do_not_render_duplicate_shortcut_strip(client, isolated_content):
    isolated_content.publish_pdf('Sid.pdf')
    isolated_content.publish_pdf('Parking.pdf')
    isolated_content.extracts.update({
        'AIR': {
            'SID': {'__files__': [{'pdf': 'Sid.pdf', 'title': 'SID'}]},
            'Parking': {'__files__': [{'pdf': 'Parking.pdf', 'title': 'Parking'}]},
        },
    })

    response = client.get('/extracts/AIR')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'category-nav-panel' not in html
    assert 'category-nav-list' not in html
    assert '<strong>SID</strong>' in html
    assert '<strong>Parking</strong>' in html


def test_checklist_category_pages_do_not_render_duplicate_shortcut_strip(client, isolated_content):
    isolated_content.checklists.update({'Tower': {'GMC': ['Line up'], 'ADC': ['Clearance']}})

    response = client.get('/checklists/Tower')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'category-nav-panel' not in html
    assert 'category-nav-list' not in html
    assert '<strong>GMC</strong>' in html
    assert '<strong>ADC</strong>' in html


def test_viewer_pages_do_not_render_duplicate_shortcut_strip(client, isolated_content):
    jpgs = isolated_content.publish_pdf('Valid.pdf')
    isolated_content.extracts.update({'AIR': {'__files__': [{'pdf': 'Valid.pdf', 'jpgs': jpgs, 'title': 'Valid'}]}})
    isolated_content.checklists.update({'Tower': {'GMC': {'Runway': ['Line up']}}})

    for path in ['/viewer/AIR/Valid.pdf', '/checklists/Tower/GMC/Runway']:
        response = client.get(path)
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'category-nav-panel' not in html
        assert 'category-nav-list' not in html


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
