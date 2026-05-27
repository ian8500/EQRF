import pytest

from app import JPG_DIR, app


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def test_home_loads(client):
    response = client.get('/')
    assert response.status_code == 200


def test_checklists_loads(client):
    response = client.get('/checklists')
    assert response.status_code == 200


def test_extracts_loads(client):
    response = client.get('/extracts')
    assert response.status_code == 200


def test_login_page_loads(client):
    response = client.get('/login')
    assert response.status_code == 200


def test_static_jpg_route_for_known_file(client):
    jpgs = sorted(JPG_DIR.glob('*.jpg'))
    if not jpgs:
        pytest.skip('No JPG assets available.')

    response = client.get(f'/jpgs/{jpgs[0].name}')
    assert response.status_code == 200
    assert response.content_type.startswith('image/jpeg')
