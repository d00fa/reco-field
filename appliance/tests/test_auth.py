from datetime import datetime, timedelta, timezone

from conftest import session_headers
from reco_field.auth import Session, SessionStore


def test_dashboard_has_no_login_form(client):
    page = client.get('/').text
    assert 'login-view' not in page
    assert 'logout-button' not in page
    assert 'class="app-shell" hidden' not in page
    assert client.post('/api/login', json={}).status_code == 404


def test_session_bootstrap_needs_no_credentials(client, runtime):
    response = client.get('/api/session')
    assert response.status_code == 200
    assert response.json()['csrf']
    assert client.get('/api/settings').status_code == 200
    assert client.get('/api/session').json() == response.json()
    assert response.headers['cache-control'] == 'no-store'
    assert not (runtime.data_dir / 'config' / 'auth.json').exists()
    assert not (runtime.data_dir / 'config' / 'first-login.txt').exists()


def test_cross_origin_session_and_writes_are_blocked(client):
    assert client.get('/api/session', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/session', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    headers = session_headers(client)
    settings = client.get('/api/settings').json()
    assert client.put('/api/settings', json=settings).status_code == 403
    assert client.put('/api/settings', json=settings, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.put('/api/settings', json=settings, headers=headers).status_code == 200


def test_session_cookie_is_http_only(client):
    cookie = client.get('/api/session').headers['set-cookie'].lower()
    assert 'httponly' in cookie
    assert 'samesite=strict' in cookie


def test_expired_session_can_reconnect_without_a_login(client):
    session_headers(client)
    store = client.app.state.auth
    token = client.cookies.get('reco_session')
    old = store.get_session(token)
    store._sessions[token] = Session(token, old.csrf, datetime.now(timezone.utc) - timedelta(seconds=1))
    assert client.get('/api/settings').status_code == 401
    session_headers(client)
    assert client.cookies.get('reco_session') != token
    assert client.get('/api/settings').status_code == 200


def test_anonymous_session_storage_is_bounded():
    store = SessionStore()
    first = store.create()
    for _ in range(1024):
        store.create()
    assert len(store._sessions) == 1024
    assert store.get_session(first.token) is None
