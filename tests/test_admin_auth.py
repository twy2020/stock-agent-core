import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.app import create_app


class AdminAuthTests(unittest.TestCase):
    headers = {'X-Core-Admin-Request': '1'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {'database': str(Path(self.temp.name) / 'core.db'), 'services': [], 'models': []}
        self.app = create_app(self.config, start_worker=False)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self, client=None, password='admin123'):
        return (client or self.client).post('/admin/api/auth/login', headers=self.headers,
                                           json={'username': 'admin', 'password': password})

    def change(self, current='admin123', new='test-admin-password'):
        return self.client.post('/admin/api/auth/password', headers=self.headers,
                                json={'currentPassword': current, 'newPassword': new})

    def test_first_login_requires_password_change_and_protects_all_admin_apis(self):
        result = self.login()
        self.assertTrue(result.json['mustChangePassword'])
        self.assertIn('HttpOnly', result.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', result.headers['Set-Cookie'])
        for method, route in [('get', 'status'), ('get', 'registry'), ('patch', 'configuration'), ('post','models/example/test')]:
            response = getattr(self.client, method)('/admin/api/' + route, headers=self.headers, json={})
            self.assertEqual(response.json['code'], 'ADMIN_PASSWORD_CHANGE_REQUIRED')
        self.assertEqual(self.change().status_code, 200)
        self.assertEqual(self.client.get('/admin/api/status').status_code, 200)

    def test_credentials_persist_across_restart_without_plaintext_storage(self):
        self.login(); self.change()
        store = self.app.extensions['core_store']
        with store.db() as db:
            row = dict(db.execute('SELECT * FROM admin_identity').fetchone())
        self.assertNotIn('test-admin-password', str(row))
        self.assertTrue(row['password_hash'].startswith('scrypt:'))
        client = create_app(self.config, start_worker=False).test_client()
        self.assertEqual(self.login(client).status_code, 401)
        self.assertFalse(self.login(client, 'test-admin-password').json['mustChangePassword'])

    def test_password_change_revokes_other_sessions_and_logout_revokes_cookie(self):
        self.login()
        other = self.app.test_client(); self.login(other)
        self.change()
        self.assertFalse(other.get('/admin/api/auth/session').json['authenticated'])
        cookie = self.client.get_cookie('core_admin_session', path='/admin').value
        self.assertEqual(self.client.post('/admin/api/auth/logout', headers=self.headers).status_code, 200)
        self.client.set_cookie('core_admin_session', cookie, path='/admin')
        self.assertEqual(self.client.get('/admin/api/status').status_code, 401)

    def test_password_validation_and_wrong_current_password(self):
        self.login()
        self.assertEqual(self.change(new='short').json['code'], 'ADMIN_INVALID_PASSWORD')
        self.assertEqual(self.change(current='wrong').status_code, 401)
        self.assertTrue(self.client.get('/admin/api/auth/session').json['mustChangePassword'])

    def test_csrf_origin_and_required_header(self):
        self.assertEqual(self.client.post('/admin/api/auth/login', json={}).status_code, 403)
        self.assertEqual(self.client.post('/admin/api/auth/login', headers={**self.headers, 'Origin': 'https://evil.test'}, json={}).status_code, 403)
        self.login()
        self.assertEqual(self.client.post('/admin/api/auth/password', json={}).status_code, 403)

    def test_rate_limit_and_expiry(self):
        for _ in range(5):
            self.assertEqual(self.login(password='wrong').status_code, 401)
        self.assertEqual(self.login().status_code, 429)
        import time
        with patch('agent_core.admin_auth.time.time', return_value=time.time() + 301):
            self.assertEqual(self.login().status_code, 200)

    def test_expired_sessions_and_legacy_tokens_rejected(self):
        self.login(); self.change()
        store = self.app.extensions['core_store']
        with store.db() as db:
            db.execute('UPDATE admin_sessions SET expires=0')
        self.assertEqual(self.client.get('/admin/api/status').status_code, 401)
        self.assertEqual(self.client.get('/admin/api/status', headers={'Authorization': 'Bearer admin'}).status_code, 401)


if __name__ == '__main__':
    unittest.main()
