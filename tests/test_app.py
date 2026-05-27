import pytest

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
    admin_response = client.get('/admin')
    assert admin_response.status_code == 200
    assert b'Content management console' in admin_response.data


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
