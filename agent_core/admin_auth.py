"""Instance-local administrator credentials and revocable browser sessions."""
import hashlib
import secrets
import time
from urllib.parse import urlsplit

from flask import g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from .security import CoreError


COOKIE = 'core_admin_session'
AUTH_PATH = '/admin/api/auth/'


def register_admin_auth(app, store):
    with store.db() as db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS admin_identity(id INTEGER PRIMARY KEY CHECK(id=1),
            username TEXT NOT NULL, password_hash TEXT NOT NULL, must_change INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS admin_sessions(digest TEXT PRIMARY KEY, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS admin_login_limits(ip TEXT PRIMARY KEY, failures INTEGER NOT NULL, until REAL NOT NULL);
        ''')
        if not db.execute('SELECT 1 FROM admin_identity').fetchone():
            db.execute('INSERT INTO admin_identity VALUES(1,?,?,1)',
                       ('admin', generate_password_hash('admin123')))

    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise CoreError('INVALID_REQUEST')
        return data

    def check_origin():
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            raise CoreError('ADMIN_ORIGIN_FORBIDDEN', 403)

    def authenticate():
        check_origin()
        if request.path == AUTH_PATH + 'login':
            if request.headers.get('X-Core-Admin-Request') != '1':
                raise CoreError('ADMIN_REQUEST_HEADER_REQUIRED', 403)
            return
        secret = request.cookies.get(COOKIE, '')
        with store.db() as db:
            row = db.execute('SELECT * FROM admin_identity WHERE id=1').fetchone()
            session = db.execute('SELECT 1 FROM admin_sessions WHERE digest=? AND expires>?',
                                 (digest(secret), time.time())).fetchone() if secret else None
        if not session:
            if request.path == AUTH_PATH + 'session' and request.method == 'GET':
                return
            raise CoreError('ADMIN_AUTHENTICATION_REQUIRED', 401)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('X-Core-Admin-Request') != '1':
            raise CoreError('ADMIN_REQUEST_HEADER_REQUIRED', 403)
        g.admin_session = digest(secret)
        g.admin_identity = dict(row)
        if row['must_change'] and request.path not in (AUTH_PATH + 'session', AUTH_PATH + 'password', AUTH_PATH + 'logout'):
            raise CoreError('ADMIN_PASSWORD_CHANGE_REQUIRED', 403)

    def create_session(db):
        secret = secrets.token_urlsafe(32)
        db.execute('DELETE FROM admin_sessions WHERE expires<=?', (time.time(),))
        db.execute('INSERT INTO admin_sessions VALUES(?,?)', (digest(secret), time.time() + 8 * 3600))
        return secret

    def session_response(username, must_change, secret):
        response = jsonify(username=username, mustChangePassword=bool(must_change))
        response.set_cookie(COOKIE, secret, httponly=True, secure=request.is_secure,
                            samesite='Strict', path='/admin', max_age=8 * 3600)
        return response

    @app.post(AUTH_PATH + 'login')
    def login():
        data = payload()
        username, password = data.get('username'), data.get('password')
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 120 or len(password) > 128:
            raise CoreError('INVALID_REQUEST')
        now = time.time()
        ip = request.remote_addr or 'local'
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            limit = db.execute('SELECT * FROM admin_login_limits WHERE ip=?', (ip,)).fetchone()
            if limit and limit['until'] > now and limit['failures'] >= 5:
                raise CoreError('ADMIN_LOGIN_RATE_LIMITED', 429)
            row = db.execute('SELECT * FROM admin_identity WHERE id=1').fetchone()
            valid_password = check_password_hash(row['password_hash'], password)
            valid = username == row['username'] and valid_password
            if not valid:
                failures = limit['failures'] + 1 if limit and limit['until'] > now else 1
                db.execute('INSERT OR REPLACE INTO admin_login_limits VALUES(?,?,?)', (ip, failures, now + 300))
            else:
                db.execute('DELETE FROM admin_login_limits WHERE ip=?', (ip,))
                secret = create_session(db)
        if not valid:
            raise CoreError('ADMIN_INVALID_CREDENTIALS', 401)
        return session_response(row['username'], row['must_change'], secret)

    @app.get(AUTH_PATH + 'session')
    def session():
        if not hasattr(g, 'admin_identity'):
            return jsonify(authenticated=False)
        return jsonify(authenticated=True, username=g.admin_identity['username'], mustChangePassword=bool(g.admin_identity['must_change']))

    @app.post(AUTH_PATH + 'password')
    def password():
        data = payload()
        current, new = data.get('currentPassword'), data.get('newPassword')
        if not isinstance(current, str) or len(current) > 128 or not isinstance(new, str) or not 10 <= len(new) <= 128 or new == current or new == 'admin123':
            raise CoreError('ADMIN_INVALID_PASSWORD')
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM admin_identity WHERE id=1').fetchone()
            if not check_password_hash(row['password_hash'], current):
                raise CoreError('ADMIN_INVALID_CREDENTIALS', 401)
            db.execute('UPDATE admin_identity SET password_hash=?, must_change=0 WHERE id=1',
                       (generate_password_hash(new),))
            db.execute('DELETE FROM admin_sessions')
            secret = create_session(db)
        return session_response(row['username'], False, secret)

    @app.post(AUTH_PATH + 'logout')
    def logout():
        with store.db() as db:
            db.execute('DELETE FROM admin_sessions WHERE digest=?', (g.admin_session,))
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE, path='/admin', samesite='Strict')
        return response

    return authenticate
