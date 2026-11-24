#!/usr/bin/env python3
"""AJZIO Education: self-hosted, persistent HSC learning community backend.
Uses only Python's standard library and SQLite. Run behind HTTPS for public hosting.
"""
from __future__ import annotations
import argparse, base64, hashlib, hmac, json, mimetypes, os, re, secrets, sqlite3, sys, time, uuid, threading, unicodedata
from datetime import datetime, timezone, timedelta
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('HSC_DATA_DIR', ROOT / 'data')).resolve()
UPLOADS = DATA / 'uploads'
DB = DATA / 'hsc-circle.sqlite3'
HOST = os.environ.get('HSC_HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '4173'))
MAX_BODY = 11 * 1024 * 1024
MAX_FILE = 10 * 1024 * 1024
SESSION_DAYS = 14
PBKDF2_ROUNDS = 310_000
AUTH_FAILURES = {}
AUTH_LOCK = threading.Lock()
SUBJECTS = ['Physics', 'Chemistry', 'Higher Mathematics', 'Biology', 'ICT', 'Bangla', 'English', 'Accounting', 'Finance, Banking & Insurance', 'Business Organization & Management', 'Economics', 'Sociology', 'Civics & Good Governance', 'Logic', 'History']



CLOUDINARY_CLOUD = os.environ.get('CLOUDINARY_CLOUD_NAME', '').strip()
CLOUDINARY_PRESET = os.environ.get('CLOUDINARY_UPLOAD_PRESET', '').strip()
FIREBASE_PROJECT = os.environ.get('FIREBASE_PROJECT_ID', '').strip()
FIREBASE_API_KEY = os.environ.get('FIREBASE_API_KEY', '').strip()
_SYNC_LOCK = threading.Lock()

def cloud_enabled():
    return bool(CLOUDINARY_CLOUD and CLOUDINARY_PRESET)

def firebase_enabled():
    return bool(FIREBASE_PROJECT and FIREBASE_API_KEY)

def upload_to_cloudinary(raw_bytes: bytes, filename: str, mime: str) -> str:
    if not cloud_enabled():
        return ''
    boundary = '----AJZIOBoundary' + uuid.uuid4().hex
    crlf = '\r\n'
    parts = [
        f'--{boundary}{crlf}',
        f'Content-Disposition: form-data; name="upload_preset"{crlf}{crlf}{CLOUDINARY_PRESET}{crlf}',
        f'--{boundary}{crlf}',
        f'Content-Disposition: form-data; name="file"; filename="{filename}"{crlf}',
        f'Content-Type: {mime}{crlf}{crlf}',
    ]
    head = ''.join(parts).encode('utf-8')
    tail = f'{crlf}--{boundary}--{crlf}'.encode('utf-8')
    url = f'https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/auto/upload'
    req = Request(url, data=head + raw_bytes + tail, headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    with urlopen(req, timeout=30) as resp:
        res = json.loads(resp.read().decode('utf-8'))
        return res.get('secure_url', '')

def restore_db_from_firebase():
    if not firebase_enabled() or DB.exists():
        return
    try:
        url = f'https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents/system/sqlite_backup?key={FIREBASE_API_KEY}'
        with urlopen(url, timeout=15) as resp:
            doc = json.loads(resp.read().decode('utf-8'))
        fields = doc.get('fields', {})
        chunks = int(fields.get('chunks', {}).get('integerValue', 0))
        if chunks > 0:
            b64_parts = []
            for i in range(chunks):
                c_url = f'https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents/system/sqlite_chunk_{i}?key={FIREBASE_API_KEY}'
                with urlopen(c_url, timeout=15) as c_resp:
                    c_doc = json.loads(c_resp.read().decode('utf-8'))
                b64_parts.append(c_doc['fields']['data']['stringValue'])
            DATA.mkdir(parents=True, exist_ok=True)
            DB.write_bytes(base64.b64decode(''.join(b64_parts)))
    except Exception:
        pass

def backup_db_to_firebase():
    if not firebase_enabled() or not DB.exists():
        return
    def _worker():
        if not _SYNC_LOCK.acquire(blocking=False):
            return
        try:
            tmp = DATA / 'backup_tmp.sqlite3'
            with sqlite3.connect(DB, timeout=20) as src, sqlite3.connect(tmp) as dst:
                src.backup(dst)
            raw = tmp.read_bytes()
            tmp.unlink(missing_ok=True)
            b64 = base64.b64encode(raw).decode('ascii')
            chunk_size = 700_000
            parts = [b64[i:i+chunk_size] for i in range(0, len(b64), chunk_size)] or ['']
            for idx, part in enumerate(parts):
                c_url = f'https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents/system/sqlite_chunk_{idx}?key={FIREBASE_API_KEY}'
                payload = json.dumps({'fields': {'data': {'stringValue': part}}}).encode('utf-8')
                req = Request(c_url, data=payload, method='PATCH', headers={'Content-Type': 'application/json'})
                urlopen(req, timeout=20).read()
            m_url = f'https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents/system/sqlite_backup?key={FIREBASE_API_KEY}'
            m_payload = json.dumps({'fields': {'chunks': {'integerValue': str(len(parts))}, 'updated_at': {'stringValue': now_iso()}}}).encode('utf-8')
            req = Request(m_url, data=m_payload, method='PATCH', headers={'Content-Type': 'application/json'})
            urlopen(req, timeout=20).read()
        except Exception:
            pass
        finally:
            _SYNC_LOCK.release()
    threading.Thread(target=_worker, daemon=True).start()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=20000')
    return con


def init_db():
    restore_db_from_firebase()
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, display_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
          class_name TEXT NOT NULL CHECK(class_name IN ('Class XI','Class XII')),
          study_group TEXT NOT NULL CHECK(study_group IN ('Science','Humanities','Business Studies')),
          language TEXT NOT NULL DEFAULT 'bn' CHECK(language IN ('bn','en')),
          role TEXT NOT NULL DEFAULT 'student' CHECK(role IN ('student','moderator','admin')),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
        CREATE TABLE IF NOT EXISTS notes (
          id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
          author_name TEXT NOT NULL, title TEXT NOT NULL, subject TEXT NOT NULL,
          paper TEXT NOT NULL DEFAULT '', class_name TEXT NOT NULL, study_group TEXT NOT NULL,
          chapter TEXT NOT NULL, note_type TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          content_language TEXT NOT NULL CHECK(content_language IN ('bn','en')),
          file_id TEXT, file_name TEXT, file_mime TEXT, file_size INTEGER,
          status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','changes_requested')),
          reviewer_id TEXT REFERENCES users(id) ON DELETE SET NULL, review_note TEXT NOT NULL DEFAULT '',
          featured INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, reviewed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS notes_status_idx ON notes(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS notes_subject_idx ON notes(subject, class_name, study_group);
        CREATE TABLE IF NOT EXISTS note_permissions (
          id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          permission_text_version TEXT NOT NULL, granted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS note_feedback (
          id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          rating INTEGER CHECK(rating BETWEEN 1 AND 5), report_text TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, UNIQUE(note_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS note_requests (
          id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
          author_name TEXT NOT NULL, title TEXT NOT NULL, subject TEXT NOT NULL,
          class_name TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','fulfilled','closed')),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exams (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, subject TEXT NOT NULL, class_name TEXT NOT NULL,
          study_group TEXT NOT NULL, chapter TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 1 AND 180),
          opens_at TEXT, closes_at TEXT, status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','closed')),
          created_by TEXT REFERENCES users(id) ON DELETE SET NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS exams_status_idx ON exams(status, opens_at);
        CREATE TABLE IF NOT EXISTS questions (
          id TEXT PRIMARY KEY, exam_id TEXT NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          prompt TEXT NOT NULL, option_a TEXT NOT NULL, option_b TEXT NOT NULL,
          option_c TEXT NOT NULL, option_d TEXT NOT NULL,
          correct_option INTEGER NOT NULL CHECK(correct_option BETWEEN 0 AND 3),
          explanation TEXT NOT NULL DEFAULT '', points INTEGER NOT NULL DEFAULT 1 CHECK(points BETWEEN 1 AND 10),
          position INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS exam_sessions (
          id TEXT PRIMARY KEY, exam_id TEXT NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          started_at TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS exam_sessions_user_idx ON exam_sessions(exam_id,user_id,expires_at);
        CREATE TABLE IF NOT EXISTS attempts (
          id TEXT PRIMARY KEY, exam_id TEXT NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          score INTEGER NOT NULL, max_score INTEGER NOT NULL, started_at TEXT NOT NULL,
          submitted_at TEXT NOT NULL, UNIQUE(exam_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS attempt_answers (
          id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
          question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
          selected_option INTEGER NOT NULL CHECK(selected_option BETWEEN -1 AND 3),
          is_correct INTEGER NOT NULL, points_earned INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS activity_user_idx ON activity(user_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS onboarding_responses (
          user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          learning_goal TEXT NOT NULL, biggest_challenge TEXT NOT NULL,
          preferred_features TEXT NOT NULL DEFAULT '[]', improvement_text TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS student_feedback (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          category TEXT NOT NULL CHECK(category IN ('feature','bug','content','other')),
          title TEXT NOT NULL, details TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','under_review','done','closed')),
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS student_feedback_status_idx ON student_feedback(status,created_at DESC);
        CREATE TABLE IF NOT EXISTS member_counter (
          id INTEGER PRIMARY KEY CHECK(id=1), last_value INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 600),
          tagged_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          reply_to_id TEXT REFERENCES chat_messages(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS chat_messages_created_idx ON chat_messages(created_at DESC);
        CREATE TABLE IF NOT EXISTS chat_mentions (
          id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
          target_type TEXT NOT NULL CHECK(target_type IN ('role','member')), target_value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chat_mentions_target_idx ON chat_mentions(target_type,target_value);
        CREATE TABLE IF NOT EXISTS moderation_events (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          violation_type TEXT NOT NULL CHECK(violation_type IN ('contact_sharing','abusive_language','spam')),
          action TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS moderation_user_date_idx ON moderation_events(user_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS account_appeals (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','declined')),
          admin_response TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          reviewed_by TEXT REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS appeals_status_date_idx ON account_appeals(status,created_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending_appeal_per_user ON account_appeals(user_id) WHERE status='pending';
        CREATE TABLE IF NOT EXISTS chat_reports (
          id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
          reporter_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          reason TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', reviewed_at TEXT,
          UNIQUE(message_id,reporter_id)
        );
        CREATE TABLE IF NOT EXISTS admin_audit_log (
          id TEXT PRIMARY KEY, admin_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
          details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS admin_audit_created_idx ON admin_audit_log(created_at DESC);
        CREATE TABLE IF NOT EXISTS google_oauth_states (
          state_hash TEXT PRIMARY KEY, nonce TEXT NOT NULL, code_verifier TEXT NOT NULL,
          redirect_uri TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS google_oauth_pending (
          token_hash TEXT PRIMARY KEY, google_sub TEXT NOT NULL, email TEXT NOT NULL,
          display_name TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        ''')
        # Backwards-compatible additive migrations. Existing students keep their completed profile state.
        user_columns={r['name'] for r in db.execute('PRAGMA table_info(users)').fetchall()}
        if 'education_board' not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN education_board TEXT NOT NULL DEFAULT ''")
        if 'preferred_language' not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN preferred_language TEXT NOT NULL DEFAULT 'bn'")
        if 'survey_completed' not in user_columns:
            db.execute('ALTER TABLE users ADD COLUMN survey_completed INTEGER NOT NULL DEFAULT 1')
        if 'google_sub' not in user_columns:
            db.execute('ALTER TABLE users ADD COLUMN google_sub TEXT')
        if 'member_no' not in user_columns:
            db.execute('ALTER TABLE users ADD COLUMN member_no INTEGER')
        if 'account_status' not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'")
        if 'suspended_until' not in user_columns:
            db.execute('ALTER TABLE users ADD COLUMN suspended_until TEXT')
        if 'restriction_reason' not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN restriction_reason TEXT NOT NULL DEFAULT ''")
        if 'bio' not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
        if 'date_of_birth' not in user_columns:
            db.execute('ALTER TABLE users ADD COLUMN date_of_birth TEXT')
        if 'show_dob' not in user_columns:
            db.execute('ALTER TABLE users ADD COLUMN show_dob INTEGER NOT NULL DEFAULT 0')
        if 'avatar_file' not in user_columns:
            db.execute('ALTER TABLE users ADD COLUMN avatar_file TEXT')
        chat_columns={r['name'] for r in db.execute('PRAGMA table_info(chat_messages)').fetchall()}
        if 'reply_to_id' not in chat_columns:
            db.execute('ALTER TABLE chat_messages ADD COLUMN reply_to_id TEXT REFERENCES chat_messages(id) ON DELETE SET NULL')
        report_columns={r['name'] for r in db.execute('PRAGMA table_info(chat_reports)').fetchall()}
        if 'status' not in report_columns:
            db.execute("ALTER TABLE chat_reports ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
        if 'reviewed_at' not in report_columns:
            db.execute('ALTER TABLE chat_reports ADD COLUMN reviewed_at TEXT')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub_idx ON users(google_sub) WHERE google_sub IS NOT NULL')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_member_no_idx ON users(member_no) WHERE member_no IS NOT NULL')
        db.execute('INSERT OR IGNORE INTO member_counter(id,last_value) VALUES(1,999)')
        current_max=db.execute('SELECT COALESCE(MAX(member_no),999) n FROM users').fetchone()['n']
        db.execute('UPDATE member_counter SET last_value=MAX(last_value,?) WHERE id=1',(current_max,))
        missing=db.execute('SELECT id FROM users WHERE member_no IS NULL ORDER BY created_at,id').fetchall()
        for old_user in missing:
            db.execute('UPDATE member_counter SET last_value=last_value+1 WHERE id=1')
            number=db.execute('SELECT last_value FROM member_counter WHERE id=1').fetchone()['last_value']
            db.execute('UPDATE users SET member_no=? WHERE id=?',(number,old_user['id']))
        db.execute('DELETE FROM google_oauth_states WHERE expires_at<=?',(now_iso(),))
        db.execute('DELETE FROM google_oauth_pending WHERE expires_at<=?',(now_iso(),))
        count = db.execute('SELECT COUNT(*) n FROM exams').fetchone()['n']
        if not count:
            exam_id = 'exam-ict-number-system-001'
            db.execute('''INSERT INTO exams(id,title,subject,class_name,study_group,chapter,description,duration_minutes,status,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?)''', (exam_id, 'ICT Practice: Number System', 'ICT', 'All HSC', 'All', 'Number System & Digital Devices', 'A short foundational practice set. This is independent practice, not an official board examination.', 12, 'published', now_iso()))
            qs = [
              ('Which number system uses only 0 and 1?', ['Decimal','Binary','Octal','Hexadecimal'], 1, 'The binary system has base 2 and uses the digits 0 and 1.'),
              ('What is the decimal value of binary 1010?', ['8','9','10','12'], 2, '1010₂ = 1×8 + 0×4 + 1×2 + 0×1 = 10.'),
              ('How many bits make one byte?', ['4','8','16','32'], 1, 'One byte is conventionally made up of 8 bits.'),
              ('Which base is used by the octal number system?', ['2','8','10','16'], 1, 'Octal is a base-8 system and uses digits 0 through 7.'),
              ('Which digit is not valid in the hexadecimal system?', ['A','F','9','G'], 3, 'Hexadecimal uses 0–9 and A–F. G is not a valid hexadecimal digit.'),
            ]
            for pos, (prompt, opts, answer, explanation) in enumerate(qs):
                db.execute('''INSERT INTO questions(id,exam_id,prompt,option_a,option_b,option_c,option_d,correct_option,explanation,points,position)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (str(uuid.uuid4()), exam_id, prompt, *opts, answer, explanation, 1, pos))
        db.execute('DELETE FROM sessions WHERE expires_at<=?', (now_iso(),))
        admin_email = os.environ.get('HSC_ADMIN_EMAIL', '').strip().lower()
        if admin_email:
            db.execute("UPDATE users SET role='admin' WHERE email=?", (admin_email,))
    # Remove orphaned upload files left by interrupted writes or replaced revisions.
    with connect() as db:
        referenced={r['file_id'] for r in db.execute('SELECT file_id FROM notes WHERE file_id IS NOT NULL')}
        referenced.update(r['avatar_file'] for r in db.execute('SELECT avatar_file FROM users WHERE avatar_file IS NOT NULL'))
    for candidate in UPLOADS.iterdir():
        if candidate.is_file() and candidate.name not in referenced:
            try: candidate.unlink()
            except OSError: pass


def password_digest(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()


def safe_user(row):
    keys=('id','display_name','email','class_name','study_group','language','role','created_at','education_board','preferred_language','survey_completed','member_no','account_status','suspended_until','restriction_reason','bio','date_of_birth','show_dob','avatar_file')
    return {k: row[k] for k in keys if k in row.keys()}


class AccountRestricted(Exception):
    def __init__(self, user):
        self.user=user
        super().__init__('This account is temporarily restricted.')


class AppHandler(BaseHTTPRequestHandler):
    server_version = 'AJZIO Education/1.0'
    def log_message(self, fmt, *args):
        if os.environ.get('HSC_ACCESS_LOG') == '1':
            super().log_message(fmt, *args)

    def _headers(self, status=200, content_type='application/json; charset=utf-8', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Permitted-Cross-Domain-Policies', 'none')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https://res.cloudinary.com; connect-src 'self' https://api.cloudinary.com; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        if self.headers.get('X-Forwarded-Proto','').lower()=='https': self.send_header('Strict-Transport-Security','max-age=31536000; includeSubDomains')
        self.send_header('Cache-Control', 'no-store' if content_type.startswith('application/json') else 'no-cache')
        if extra:
            for k,v in extra.items(): self.send_header(k,v)
        self.end_headers()

    def send_json(self, data, status=200, extra=None):
        if self.command in ('POST', 'PATCH', 'DELETE') and 200 <= status < 300:
            backup_db_to_firebase()
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self._headers(status, extra=extra)
        self.wfile.write(raw)

    def read_body(self):
        n = int(self.headers.get('Content-Length','0') or 0)
        if n < 0 or n > MAX_BODY: raise ValueError('Request is too large.')
        return self.rfile.read(n)

    def read_json(self):
        raw = self.read_body()
        if not raw: return {}
        obj = json.loads(raw.decode('utf-8'))
        if not isinstance(obj, dict): raise ValueError('Invalid request body.')
        return obj

    def path_parts(self):
        return [unquote(p) for p in urlparse(self.path).path.strip('/').split('/') if p]

    def current_user(self, db):
        cookie = SimpleCookie(self.headers.get('Cookie',''))
        morsel = cookie.get('hsc_session')
        if not morsel: return None
        token_hash = hashlib.sha256(morsel.value.encode()).hexdigest()
        row = db.execute('''SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
          WHERE s.token_hash=? AND s.expires_at>?''', (token_hash, now_iso())).fetchone()
        if not row: return None
        if row['account_status']=='suspended' and row['suspended_until'] and row['suspended_until']<=now_iso():
            db.execute("UPDATE users SET account_status='active',suspended_until=NULL,restriction_reason='' WHERE id=?",(row['id'],))
            row=db.execute('SELECT * FROM users WHERE id=?',(row['id'],)).fetchone()
        return safe_user(row)

    def require_user(self, db):
        user = self.current_user(db)
        if not user: raise PermissionError('Please sign in to continue.')
        if user.get('account_status')=='locked' or (user.get('account_status')=='suspended' and user.get('suspended_until') and user['suspended_until']>now_iso()):
            raise AccountRestricted(user)
        return user

    def require_staff(self, db):
        user = self.require_user(db)
        if user['role'] not in ('moderator','admin'): raise PermissionError('Moderator permission is required.')
        return user

    def require_admin(self, db):
        user=self.require_user(db)
        if user['role']!='admin': raise PermissionError('Administrator permission is required.')
        return user

    def check_origin(self):
        origin = self.headers.get('Origin')
        if not origin: return
        parsed = urlparse(origin)
        host = self.headers.get('Host','')
        if parsed.netloc != host:
            raise PermissionError('Cross-origin request blocked.')

    def _google_oauth_ready(self):
        return bool(os.environ.get('GOOGLE_CLIENT_ID') and os.environ.get('GOOGLE_CLIENT_SECRET') and os.environ.get('GOOGLE_REDIRECT_URI'))

    def _redirect(self, location, cookies=None):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for cookie in (cookies or []): self.send_header('Set-Cookie', cookie)
        self.end_headers()

    def _google_callback(self, query):
        if not self._google_oauth_ready():
            self._redirect('/?oauth_error=not_configured'); return
        cookie=SimpleCookie(self.headers.get('Cookie','')); state_cookie=cookie.get('hsc_google_state')
        state=query.get('state',[''])[0]; code=query.get('code',[''])[0]
        if not state_cookie or not state or not hmac.compare_digest(state_cookie.value,state) or not code:
            self._redirect('/?oauth_error=invalid_state',["hsc_google_state=; Path=/auth/google; HttpOnly; SameSite=Lax; Max-Age=0"]); return
        with connect() as db:
            state_row=db.execute('SELECT * FROM google_oauth_states WHERE state_hash=? AND expires_at>?',(hashlib.sha256(state.encode()).hexdigest(),now_iso())).fetchone()
            if not state_row:
                self._redirect('/?oauth_error=expired_state',["hsc_google_state=; Path=/auth/google; HttpOnly; SameSite=Lax; Max-Age=0"]); return
            db.execute('DELETE FROM google_oauth_states WHERE state_hash=?',(hashlib.sha256(state.encode()).hexdigest(),))
        try:
            payload=urlencode({'code':code,'client_id':os.environ['GOOGLE_CLIENT_ID'],'client_secret':os.environ['GOOGLE_CLIENT_SECRET'],'redirect_uri':state_row['redirect_uri'],'grant_type':'authorization_code','code_verifier':state_row['code_verifier']}).encode()
            req=Request('https://oauth2.googleapis.com/token',data=payload,headers={'Content-Type':'application/x-www-form-urlencoded'},method='POST')
            with urlopen(req,timeout=15) as response: token_data=json.loads(response.read().decode())
            id_token=token_data.get('id_token')
            if not id_token: raise ValueError('Google did not return an ID token.')
            verify_url='https://oauth2.googleapis.com/tokeninfo?'+urlencode({'id_token':id_token})
            with urlopen(verify_url,timeout=15) as response: claims=json.loads(response.read().decode())
            expected_issuer=('accounts.google.com','https://accounts.google.com')
            if claims.get('aud')!=os.environ['GOOGLE_CLIENT_ID'] or claims.get('iss') not in expected_issuer or claims.get('email_verified') not in (True,'true','True') or claims.get('nonce')!=state_row['nonce'] or int(claims.get('exp','0'))<=int(time.time()):
                raise ValueError('Google identity verification failed.')
            sub=str(claims.get('sub','')); email=str(claims.get('email','')).lower().strip(); name=str(claims.get('name') or email.split('@')[0]).strip()[:40]
            if not sub or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): raise ValueError('Google account did not provide a verified email.')
        except Exception as exc:
            self.log_error('Google OAuth failure: %s',exc)
            self._redirect('/?oauth_error=verification_failed',["hsc_google_state=; Path=/auth/google; HttpOnly; SameSite=Lax; Max-Age=0"]); return
        with connect() as db:
            row=db.execute('SELECT * FROM users WHERE google_sub=?',(sub,)).fetchone()
            if row:
                token=secrets.token_urlsafe(36); hashed=hashlib.sha256(token.encode()).hexdigest(); exp=(datetime.now(timezone.utc)+timedelta(days=SESSION_DAYS)).isoformat(timespec='seconds')
                db.execute('INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)',(hashed,row['id'],exp,now_iso()))
                secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''
                cookies=[f'hsc_session={token}; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age={SESSION_DAYS*86400}',"hsc_google_state=; Path=/auth/google; HttpOnly; SameSite=Lax; Max-Age=0"]
                backup_db_to_firebase(); self._redirect('/?google=success',cookies); return
            existing=db.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
            if existing:
                self._redirect('/?oauth_error=email_exists',["hsc_google_state=; Path=/auth/google; HttpOnly; SameSite=Lax; Max-Age=0"]); return
            pending=secrets.token_urlsafe(36); pending_hash=hashlib.sha256(pending.encode()).hexdigest(); expires=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(timespec='seconds')
            db.execute('INSERT INTO google_oauth_pending(token_hash,google_sub,email,display_name,expires_at) VALUES(?,?,?,?,?)',(pending_hash,sub,email,name,expires))
        secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''
        cookies=[f'hsc_google_pending={pending}; Path=/api/auth/google; HttpOnly; SameSite=Lax{secure}; Max-Age=600',"hsc_google_state=; Path=/auth/google; HttpOnly; SameSite=Lax; Max-Age=0"]
        backup_db_to_firebase(); self._redirect('/?google=finish',cookies)

    def do_GET(self):
        parts = self.path_parts()
        try:
            if parts == ['auth','google','start']:
                if not self._google_oauth_ready(): self._redirect('/?oauth_error=not_configured'); return
                state=secrets.token_urlsafe(32); nonce=secrets.token_urlsafe(24); verifier=secrets.token_urlsafe(48)
                challenge=hashlib.sha256(verifier.encode()).digest()
                import base64
                challenge_b64=base64.urlsafe_b64encode(challenge).rstrip(b'=').decode()
                redirect_uri=os.environ['GOOGLE_REDIRECT_URI']
                with connect() as db:
                    db.execute('INSERT INTO google_oauth_states(state_hash,nonce,code_verifier,redirect_uri,expires_at) VALUES(?,?,?,?,?)',(hashlib.sha256(state.encode()).hexdigest(),nonce,verifier,redirect_uri,(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(timespec='seconds')))
                params={'client_id':os.environ['GOOGLE_CLIENT_ID'],'redirect_uri':redirect_uri,'response_type':'code','scope':'openid email profile','state':state,'nonce':nonce,'code_challenge':challenge_b64,'code_challenge_method':'S256','prompt':'select_account'}
                secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''
                self._redirect('https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params),[f'hsc_google_state={state}; Path=/auth/google; HttpOnly; SameSite=Lax{secure}; Max-Age=600']); return
            if parts == ['auth','google','callback']:
                self._google_callback(parse_qs(urlparse(self.path).query)); return
            if not parts or parts[0] not in ('api',):
                self.serve_static(parts); return
            with connect() as db:
                user = self.current_user(db)
                if parts == ['api','health']:
                    return self.send_json({'ok':True,'app':'AJZIO Education','database':'sqlite'})
                if parts == ['api','config']:
                    return self.send_json({'google_oauth':self._google_oauth_ready(),'free_access':True,'bilingual':True})
                if parts == ['api','auth','google','pending']:
                    c=SimpleCookie(self.headers.get('Cookie','')); m=c.get('hsc_google_pending')
                    if not m: return self.send_json({'pending':False})
                    row=db.execute('SELECT email,display_name FROM google_oauth_pending WHERE token_hash=? AND expires_at>?',(hashlib.sha256(m.value.encode()).hexdigest(),now_iso())).fetchone()
                    return self.send_json({'pending':bool(row),'profile':dict(row) if row else None})
                if parts == ['api','me']:
                    return self.send_json({'user':user})
                if len(parts)==4 and parts[:3]==['api','profile','avatar']:
                    self.require_user(db)
                    try: member_no=int(parts[3])
                    except ValueError: return self.send_json({'error':'Avatar not found.'},404)
                    avatar=db.execute("SELECT avatar_file FROM users WHERE member_no=? AND account_status<>'locked'",(member_no,)).fetchone()
                    if not avatar or not avatar['avatar_file']: return self.send_json({'error':'Avatar not found.'},404)
                    filename=avatar['avatar_file']
                    if filename.startswith('http://') or filename.startswith('https://'): return self._redirect(filename)
                    if not re.fullmatch(r'[a-f0-9]{32}\.(?:png|jpg|webp)',filename): return self.send_json({'error':'Avatar not found.'},404)
                    path=(UPLOADS/filename).resolve()
                    if path.parent!=UPLOADS.resolve() or not path.is_file(): return self.send_json({'error':'Avatar not found.'},404)
                    raw=path.read_bytes(); mime={'png':'image/png','jpg':'image/jpeg','webp':'image/webp'}[path.suffix[1:]]
                    self._headers(200,mime,{'Content-Length':str(len(raw)),'Cache-Control':'private, max-age=300'})
                    self.wfile.write(raw); return
                if parts==['api','profile','me']:
                    u=self.require_user(db)
                    profile=db.execute("SELECT member_no,display_name,class_name,study_group,education_board,role,created_at,bio,date_of_birth,show_dob,avatar_file FROM users WHERE id=?",(u['id'],)).fetchone()
                    row=dict(profile); row['avatar_url']=f"/api/profile/avatar/{row['member_no']}" if row['avatar_file'] else None
                    return self.send_json({'profile':row})
                if len(parts)==3 and parts[:2]==['api','profile']:
                    self.require_user(db)
                    try: member_no=int(parts[2])
                    except ValueError: return self.send_json({'error':'Profile not found.'},404)
                    profile=db.execute("SELECT member_no,display_name,class_name,study_group,education_board,role,created_at,bio,date_of_birth,show_dob,avatar_file FROM users WHERE member_no=? AND account_status<>'locked'",(member_no,)).fetchone()
                    if not profile: return self.send_json({'error':'Profile not found.'},404)
                    row=dict(profile)
                    if not row['show_dob']: row['date_of_birth']=None
                    row['avatar_url']=f"/api/profile/avatar/{row['member_no']}" if row['avatar_file'] else None
                    row.pop('avatar_file',None)
                    return self.send_json({'profile':row})
                if parts==['api','members','search']:
                    u=self.require_user(db); q=parse_qs(urlparse(self.path).query).get('q',[''])[0].strip()[:40]
                    if len(q)<2: return self.send_json({'members':[]})
                    like='%'+q.replace('%','\\%').replace('_','\\_')+'%'
                    rows=db.execute("SELECT member_no,display_name,role FROM users WHERE account_status='active' AND (display_name LIKE ? ESCAPE '\\' OR CAST(member_no AS TEXT) LIKE ? ESCAPE '\\') ORDER BY CASE WHEN CAST(member_no AS TEXT)=? THEN 0 ELSE 1 END,display_name LIMIT 8",(like,like,q)).fetchall()
                    return self.send_json({'members':[dict(r) for r in rows]})
                if parts == ['api','chat']:
                    u=self.require_user(db)
                    db.execute("DELETE FROM chat_messages WHERE created_at<?",((datetime.now(timezone.utc)-timedelta(days=30)).isoformat(timespec='seconds'),))
                    rows=db.execute('''SELECT m.rowid message_order,m.id,m.message,m.tagged_admin,m.created_at,m.reply_to_id,u.display_name,u.member_no,u.avatar_file,
                      p.message reply_message,pu.display_name reply_display_name,pu.member_no reply_member_no
                      FROM chat_messages m JOIN users u ON u.id=m.user_id
                      LEFT JOIN chat_messages p ON p.id=m.reply_to_id LEFT JOIN users pu ON pu.id=p.user_id
                      ORDER BY m.created_at DESC,m.rowid DESC LIMIT 100''').fetchall()
                    messages=[]
                    for x in sorted(rows,key=lambda r:r['message_order']):
                        item=dict(x); item.pop('message_order',None); item['avatar_url']=f"/api/profile/avatar/{item['member_no']}" if item['avatar_file'] else None; item.pop('avatar_file',None)
                        item['mentions']=[dict(m) for m in db.execute('SELECT target_type,target_value FROM chat_mentions WHERE message_id=?',(item['id'],)).fetchall()]
                        messages.append(item)
                    return self.send_json({'messages':messages,'member_no':u['member_no']})
                if parts == ['api','appeal']:
                    if not user: return self.send_json({'appeal':None})
                    a=db.execute('SELECT id,details,status,admin_response,created_at,updated_at FROM account_appeals WHERE user_id=? ORDER BY created_at DESC LIMIT 1',(user['id'],)).fetchone()
                    return self.send_json({'appeal':dict(a) if a else None,'restricted':user.get('account_status')!='active','user':user})
                if parts == ['api','admin','users']:
                    self.require_admin(db)
                    rows=db.execute('''SELECT id,member_no,display_name,email,class_name,study_group,education_board,role,account_status,suspended_until,restriction_reason,created_at
                      FROM users ORDER BY member_no ASC LIMIT 500''').fetchall()
                    return self.send_json({'users':[dict(x) for x in rows]})
                if parts == ['api','admin','appeals']:
                    self.require_admin(db)
                    rows=db.execute('''SELECT a.id,a.user_id,a.details,a.status,a.admin_response,a.created_at,a.updated_at,u.member_no,u.display_name,u.email,u.account_status,u.suspended_until
                      FROM account_appeals a JOIN users u ON u.id=a.user_id ORDER BY CASE a.status WHEN 'pending' THEN 0 ELSE 1 END,a.created_at DESC LIMIT 200''').fetchall()
                    return self.send_json({'appeals':[dict(x) for x in rows]})
                if parts == ['api','admin','chat']:
                    admin=self.require_admin(db)
                    rows=db.execute('''SELECT m.id,m.message,m.tagged_admin,m.created_at,m.reply_to_id,u.member_no,u.display_name
                      FROM chat_messages m JOIN users u ON u.id=m.user_id
                      WHERE m.tagged_admin=1 OR EXISTS(SELECT 1 FROM chat_mentions cm WHERE cm.message_id=m.id AND cm.target_type='role' AND cm.target_value='admin')
                        OR EXISTS(SELECT 1 FROM chat_mentions cm WHERE cm.message_id=m.id AND cm.target_type='member' AND cm.target_value=?)
                      ORDER BY m.created_at DESC LIMIT 100''',(str(admin['member_no']),)).fetchall()
                    return self.send_json({'messages':[dict(x) for x in rows]})
                if parts == ['api','admin','moderation']:
                    self.require_admin(db)
                    rows=db.execute('''SELECT e.id,e.violation_type,e.action,e.created_at,u.member_no,u.display_name
                      FROM moderation_events e JOIN users u ON u.id=e.user_id ORDER BY e.created_at DESC LIMIT 200''').fetchall()
                    return self.send_json({'events':[dict(x) for x in rows]})
                if parts == ['api','admin','audit']:
                    self.require_admin(db)
                    rows=db.execute('''SELECT a.id,a.action,a.target_type,a.target_id,a.details,a.created_at,u.member_no admin_member_no,u.display_name admin_name
                      FROM admin_audit_log a JOIN users u ON u.id=a.admin_id ORDER BY a.created_at DESC LIMIT 200''').fetchall()
                    return self.send_json({'events':[dict(x) for x in rows]})
                if parts == ['api','admin','chat-reports']:
                    self.require_admin(db)
                    rows=db.execute('''SELECT r.id,r.reason,r.created_at,r.status,m.id message_id,m.message,author.member_no message_member_no,author.display_name message_author,u.member_no,u.display_name reporter
                      FROM chat_reports r JOIN chat_messages m ON m.id=r.message_id JOIN users u ON u.id=r.reporter_id JOIN users author ON author.id=m.user_id
                      ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'reviewed' THEN 1 ELSE 2 END,r.created_at DESC LIMIT 100''').fetchall()
                    return self.send_json({'reports':[dict(x) for x in rows]})
                if parts == ['api','stats']:
                    return self.send_json({
                      'approved_notes':db.execute("SELECT COUNT(*) n FROM notes WHERE status='approved'").fetchone()['n'],
                      'students':db.execute("SELECT COUNT(*) n FROM users WHERE role='student'").fetchone()['n'],
                      'exams':db.execute("SELECT COUNT(*) n FROM exams WHERE status='published'").fetchone()['n'],
                      'requests':db.execute("SELECT COUNT(*) n FROM note_requests WHERE status='open'").fetchone()['n']})
                if parts == ['api','notes']:
                    q=parse_qs(urlparse(self.path).query)
                    subject=q.get('subject',[''])[0]; search=q.get('q',[''])[0][:100]
                    rows=db.execute('''SELECT n.*, COALESCE(AVG(f.rating),0) rating, COUNT(f.rating) rating_count
                      FROM notes n LEFT JOIN note_feedback f ON f.note_id=n.id
                      WHERE n.status='approved' AND (?='' OR n.subject=?) AND
                      (?='' OR n.title LIKE ? OR n.chapter LIKE ? OR n.description LIKE ?)
                      GROUP BY n.id ORDER BY n.featured DESC,n.created_at DESC LIMIT 100''',
                      (subject,subject,search,*([f'%{search}%']*3))).fetchall()
                    return self.send_json({'notes':[self.note_public(r) for r in rows]})
                if parts == ['api','my','notes']:
                    u=self.require_user(db)
                    rows=db.execute('SELECT * FROM notes WHERE user_id=? ORDER BY created_at DESC',(u['id'],)).fetchall()
                    return self.send_json({'notes':[self.note_private(r) for r in rows]})
                if parts == ['api','admin','notes']:
                    self.require_staff(db)
                    rows=db.execute("SELECT * FROM notes WHERE status IN ('pending','changes_requested') ORDER BY created_at ASC").fetchall()
                    return self.send_json({'notes':[self.note_private(r) for r in rows]})
                if parts == ['api','admin','reports']:
                    self.require_staff(db)
                    rows=db.execute('''SELECT f.id,f.report_text,f.rating,f.created_at,n.id note_id,n.title note_title,n.subject,n.status note_status,u.display_name reporter
                      FROM note_feedback f JOIN notes n ON n.id=f.note_id LEFT JOIN users u ON u.id=f.user_id
                      WHERE f.report_text<>'' ORDER BY f.created_at DESC LIMIT 100''').fetchall()
                    return self.send_json({'reports':[dict(r) for r in rows]})
                if parts == ['api','feedback']:
                    u=self.require_user(db)
                    rows=db.execute('SELECT id,category,title,details,status,created_at,updated_at FROM student_feedback WHERE user_id=? ORDER BY created_at DESC LIMIT 50',(u['id'],)).fetchall()
                    return self.send_json({'feedback':[dict(r) for r in rows]})
                if parts == ['api','admin','feedback']:
                    self.require_staff(db)
                    rows=db.execute('''SELECT f.id,f.category,f.title,f.details,f.status,f.created_at,f.updated_at,u.display_name,u.email,u.class_name,u.study_group
                      FROM student_feedback f JOIN users u ON u.id=f.user_id ORDER BY CASE f.status WHEN 'open' THEN 0 WHEN 'under_review' THEN 1 ELSE 2 END,f.created_at DESC LIMIT 200''').fetchall()
                    return self.send_json({'feedback':[dict(r) for r in rows]})
                if parts == ['api','exams']:
                    rows=db.execute("SELECT e.*,COUNT(q.id) question_count FROM exams e LEFT JOIN questions q ON q.exam_id=e.id WHERE e.status='published' GROUP BY e.id ORDER BY e.created_at DESC").fetchall()
                    exams=[]
                    for row in rows:
                        item=dict(row); item['attempted']=bool(db.execute('SELECT 1 FROM attempts WHERE exam_id=? AND user_id=?',(row['id'],user['id'])).fetchone()) if user else False
                        exams.append(item)
                    return self.send_json({'exams':exams})
                if len(parts)==3 and parts[:2]==['api','exams']:
                    exam=db.execute("SELECT * FROM exams WHERE id=? AND status='published'",(parts[2],)).fetchone()
                    if not exam: return self.send_json({'error':'Examination not found.'},404)
                    qs=db.execute('SELECT id,prompt,option_a,option_b,option_c,option_d,points FROM questions WHERE exam_id=? ORDER BY position',(exam['id'],)).fetchall()
                    attempted=False
                    if user:
                        attempted=bool(db.execute('SELECT 1 FROM attempts WHERE exam_id=? AND user_id=?',(exam['id'],user['id'])).fetchone())
                    return self.send_json({'exam':dict(exam),'questions':[dict(q) for q in qs],'attempted':attempted})
                if parts == ['api','requests']:
                    rows=db.execute("SELECT id,author_name,title,subject,class_name,details,status,created_at FROM note_requests WHERE status='open' ORDER BY created_at DESC LIMIT 50").fetchall()
                    return self.send_json({'requests':[dict(r) for r in rows]})
                if parts == ['api','leaderboard']:
                    rows=db.execute('''SELECT u.display_name,u.class_name,u.study_group,
                      (SELECT COUNT(*) FROM notes n WHERE n.user_id=u.id AND n.status='approved') contributions,
                      COALESCE((SELECT SUM(a.score) FROM attempts a WHERE a.user_id=u.id),0) exam_points
                      FROM users u ORDER BY exam_points DESC,contributions DESC,u.created_at ASC LIMIT 20''').fetchall()
                    return self.send_json({'leaders':[dict(r) for r in rows]})
                if parts == ['api','activity']:
                    u=self.require_user(db)
                    rows=db.execute('SELECT event_type,description,created_at FROM activity WHERE user_id=? ORDER BY created_at DESC LIMIT 50',(u['id'],)).fetchall()
                    return self.send_json({'activity':[dict(r) for r in rows]})
                if parts == ['api','my','summary']:
                    u=self.require_user(db)
                    totals=db.execute('''SELECT (SELECT COUNT(*) FROM notes WHERE user_id=?) note_count,
                      (SELECT COUNT(*) FROM attempts WHERE user_id=?) exam_count,
                      (SELECT COALESCE(SUM(score),0) FROM attempts WHERE user_id=?) exam_points''',(u['id'],u['id'],u['id'])).fetchone()
                    return self.send_json(dict(totals))
                if len(parts)==3 and parts[:2]==['api','files']:
                    f_id=parts[2]
                    row=db.execute('SELECT * FROM notes WHERE file_id=?',(f_id,)).fetchone()
                    if not row: return self.send_json({'error':'File not found.'},404)
                    if row['status']!='approved' and (not user or (user['id']!=row['user_id'] and user['role'] not in ('moderator','admin'))):
                        return self.send_json({'error':'This file is not public.'},403)
                    if f_id.startswith('http://') or f_id.startswith('https://'):
                        return self._redirect(f_id)
                    path=(UPLOADS/f_id).resolve()
                    if path.parent!=UPLOADS or not path.is_file(): return self.send_json({'error':'File not found.'},404)
                    raw=path.read_bytes()
                    self._headers(200,row['file_mime'] or 'application/octet-stream',{'Content-Length':str(len(raw)),'Content-Disposition':f'inline; filename="note{Path(row["file_name"]).suffix.lower()}"','Cache-Control':'private, max-age=60'})
                    self.wfile.write(raw); return
                if parts == ['api','admin','overview']:
                    self.require_staff(db)
                    return self.send_json({
                      'pending':db.execute("SELECT COUNT(*) n FROM notes WHERE status='pending'").fetchone()['n'],
                      'approved':db.execute("SELECT COUNT(*) n FROM notes WHERE status='approved'").fetchone()['n'],
                      'students':db.execute("SELECT COUNT(*) n FROM users WHERE role='student'").fetchone()['n'],
                      'exams':db.execute("SELECT COUNT(*) n FROM exams").fetchone()['n']})
            self.send_json({'error':'Not found.'},404)
        except AccountRestricted as e: self.send_json({'error':'Account restricted. Submit an appeal to request review.','user':e.user},423)
        except PermissionError as e: self.send_json({'error':str(e)},401 if 'sign in' in str(e) else 403)
        except Exception as e:
            self.log_error('GET error: %s',e); self.send_json({'error':'Something went wrong. Please try again.'},500)

    @staticmethod
    def note_public(r):
        return {'id':r['id'],'author_name':r['author_name'],'title':r['title'],'subject':r['subject'],'paper':r['paper'],'class_name':r['class_name'],'study_group':r['study_group'],'chapter':r['chapter'],'note_type':r['note_type'],'description':r['description'],'content_language':r['content_language'],'file_id':r['file_id'],'file_name':r['file_name'],'file_mime':r['file_mime'],'file_size':r['file_size'],'featured':bool(r['featured']),'rating':round(r['rating'] or 0,1) if 'rating' in r.keys() else 0,'rating_count':r['rating_count'] if 'rating_count' in r.keys() else 0,'created_at':r['created_at']}
    @classmethod
    def note_private(cls,r):
        x=cls.note_public(r); x.update({'status':r['status'],'review_note':r['review_note'],'reviewed_at':r['reviewed_at']}); return x

    def do_POST(self):
        parts=self.path_parts()
        try:
            self.check_origin()
            with connect() as db:
                if parts==['api','auth','register']:
                    body=self.read_json()
                    name=str(body.get('display_name','')).strip(); email=str(body.get('email','')).strip().lower(); password=str(body.get('password',''))
                    cls=body.get('class_name'); group=body.get('study_group'); lang=body.get('language','bn'); board=str(body.get('education_board','')).strip()
                    if not 2<=len(name)<=40: return self.send_json({'error':'Name must be 2–40 characters.'},400)
                    if len(email)>180 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): return self.send_json({'error':'Enter a valid email address.'},400)
                    if len(password)<10 or len(password)>128: return self.send_json({'error':'Password must be at least 10 characters.'},400)
                    if cls not in ('Class XI','Class XII') or group not in ('Science','Humanities','Business Studies') or lang not in ('bn','en'): return self.send_json({'error':'Choose a valid class, group and language.'},400)
                    allowed_boards=('Dhaka','Rajshahi','Cumilla','Jashore','Chattogram','Barishal','Sylhet','Dinajpur','Mymensingh','Madrasah','Technical','Other')
                    if board not in allowed_boards: return self.send_json({'error':'Choose your education board.'},400)
                    if body.get('agree_terms') is not True: return self.send_json({'error':'Please accept the community guidelines and privacy notice.'},400)
                    salt=secrets.token_hex(16); uid=str(uuid.uuid4())
                    try:
                        db.execute('''INSERT INTO users(id,display_name,email,password_hash,password_salt,class_name,study_group,language,role,created_at,education_board,preferred_language,survey_completed,google_sub)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,NULL)''',(uid,name,email,password_digest(password,salt),salt,cls,group,lang,'student',now_iso(),board,lang))
                    except sqlite3.IntegrityError: return self.send_json({'error':'This email is already registered.'},409)
                    # The configured owner email is promoted without exposing a browser-side admin switch.
                    if os.environ.get('HSC_ADMIN_EMAIL','').strip().lower()==email:
                        db.execute("UPDATE users SET role='admin' WHERE id=?",(uid,))
                    db.execute('UPDATE member_counter SET last_value=last_value+1 WHERE id=1')
                    member_no=db.execute('SELECT last_value FROM member_counter WHERE id=1').fetchone()['last_value']
                    db.execute('UPDATE users SET member_no=? WHERE id=?',(member_no,uid))
                    return self.create_session(db,uid,201)
                if parts==['api','profile']:
                    u=self.require_user(db); body=self.read_json()
                    name=str(body.get('display_name',u['display_name'])).strip(); cls=body.get('class_name',u['class_name']); group=body.get('study_group',u['study_group']); board=body.get('education_board',u.get('education_board','')); language=body.get('preferred_language',u.get('preferred_language','bn'))
                    bio=str(body.get('bio',u.get('bio',''))).strip()
                    dob=str(body.get('date_of_birth',u.get('date_of_birth') or '')).strip() or None
                    show_dob=1 if body.get('show_dob') is True else 0
                    if not 2<=len(name)<=40 or len(bio)>280 or cls not in ('Class XI','Class XII') or group not in ('Science','Humanities','Business Studies') or board not in ('Dhaka','Rajshahi','Cumilla','Jashore','Chattogram','Barishal','Sylhet','Dinajpur','Mymensingh','Madrasah','Technical','Other') or language not in ('bn','en'):
                        return self.send_json({'error':'Check your profile information.'},400)
                    if dob:
                        try:
                            birth=datetime.strptime(dob,'%Y-%m-%d').date(); today=datetime.now(timezone.utc).date()
                            age=(today-birth).days/365.2425
                            if birth>today or age<13 or age>100: raise ValueError()
                        except ValueError: return self.send_json({'error':'Enter a valid date of birth. It must be a real date and at least 13 years ago.'},400)
                    avatar_file=u.get('avatar_file')
                    avatar_data=body.get('avatar_data')
                    if avatar_data:
                        if not isinstance(avatar_data,str) or len(avatar_data)>3_000_000 or not avatar_data.startswith('data:image/') or ',' not in avatar_data:
                            return self.send_json({'error':'Choose a PNG, JPG or WebP image under 2 MB.'},400)
                        header,encoded=avatar_data.split(',',1)
                        try: raw=base64.b64decode(encoded,validate=True)
                        except Exception: return self.send_json({'error':'The selected profile picture could not be read.'},400)
                        if len(raw)>2*1024*1024: return self.send_json({'error':'Profile pictures must be 2 MB or smaller.'},400)
                        signatures=[('image/png','png',raw.startswith(b'\x89PNG\r\n\x1a\n')),('image/jpeg','jpg',raw.startswith(b'\xff\xd8\xff')),('image/webp','webp',len(raw)>12 and raw[:4]==b'RIFF' and raw[8:12]==b'WEBP')]
                        chosen=next(((ext) for mime,ext,valid in signatures if valid and header.startswith('data:'+mime+';base64')),None)
                        if not chosen: return self.send_json({'error':'Only genuine PNG, JPG or WebP images are accepted.'},400)
                        new_avatar=uuid.uuid4().hex+'.'+chosen
                        cloud_url=upload_to_cloudinary(raw, new_avatar, 'image/'+chosen)
                        if cloud_url:
                            avatar_file=cloud_url
                        else:
                            (UPLOADS/new_avatar).write_bytes(raw)
                            avatar_file=new_avatar
                        old_avatar=u.get('avatar_file')
                        if old_avatar and old_avatar!=new_avatar:
                            try: (UPLOADS/old_avatar).unlink(missing_ok=True)
                            except OSError: pass
                    db.execute('UPDATE users SET display_name=?,class_name=?,study_group=?,education_board=?,preferred_language=?,language=?,bio=?,date_of_birth=?,show_dob=?,avatar_file=? WHERE id=?',(name,cls,group,board,language,language,bio,dob,show_dob,avatar_file,u['id']))
                    db.execute('UPDATE notes SET author_name=? WHERE user_id=?',(name,u['id']))
                    row=db.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
                    self.log_activity(db,u['id'],'profile_updated','Updated profile details')
                    return self.send_json({'ok':True,'user':safe_user(row)})
                if parts==['api','chat']:
                    u=self.require_user(db); body=self.read_json(); message=str(body.get('message','')).strip(); reply_to=str(body.get('reply_to_id') or '').strip() or None
                    if not 1<=len(message)<=600: return self.send_json({'error':'Message must be 1–600 characters.'},400)
                    last=db.execute('SELECT created_at FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT 1',(u['id'],)).fetchone()
                    if last and (datetime.now(timezone.utc)-datetime.fromisoformat(last['created_at'])).total_seconds()<2:
                        return self.send_json({'error':'Please wait a moment before sending another message.'},429)
                    daily=db.execute('SELECT COUNT(*) n FROM chat_messages WHERE user_id=? AND created_at>?',(u['id'],(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='seconds'))).fetchone()['n']
                    if daily>=50: return self.send_json({'error':'Daily chat limit reached. Please continue tomorrow.'},429)
                    if reply_to and not db.execute('SELECT 1 FROM chat_messages WHERE id=?',(reply_to,)).fetchone(): return self.send_json({'error':'The message you are replying to is no longer available.'},404)
                    digit_map={ord(c):str(i) for i,c in enumerate('০১২৩৪৫৬৭৮৯')}; digit_map.update({ord(c):str(i) for i,c in enumerate('٠١٢٣٤٥٦٧٨٩')})
                    normalized=message.translate(digit_map)
                    phone_pattern=r'(?:\+?88[ .-]*)?01[3-9](?:[ .()\-]*\d){8}'; international_pattern=r'\+\s*[1-9](?:[ .()\-]*\d){7,14}'; email_pattern=r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'
                    contact=bool(re.search(phone_pattern,normalized) or re.search(international_pattern,normalized) or re.search(email_pattern,normalized))
                    def transliterate_bangla(value):
                        mapping={'অ':'o','আ':'a','ই':'i','ঈ':'i','উ':'u','ঊ':'u','ঋ':'ri','এ':'e','ঐ':'oi','ও':'o','ঔ':'ou','া':'a','ি':'i','ী':'i','ু':'u','ূ':'u','ে':'e','ৈ':'oi','ো':'o','ৌ':'ou','ৃ':'ri','্':'','ং':'ng','ঃ':'h','ঁ':'n','ক':'k','খ':'kh','গ':'g','ঘ':'gh','ঙ':'ng','চ':'ch','ছ':'chh','জ':'j','ঝ':'jh','ঞ':'n','ট':'t','ঠ':'th','ড':'d','ঢ':'dh','ণ':'n','ত':'t','থ':'th','দ':'d','ধ':'dh','ন':'n','প':'p','ফ':'ph','ব':'b','ভ':'bh','ম':'m','য':'j','র':'r','ল':'l','শ':'sh','ষ':'sh','স':'s','হ':'h','ড়':'r','ঢ়':'rh','য়':'y'}
                        return ''.join(mapping.get(ch,ch) for ch in value)
                    def compact(value):
                        value=unicodedata.normalize('NFKC',value).casefold()
                        value=re.sub(r'[^a-z]+','',value)
                        return re.sub(r'(.)\1{1,}',r'\1',value)
                    tokens=re.findall(r'[A-Za-z]+|[ঀ-৿]+',unicodedata.normalize('NFKC',normalized).casefold())
                    canonical=[]
                    for token in tokens:
                        roman=transliterate_bangla(token) if re.search(r'[ঀ-৿]',token) else token
                        canonical.append(compact(roman))
                    # Conservative Banglish/Bangla dictionary. Exact token matching avoids flagging harmless substrings.
                    builtins={'fuck','fucking','shit','bitch','bastard','motherfucker','asshole','banchod','benchod','baal','bal','choda','chuda','chud','chod','chudi','chodna','bokachoda','madarchod','madarchuda','khanki','khankir','maghi','haramjada','চুদ','মাদারচোদ','খানকির','হারামজাদা','বাল'}
                    configured={compact(x.strip()) for x in os.environ.get('AJZIO_BLOCKED_WORDS','').split(',') if x.strip()}
                    blocked={compact(x) for x in builtins}|configured
                    aliases={'chudd':'chud','choda':'chod','chuda':'chod','chudaa':'chod','banchod':'banchod','benchod':'banchod','madarchuda':'madarchod','mardarchod':'madarchod','khankir':'khanki','khankee':'khanki','bal':'bal','baal':'bal'}
                    canonical=[aliases.get(w,w) for w in canonical]
                    abusive=any(w in blocked for w in canonical)
                    # Also recognize short split-letter evasion and common two-part spellings.
                    joined=[]
                    for width in (2,3):
                        for i in range(max(0,len(canonical)-width+1)):
                            chunk=canonical[i:i+width]
                            if all(1<=len(x)<=3 for x in chunk): joined.append(''.join(chunk))
                    abusive=abusive or any(aliases.get(w,w) in blocked for w in joined)
                    # A one-character typo is tolerated only for longer high-confidence terms.
                    def near_one(a,b):
                        if abs(len(a)-len(b))>1: return False
                        i=j=errors=0
                        while i<len(a) and j<len(b):
                            if a[i]==b[j]: i+=1; j+=1; continue
                            errors+=1
                            if errors>1: return False
                            if len(a)>len(b): i+=1
                            elif len(b)>len(a): j+=1
                            else: i+=1; j+=1
                        return errors+(len(a)-i)+(len(b)-j)<=1
                    if not abusive:
                        high_confidence={w for w in blocked if len(w)>=6}
                        abusive=any(any(near_one(w,b) for b in high_confidence) for w in canonical if len(w)>=5)
                    squeezed=re.sub(r'(?i)(?<![A-Za-z])([A-Za-z])(?:[ ._*\-]+([A-Za-z])){2,}(?![A-Za-z])',lambda m:re.sub(r'[^A-Za-z]','',m.group(0)).casefold(),normalized)
                    if not abusive and squeezed!=normalized:
                        abusive=any(aliases.get(compact(x),compact(x)) in blocked for x in re.findall(r'[A-Za-z]+',squeezed.casefold()))
                    if contact or abusive:
                        kind='contact_sharing' if contact else 'abusive_language'
                        prior=db.execute("SELECT COUNT(*) n FROM moderation_events WHERE user_id=? AND created_at>?",(u['id'],(datetime.now(timezone.utc)-timedelta(days=7)).isoformat(timespec='seconds'))).fetchone()['n']
                        strike=prior+1; until=None; action='warning'
                        if strike==2: until=(datetime.now(timezone.utc)+timedelta(hours=6)).isoformat(timespec='seconds'); action='suspended_6h'
                        elif strike>=3: until=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(timespec='seconds'); action='suspended_24h'
                        if until:
                            db.execute("UPDATE users SET account_status='suspended',suspended_until=?,restriction_reason=? WHERE id=?",(until,'Community safety rule violation',u['id']))
                            token=secrets.token_urlsafe(36); token_hash=hashlib.sha256(token.encode()).hexdigest(); exp=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat(timespec='seconds')
                            db.execute('INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)',(token_hash,u['id'],exp,now_iso()))
                            secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''; cookie={'Set-Cookie':f'hsc_session={token}; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age=172800'}
                        else: cookie=None
                        db.execute('INSERT INTO moderation_events(id,user_id,violation_type,action,created_at) VALUES(?,?,?,?,?)',(str(uuid.uuid4()),u['id'],kind,action,now_iso()))
                        warning='This message was not posted. Do not share personal contact details or abusive language.'
                        if until: warning+=' Your account is temporarily suspended. You can submit an appeal.'
                        return self.send_json({'ok':False,'blocked':True,'warning':warning,'suspended_until':until},200,cookie)
                    mid=str(uuid.uuid4())
                    tagged=bool(re.search(r'(?i)(?:^|\s)@admin\b',message))
                    db.execute('INSERT INTO chat_messages(id,user_id,message,tagged_admin,created_at,reply_to_id) VALUES(?,?,?,?,?,?)',(mid,u['id'],message,int(tagged),now_iso(),reply_to))
                    # Mentions may target a built-in role or an individual permanent member number.
                    for token in re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z][A-Za-z0-9_-]{1,19}|[0-9]{4,9})',message):
                        value=token.casefold()
                        if value in ('admin','moderator','student'):
                            db.execute('INSERT INTO chat_mentions(id,message_id,target_type,target_value) VALUES(?,?,?,?)',(str(uuid.uuid4()),mid,'role',value))
                        elif value.isdigit() and db.execute("SELECT 1 FROM users WHERE member_no=? AND account_status='active'",(int(value),)).fetchone():
                            db.execute('INSERT INTO chat_mentions(id,message_id,target_type,target_value) VALUES(?,?,?,?)',(str(uuid.uuid4()),mid,'member',value))
                    return self.send_json({'ok':True,'tagged_admin':tagged,'message_id':mid},201)
                if parts==['api','chat','report']:
                    u=self.require_user(db); body=self.read_json(); mid=str(body.get('message_id','')); reason=str(body.get('reason','')).strip()[:300]
                    if len(reason)<3: return self.send_json({'error':'Please give a short report reason.'},400)
                    target=db.execute('SELECT user_id FROM chat_messages WHERE id=?',(mid,)).fetchone()
                    if not target: return self.send_json({'error':'Message not found.'},404)
                    if target['user_id']==u['id']: return self.send_json({'error':'You cannot report your own message.'},400)
                    try: db.execute('INSERT INTO chat_reports(id,message_id,reporter_id,reason,created_at) VALUES(?,?,?,?,?)',(str(uuid.uuid4()),mid,u['id'],reason,now_iso()))
                    except sqlite3.IntegrityError: return self.send_json({'error':'You have already reported this message.'},409)
                    return self.send_json({'ok':True},201)
                if parts==['api','appeal']:
                    u=self.current_user(db)
                    if not u: return self.send_json({'error':'Sign in to submit an appeal.'},401)
                    restricted=u.get('account_status')=='locked' or (u.get('account_status')=='suspended' and u.get('suspended_until') and u['suspended_until']>now_iso())
                    if not restricted: return self.send_json({'error':'Your account is not restricted.'},409)
                    body=self.read_json(); details=str(body.get('details','')).strip()
                    if not 30<=len(details)<=1500: return self.send_json({'error':'Please explain your appeal in 30–1500 characters.'},400)
                    existing=db.execute("SELECT id FROM account_appeals WHERE user_id=? AND status='pending'",(u['id'],)).fetchone()
                    if existing: return self.send_json({'error':'You already have a pending appeal.'},409)
                    aid=str(uuid.uuid4()); stamp=now_iso()
                    db.execute('INSERT INTO account_appeals(id,user_id,details,created_at,updated_at) VALUES(?,?,?,?,?)',(aid,u['id'],details,stamp,stamp))
                    return self.send_json({'ok':True,'id':aid},201)
                if parts==['api','admin','users','action']:
                    admin=self.require_admin(db); body=self.read_json(); target=str(body.get('user_id','')); action=body.get('action'); reason=str(body.get('reason','')).strip()[:300]
                    target_row=db.execute('SELECT id,role,account_status FROM users WHERE id=?',(target,)).fetchone()
                    if not target_row: return self.send_json({'error':'Student not found.'},404)
                    if target_row['id']==admin['id'] or target_row['role']=='admin': return self.send_json({'error':'Administrator accounts cannot be restricted from this panel.'},403)
                    if action=='suspend':
                        hours=body.get('hours',6)
                        if type(hours) is not int or hours not in (6,24): return self.send_json({'error':'Choose a 6-hour or 24-hour suspension.'},400)
                        until=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat(timespec='seconds')
                        db.execute("UPDATE users SET account_status='suspended',suspended_until=?,restriction_reason=? WHERE id=?",(until,reason or 'Temporary community restriction',target))
                    elif action=='lock':
                        db.execute("UPDATE users SET account_status='locked',suspended_until=NULL,restriction_reason=? WHERE id=?",(reason or 'Account locked by administrator',target))
                    elif action=='unlock':
                        db.execute("UPDATE users SET account_status='active',suspended_until=NULL,restriction_reason='' WHERE id=?",(target,))
                    else: return self.send_json({'error':'Invalid account action.'},400)
                    self.log_admin_action(db,admin['id'],action,'user',target,reason)
                    return self.send_json({'ok':True})
                if parts==['api','admin','chat-reports','action']:
                    admin=self.require_admin(db); body=self.read_json(); rid=str(body.get('report_id','')); decision=body.get('decision')
                    report=db.execute('SELECT id,message_id,status FROM chat_reports WHERE id=?',(rid,)).fetchone()
                    if not report: return self.send_json({'error':'Report not found.'},404)
                    if decision not in ('reviewed','dismissed'): return self.send_json({'error':'Choose a valid report action.'},400)
                    db.execute('UPDATE chat_reports SET status=?,reviewed_at=? WHERE id=?',(decision,now_iso(),rid))
                    self.log_admin_action(db,admin['id'],'chat_report_'+decision,'chat_report',rid,'Reviewed community report')
                    return self.send_json({'ok':True,'status':decision})
                if parts==['api','admin','appeals','action']:
                    admin=self.require_admin(db); body=self.read_json(); aid=str(body.get('appeal_id','')); decision=body.get('decision'); response=str(body.get('response','')).strip()[:500]
                    appeal=db.execute("SELECT * FROM account_appeals WHERE id=? AND status='pending'",(aid,)).fetchone()
                    if not appeal: return self.send_json({'error':'Pending appeal not found.'},404)
                    if decision not in ('approve','decline'): return self.send_json({'error':'Choose approve or decline.'},400)
                    stamp=now_iso(); state='approved' if decision=='approve' else 'declined'
                    db.execute('UPDATE account_appeals SET status=?,admin_response=?,updated_at=?,reviewed_by=? WHERE id=?',(state,response,stamp,admin['id'],aid))
                    if decision=='approve': db.execute("UPDATE users SET account_status='active',suspended_until=NULL,restriction_reason='' WHERE id=?",(appeal['user_id'],))
                    self.log_admin_action(db,admin['id'],'appeal_'+state,'appeal',aid,response)
                    return self.send_json({'ok':True,'status':state})
                if parts==['api','admin','chat','delete']:
                    admin=self.require_admin(db); body=self.read_json(); mid=str(body.get('message_id',''))
                    cur=db.execute('DELETE FROM chat_messages WHERE id=?',(mid,))
                    if not cur.rowcount: return self.send_json({'error':'Message not found.'},404)
                    self.log_admin_action(db,admin['id'],'delete_message','chat_message',mid,'Removed by administrator')
                    return self.send_json({'ok':True})
                if parts==['api','auth','login']:
                    ip=self.client_address[0]
                    with AUTH_LOCK:
                        bucket=AUTH_FAILURES.get(ip,[]); bucket=[t for t in bucket if time.time()-t<900]
                        if len(bucket)>=10: return self.send_json({'error':'Too many sign-in attempts. Please try again in 15 minutes.'},429)
                    body=self.read_json(); email=str(body.get('email','')).strip().lower(); password=str(body.get('password',''))
                    if len(password)>128: return self.send_json({'error':'Email or password is incorrect.'},401)
                    row=db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
                    if not row or not hmac.compare_digest(password_digest(password,row['password_salt'] if row else '00'*16),row['password_hash'] if row else '0'*64):
                        with AUTH_LOCK:
                            AUTH_FAILURES[ip]=[t for t in AUTH_FAILURES.get(ip,[]) if time.time()-t<900]+[time.time()]
                        return self.send_json({'error':'Email or password is incorrect.'},401)
                    with AUTH_LOCK: AUTH_FAILURES.pop(ip,None)
                    return self.create_session(db,row['id'])
                if parts==['api','auth','logout']:
                    self.read_body()
                    cookie=SimpleCookie(self.headers.get('Cookie','')); m=cookie.get('hsc_session')
                    if m: db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(m.value.encode()).hexdigest(),))
                    secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''
                    return self.send_json({'ok':True},extra={'Set-Cookie':f'hsc_session=; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age=0'})
                if parts==['api','auth','google','complete']:
                    body=self.read_json(); c=SimpleCookie(self.headers.get('Cookie','')); m=c.get('hsc_google_pending')
                    if not m: return self.send_json({'error':'Google sign-in session expired. Please try again.'},401)
                    pending_hash=hashlib.sha256(m.value.encode()).hexdigest()
                    pending=db.execute('SELECT * FROM google_oauth_pending WHERE token_hash=? AND expires_at>?',(pending_hash,now_iso())).fetchone()
                    if not pending: return self.send_json({'error':'Google sign-in session expired. Please try again.'},401)
                    cls=body.get('class_name'); group=body.get('study_group'); lang=body.get('preferred_language','bn'); board=body.get('education_board','')
                    boards=('Dhaka','Rajshahi','Cumilla','Jashore','Chattogram','Barishal','Sylhet','Dinajpur','Mymensingh','Madrasah','Technical','Other')
                    if cls not in ('Class XI','Class XII') or group not in ('Science','Humanities','Business Studies') or lang not in ('bn','en') or board not in boards or body.get('agree_terms') is not True:
                        return self.send_json({'error':'Complete your student profile and accept the community guidelines.'},400)
                    uid=str(uuid.uuid4()); salt=secrets.token_hex(16); unusable_password=secrets.token_urlsafe(48)
                    db.execute('''INSERT INTO users(id,display_name,email,password_hash,password_salt,class_name,study_group,language,role,created_at,education_board,preferred_language,survey_completed,google_sub)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)''',(uid,pending['display_name'],pending['email'],password_digest(unusable_password,salt),salt,cls,group,lang,'student',now_iso(),board,lang,pending['google_sub']))
                    db.execute('UPDATE member_counter SET last_value=last_value+1 WHERE id=1')
                    member_no=db.execute('SELECT last_value FROM member_counter WHERE id=1').fetchone()['last_value']
                    db.execute('UPDATE users SET member_no=? WHERE id=?',(member_no,uid))
                    db.execute('DELETE FROM google_oauth_pending WHERE token_hash=?',(pending_hash,))
                    self.log_activity(db,uid,'account_created','Created account with Google')
                    return self.create_session(db,uid,201)
                if parts==['api','onboarding']:
                    u=self.require_user(db); body=self.read_json()
                    goals=('board_exam','concepts','practice','resources','self_study')
                    challenges=('quality_notes','hard_topics','exam_fear','time_management','study_plan','other')
                    goal=body.get('learning_goal'); challenge=body.get('biggest_challenge'); features=body.get('preferred_features',[]); extra=str(body.get('improvement_text','')).strip()
                    if goal not in goals or challenge not in challenges: return self.send_json({'error':'Please answer both required questions.'},400)
                    if not isinstance(features,list) or len(features)>8 or any(not isinstance(x,str) or len(x)>40 for x in features): return self.send_json({'error':'Invalid feature preferences.'},400)
                    if len(extra)>1000: return self.send_json({'error':'Your suggestion is too long.'},400)
                    now=now_iso()
                    db.execute('''INSERT INTO onboarding_responses(user_id,learning_goal,biggest_challenge,preferred_features,improvement_text,created_at,updated_at)
                      VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET learning_goal=excluded.learning_goal,biggest_challenge=excluded.biggest_challenge,preferred_features=excluded.preferred_features,improvement_text=excluded.improvement_text,updated_at=excluded.updated_at''',(u['id'],goal,challenge,json.dumps(features,ensure_ascii=False),extra,now,now))
                    db.execute('UPDATE users SET survey_completed=1 WHERE id=?',(u['id'],))
                    self.log_activity(db,u['id'],'onboarding_completed','Completed welcome questions')
                    row=db.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
                    return self.send_json({'ok':True,'user':safe_user(row)})
                if parts==['api','feedback']:
                    u=self.require_user(db); body=self.read_json(); category=body.get('category'); title=str(body.get('title','')).strip(); details=str(body.get('details','')).strip()
                    if category not in ('feature','bug','content','other'): return self.send_json({'error':'Choose a valid feedback category.'},400)
                    if not 5<=len(title)<=100 or not 15<=len(details)<=2000: return self.send_json({'error':'Title must be 5–100 characters and details 15–2000 characters.'},400)
                    since=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='seconds')
                    if db.execute('SELECT COUNT(*) n FROM student_feedback WHERE user_id=? AND created_at>?',(u['id'],since)).fetchone()['n']>=5: return self.send_json({'error':'You can send up to five suggestions per day.'},429)
                    fid=str(uuid.uuid4()); now=now_iso()
                    db.execute('INSERT INTO student_feedback(id,user_id,category,title,details,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(fid,u['id'],category,title,details,'open',now,now))
                    self.log_activity(db,u['id'],'feedback_submitted','Submitted feedback: '+title)
                    return self.send_json({'ok':True,'id':fid},201)
                if parts==['api','admin','feedback']:
                    staff=self.require_staff(db); body=self.read_json(); fid=body.get('feedback_id'); status=body.get('status')
                    if status not in ('under_review','done','closed'): return self.send_json({'error':'Invalid feedback status.'},400)
                    cur=db.execute('UPDATE student_feedback SET status=?,updated_at=? WHERE id=?',(status,now_iso(),fid))
                    if not cur.rowcount: return self.send_json({'error':'Feedback item not found.'},404)
                    return self.send_json({'ok':True})
                if parts==['api','notes']:
                    u=self.require_user(db); fields, file_data, file_name, mime=self.read_multipart()
                    required=['title','subject','class_name','study_group','chapter','note_type','content_language']
                    if any(not fields.get(k,'').strip() for k in required): return self.send_json({'error':'Please complete all required fields.'},400)
                    if fields.get('permission_agree')!='yes': return self.send_json({'error':'Confirm that you own this note or have permission to share it.'},400)
                    if fields['class_name']!=u['class_name'] or fields['study_group']!=u['study_group']:
                        return self.send_json({'error':'Your note must match the class and group on your account.'},400)
                    if fields['subject'] not in SUBJECTS: return self.send_json({'error':'Choose a valid subject.'},400)
                    if fields['content_language'] not in ('bn','en'): return self.send_json({'error':'Choose a valid note language.'},400)
                    if len(fields['title'])>120 or len(fields['chapter'])>100 or len(fields.get('description',''))>500: return self.send_json({'error':'Some fields are too long.'},400)
                    fid=None
                    if file_data is None:
                        return self.send_json({'error':'Attach a PDF, JPG or PNG file with your note.'},400)
                    if file_data is not None:
                        if len(file_data)>MAX_FILE: return self.send_json({'error':'File must be 10 MB or smaller.'},400)
                        allowed={'application/pdf':'.pdf','image/jpeg':'.jpg','image/png':'.png'}
                        ext=allowed.get(mime)
                        valid=(mime=='application/pdf' and file_data.startswith(b'%PDF-')) or (mime=='image/jpeg' and file_data.startswith(b'\xff\xd8\xff')) or (mime=='image/png' and file_data.startswith(b'\x89PNG\r\n\x1a\n'))
                        if not ext or not valid: return self.send_json({'error':'Only valid PDF, JPG or PNG files are accepted.'},400)
                        fid=uuid.uuid4().hex+ext
                        cloud_url=upload_to_cloudinary(file_data, Path(file_name or fid).name, mime)
                        if cloud_url: fid=cloud_url
                        else: (UPLOADS/fid).write_bytes(file_data)
                    nid=str(uuid.uuid4()); created=now_iso()
                    db.execute('''INSERT INTO notes(id,user_id,author_name,title,subject,paper,class_name,study_group,chapter,note_type,description,content_language,file_id,file_name,file_mime,file_size,status,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(nid,u['id'],u['display_name'],fields['title'].strip(),fields['subject'],fields.get('paper','').strip()[:40],fields['class_name'],fields['study_group'],fields['chapter'].strip(),fields['note_type'],fields.get('description','').strip(),fields['content_language'],fid,Path(file_name).name[:180] if file_name else None,mime if fid else None,len(file_data) if file_data else None,'pending',created))
                    db.execute('INSERT INTO note_permissions(id,note_id,user_id,permission_text_version,granted_at) VALUES(?,?,?,?,?)',(str(uuid.uuid4()),nid,u['id'],'hsc-note-share-v1',created))
                    self.log_activity(db,u['id'],'note_submitted',f"Submitted: {fields['title'].strip()}")
                    return self.send_json({'ok':True,'id':nid,'status':'pending'},201)
                if len(parts)==3 and parts[:2]==['api','notes'] and parts[2] and self.command=='POST':
                    u=self.require_user(db); body=self.read_json(); note=db.execute("SELECT id FROM notes WHERE id=? AND status='approved'",(parts[2],)).fetchone()
                    if not note: return self.send_json({'error':'Note not found.'},404)
                    rating=body.get('rating'); report=str(body.get('report_text','')).strip()[:500]
                    if rating is not None and (not isinstance(rating,int) or not 1<=rating<=5): return self.send_json({'error':'Rating must be from 1 to 5.'},400)
                    db.execute('''INSERT INTO note_feedback(id,note_id,user_id,rating,report_text,created_at) VALUES(?,?,?,?,?,?)
                      ON CONFLICT(note_id,user_id) DO UPDATE SET rating=excluded.rating,report_text=excluded.report_text,created_at=excluded.created_at''',(str(uuid.uuid4()),parts[2],u['id'],rating,report,now_iso()))
                    return self.send_json({'ok':True})
                if len(parts)==4 and parts[:2]==['api','notes'] and parts[3]=='revise':
                    u=self.require_user(db); fields,file_data,file_name,mime=self.read_multipart()
                    current=db.execute("SELECT * FROM notes WHERE id=? AND user_id=? AND status='changes_requested'",(parts[2],u['id'])).fetchone()
                    if not current: return self.send_json({'error':'This note is not awaiting a revision.'},404)
                    required=['title','subject','class_name','study_group','chapter','note_type','content_language']
                    if any(not fields.get(k,'').strip() for k in required): return self.send_json({'error':'Please complete all required fields.'},400)
                    if fields.get('permission_agree')!='yes': return self.send_json({'error':'Confirm that you own this note or have permission to share it.'},400)
                    if fields['class_name']!=u['class_name'] or fields['study_group']!=u['study_group'] or fields['subject'] not in SUBJECTS or fields['content_language'] not in ('bn','en'):
                        return self.send_json({'error':'Check the selected class, group, subject and language.'},400)
                    if len(fields['title'])>120 or len(fields['chapter'])>100 or len(fields.get('description',''))>500: return self.send_json({'error':'Some fields are too long.'},400)
                    new_fid=current['file_id']; new_name=current['file_name']; new_mime=current['file_mime']; new_size=current['file_size']; old_to_remove=None
                    if file_data is not None:
                        if len(file_data)>MAX_FILE: return self.send_json({'error':'File must be 10 MB or smaller.'},400)
                        allowed={'application/pdf':'.pdf','image/jpeg':'.jpg','image/png':'.png'}; ext=allowed.get(mime)
                        valid=(mime=='application/pdf' and file_data.startswith(b'%PDF-')) or (mime=='image/jpeg' and file_data.startswith(b'\xff\xd8\xff')) or (mime=='image/png' and file_data.startswith(b'\x89PNG\r\n\x1a\n'))
                        if not ext or not valid: return self.send_json({'error':'Only valid PDF, JPG or PNG files are accepted.'},400)
                        old_to_remove=current['file_id']; new_fid=uuid.uuid4().hex+ext; _curl=upload_to_cloudinary(file_data, Path(file_name or new_fid).name, mime); (UPLOADS/new_fid).write_bytes(file_data) if not _curl else None; new_fid=_curl or new_fid; new_name=Path(file_name).name[:180]; new_mime=mime; new_size=len(file_data)
                    db.execute('''UPDATE notes SET title=?,subject=?,paper=?,chapter=?,note_type=?,description=?,content_language=?,file_id=?,file_name=?,file_mime=?,file_size=?,status='pending',reviewer_id=NULL,review_note='',reviewed_at=NULL,created_at=? WHERE id=?''',
                      (fields['title'].strip(),fields['subject'],fields.get('paper','').strip()[:40],fields['chapter'].strip(),fields['note_type'],fields.get('description','').strip(),fields['content_language'],new_fid,new_name,new_mime,new_size,now_iso(),parts[2]))
                    # A replaced file becomes unreferenced; startup cleanup removes it safely after commit.
                    db.execute('INSERT INTO note_permissions(id,note_id,user_id,permission_text_version,granted_at) VALUES(?,?,?,?,?)',(str(uuid.uuid4()),parts[2],u['id'],'hsc-note-share-v1',now_iso()))
                    self.log_activity(db,u['id'],'note_revised',f"Resubmitted: {fields['title'].strip()}")
                    return self.send_json({'ok':True,'status':'pending'})
                if parts==['api','requests']:
                    u=self.require_user(db); body=self.read_json(); title=str(body.get('title','')).strip(); subject=body.get('subject'); cls=body.get('class_name')
                    if not 5<=len(title)<=120 or subject not in SUBJECTS or cls not in ('Class XI','Class XII'): return self.send_json({'error':'Check the request title, subject and class.'},400)
                    rid=str(uuid.uuid4()); db.execute('INSERT INTO note_requests(id,user_id,author_name,title,subject,class_name,details,created_at) VALUES(?,?,?,?,?,?,?,?)',(rid,u['id'],u['display_name'],title,subject,cls,str(body.get('details',''))[:500],now_iso()))
                    self.log_activity(db,u['id'],'note_request',f'Requested: {title}')
                    return self.send_json({'ok':True,'id':rid},201)
                if len(parts)==4 and parts[:2]==['api','exams'] and parts[3]=='start':
                    self.read_body()
                    u=self.require_user(db); exam=db.execute("SELECT * FROM exams WHERE id=? AND status='published'",(parts[2],)).fetchone()
                    if not exam: return self.send_json({'error':'Examination not found.'},404)
                    if exam['class_name']!='All HSC' and u['class_name']!=exam['class_name']:
                        return self.send_json({'error':'This examination is for a different class.'},403)
                    if exam['study_group']!='All' and u['study_group']!=exam['study_group']:
                        return self.send_json({'error':'This examination is for a different group.'},403)
                    if exam['opens_at'] and now_iso()<exam['opens_at']: return self.send_json({'error':'This examination has not opened yet.'},403)
                    if exam['closes_at'] and now_iso()>exam['closes_at']: return self.send_json({'error':'This examination has closed.'},403)
                    if db.execute('SELECT 1 FROM attempts WHERE exam_id=? AND user_id=?',(exam['id'],u['id'])).fetchone(): return self.send_json({'error':'You have already submitted this examination.'},409)
                    started=now_iso(); expires=(datetime.now(timezone.utc)+timedelta(minutes=exam['duration_minutes'],seconds=30)).isoformat(timespec='seconds')
                    old=db.execute('SELECT * FROM exam_sessions WHERE exam_id=? AND user_id=? AND used=0 AND expires_at>? ORDER BY started_at DESC LIMIT 1',(exam['id'],u['id'],started)).fetchone()
                    if old: session_id=old['id']; started=old['started_at']; expires=old['expires_at']
                    else:
                        session_id=str(uuid.uuid4())
                        db.execute('INSERT INTO exam_sessions(id,exam_id,user_id,started_at,expires_at) VALUES(?,?,?,?,?)',(session_id,exam['id'],u['id'],started,expires))
                    qs=db.execute('SELECT id,prompt,option_a,option_b,option_c,option_d,points FROM questions WHERE exam_id=? ORDER BY position',(exam['id'],)).fetchall()
                    return self.send_json({'session_id':session_id,'started_at':started,'expires_at':expires,'exam':dict(exam),'questions':[dict(q) for q in qs]})
                if len(parts)==3 and parts[:2]==['api','exams'] and parts[2] and self.command=='POST':
                    u=self.require_user(db); body=self.read_json(); exam=db.execute("SELECT * FROM exams WHERE id=? AND status='published'",(parts[2],)).fetchone()
                    if not exam: return self.send_json({'error':'Examination not found.'},404)
                    if (exam['class_name']!='All HSC' and u['class_name']!=exam['class_name']) or (exam['study_group']!='All' and u['study_group']!=exam['study_group']):
                        return self.send_json({'error':'This examination is for a different class or group.'},403)
                    if exam['opens_at'] and now_iso()<exam['opens_at']: return self.send_json({'error':'This examination has not opened yet.'},403)
                    if exam['closes_at'] and now_iso()>exam['closes_at']: return self.send_json({'error':'This examination has closed.'},403)
                    if db.execute('SELECT 1 FROM attempts WHERE exam_id=? AND user_id=?',(exam['id'],u['id'])).fetchone(): return self.send_json({'error':'You have already submitted this examination.'},409)
                    session_id=str(body.get('session_id') or '')
                    session=db.execute('SELECT * FROM exam_sessions WHERE id=? AND exam_id=? AND user_id=? AND used=0',(session_id,exam['id'],u['id'])).fetchone()
                    if not session: return self.send_json({'error':'Exam session is invalid or already used. Start the exam again.'},403)
                    if now_iso()>session['expires_at']: return self.send_json({'error':'The examination time has expired.'},403)
                    answers=body.get('answers')
                    if not isinstance(answers,dict): return self.send_json({'error':'Invalid answers.'},400)
                    qs=db.execute('SELECT * FROM questions WHERE exam_id=? ORDER BY position',(exam['id'],)).fetchall()
                    if not qs or set(answers.keys())!={q['id'] for q in qs}: return self.send_json({'error':'Answer every question before submitting.'},400)
                    score=0; max_score=sum(q['points'] for q in qs); evaluated=[]
                    for q in qs:
                        choice=answers.get(q['id'],-1)
                        if not isinstance(choice,int) or choice not in (-1,0,1,2,3): return self.send_json({'error':'Invalid answer selection.'},400)
                        correct=choice==q['correct_option']; earned=q['points'] if correct else 0; score+=earned
                        evaluated.append((q,choice,correct,earned))
                    aid=str(uuid.uuid4()); submitted=now_iso(); started=session['started_at']
                    db.execute('INSERT INTO attempts(id,exam_id,user_id,score,max_score,started_at,submitted_at) VALUES(?,?,?,?,?,?,?)',(aid,exam['id'],u['id'],score,max_score,started,submitted))
                    db.execute('UPDATE exam_sessions SET used=1 WHERE id=?',(session_id,))
                    result=[]
                    for q,choice,correct,earned in evaluated:
                        db.execute('INSERT INTO attempt_answers(id,attempt_id,question_id,selected_option,is_correct,points_earned) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),aid,q['id'],choice,int(correct),earned))
                        result.append({'question_id':q['id'],'prompt':q['prompt'],'options':[q['option_a'],q['option_b'],q['option_c'],q['option_d']],'selected_option':choice,'correct_option':q['correct_option'],'is_correct':correct,'points_earned':earned,'points':q['points'],'explanation':q['explanation']})
                    self.log_activity(db,u['id'],'exam_submitted',f"Completed {exam['title']}: {score}/{max_score}")
                    return self.send_json({'ok':True,'attempt_id':aid,'score':score,'max_score':max_score,'answers':result,'submitted_at':submitted},201)
                if parts==['api','admin','notes']:
                    u=self.require_staff(db); body=self.read_json(); nid=str(body.get('note_id','')); status=body.get('status'); reason=str(body.get('review_note','')).strip()[:500]
                    if status not in ('approved','rejected','changes_requested'): return self.send_json({'error':'Invalid review decision.'},400)
                    row=db.execute("SELECT user_id,title FROM notes WHERE id=? AND status IN ('pending','changes_requested')",(nid,)).fetchone()
                    if not row: return self.send_json({'error':'This note is no longer awaiting review.'},409)
                    if row['user_id']==u['id']:
                        return self.send_json({'error':'You cannot review your own note. Ask another moderator to review it.'},403)
                    cur=db.execute("UPDATE notes SET status=?,reviewer_id=?,review_note=?,reviewed_at=? WHERE id=? AND status IN ('pending','changes_requested')",(status,u['id'],reason,now_iso(),nid))
                    if not cur.rowcount: return self.send_json({'error':'This note is no longer awaiting review.'},409)
                    if row['user_id']: self.log_activity(db,row['user_id'],'note_reviewed',f"Your note '{row['title']}' was {status.replace('_',' ')}.")
                    return self.send_json({'ok':True,'status':status})
                if parts==['api','admin','exams']:
                    u=self.require_staff(db); body=self.read_json()
                    title=str(body.get('title','')).strip(); subject=body.get('subject'); cls=body.get('class_name'); group=body.get('study_group'); chapter=str(body.get('chapter','')).strip(); duration=body.get('duration_minutes'); questions=body.get('questions')
                    if not 1<=len(title)<=140 or subject not in SUBJECTS or cls not in ('Class XI','Class XII') or group not in ('Science','Humanities','Business Studies') or not 1<=len(chapter)<=120 or not isinstance(duration,int) or isinstance(duration,bool) or not 1<=duration<=180: return self.send_json({'error':'Check examination information.'},400)
                    if not isinstance(questions,list) or not 1<=len(questions)<=50: return self.send_json({'error':'Add between 1 and 50 questions.'},400)
                    opens_at=body.get('opens_at') or None; closes_at=body.get('closes_at') or None
                    try:
                        open_dt=datetime.fromisoformat(opens_at.replace('Z','+00:00')) if opens_at else None
                        close_dt=datetime.fromisoformat(closes_at.replace('Z','+00:00')) if closes_at else None
                    except ValueError: return self.send_json({'error':'Enter valid examination opening and closing dates.'},400)
                    if open_dt and close_dt and close_dt<=open_dt: return self.send_json({'error':'The closing time must be after the opening time.'},400)
                    eid=str(uuid.uuid4()); created=now_iso()
                    db.execute('INSERT INTO exams(id,title,subject,class_name,study_group,chapter,description,duration_minutes,opens_at,closes_at,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(eid,title,subject,cls,group,chapter,str(body.get('description',''))[:500],duration,opens_at,closes_at,'published',u['id'],created))
                    for i,q in enumerate(questions):
                        if not isinstance(q,dict): raise ValueError('Each question must be an object.')
                        opts=q.get('options'); prompt=str(q.get('prompt','')).strip(); correct=q.get('correct_option'); points=q.get('points',1)
                        if (not isinstance(opts,list) or len(opts)!=4 or not 1<=len(prompt)<=1000
                            or any(not isinstance(x,str) or not x.strip() or len(x)>300 for x in opts)
                            or type(correct) is not int or correct not in range(4)
                            or type(points) is not int or not 1<=points<=100):
                            raise ValueError('Each question needs a prompt, four options, a correct option (0–3), and points from 1 to 100.')
                        db.execute('INSERT INTO questions(id,exam_id,prompt,option_a,option_b,option_c,option_d,correct_option,explanation,points,position) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),eid,prompt,*opts,correct,str(q.get('explanation',''))[:1000],points,i))
                    return self.send_json({'ok':True,'id':eid,'status':'published'},201)
            self.send_json({'error':'Not found.'},404)
        except AccountRestricted as e: self.send_json({'error':'Account restricted. Submit an appeal to request review.','user':e.user},423)
        except PermissionError as e: self.send_json({'error':str(e)},401 if 'sign in' in str(e) else 403)
        except sqlite3.IntegrityError as e:
            message='An account with this email already exists.' if 'users.email' in str(e) else 'This action conflicts with an existing record.'
            self.send_json({'error':message},409)
        except ValueError as e: self.send_json({'error':str(e)},400)
        except Exception as e:
            self.log_error('POST error: %s',e); self.send_json({'error':'Something went wrong. Please try again.'},500)

    def create_session(self, db, uid, status=200):
        token=secrets.token_urlsafe(36); hashed=hashlib.sha256(token.encode()).hexdigest(); exp=(datetime.now(timezone.utc)+timedelta(days=SESSION_DAYS)).isoformat(timespec='seconds')
        db.execute('INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)',(hashed,uid,exp,now_iso()))
        row=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''
        return self.send_json({'ok':True,'user':safe_user(row)},status,{'Set-Cookie':f'hsc_session={token}; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age={SESSION_DAYS*86400}'})

    def log_activity(self,db,uid,kind,description):
        db.execute('INSERT INTO activity(id,user_id,event_type,description,created_at) VALUES(?,?,?,?,?)',(str(uuid.uuid4()),uid,kind,description,now_iso()))

    def log_admin_action(self,db,admin_id,action,target_type,target_id,details=''):
        db.execute('INSERT INTO admin_audit_log(id,admin_id,action,target_type,target_id,details,created_at) VALUES(?,?,?,?,?,?,?)',(str(uuid.uuid4()),admin_id,action,target_type,target_id,str(details)[:500],now_iso()))

    def read_multipart(self):
        raw=self.read_body(); ctype=self.headers.get('Content-Type','')
        if 'multipart/form-data' not in ctype: raise ValueError('A multipart form is required for note uploads.')
        msg=BytesParser(policy=policy.default).parsebytes(b'Content-Type: '+ctype.encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+raw)
        fields={}; file_data=None; file_name=None; file_mime=None
        for part in msg.iter_parts():
            name=part.get_param('name',header='content-disposition')
            if not name: continue
            payload=part.get_payload(decode=True) or b''
            filename=part.get_filename()
            if filename:
                if payload:
                    file_data=payload; file_name=filename; file_mime=part.get_content_type()
            else:
                fields[str(name)]=payload.decode(part.get_content_charset() or 'utf-8','replace')
        return fields,file_data,file_name,file_mime

    def do_DELETE(self):
        try:
            self.check_origin(); parts=self.path_parts()
            if parts!=['api','account']: return self.send_json({'error':'Not found.'},404)
            with connect() as db:
                u=self.require_user(db)
                if u.get('role')=='admin':
                    return self.send_json({'error':'Administrator accounts cannot be deleted from the profile. Transfer administrator access first.'},403)
                # Remove account-owned note binaries before cascading database rows.
                files=db.execute('SELECT file_id FROM notes WHERE user_id=? AND file_id IS NOT NULL',(u['id'],)).fetchall()
                for f in files:
                    p=UPLOADS/f['file_id']
                    if p.exists(): p.unlink()
                db.execute('DELETE FROM notes WHERE user_id=?',(u['id'],))
                db.execute('DELETE FROM users WHERE id=?',(u['id'],))
                secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''
                return self.send_json({'ok':True},extra={'Set-Cookie':f'hsc_session=; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age=0'})
        except AccountRestricted as e: self.send_json({'error':'Account restricted. Submit an appeal.','user':e.user},423)
        except PermissionError as e: self.send_json({'error':str(e)},401)
        except Exception as e: self.log_error('DELETE error: %s',e); self.send_json({'error':'Could not delete account.'},500)

    def do_PATCH(self):
        # Review endpoint is intentionally POST-only to keep the mutation surface small.
        self.send_json({'error':'Method not allowed.'},405)

    def serve_static(self, parts):
        rel='/'.join(parts) if parts else 'index.html'
        if '..' in parts or rel.startswith('data/') or rel.startswith('server.py') or any(p.startswith('.') for p in parts):
            self.send_json({'error':'Not found.'},404); return
        path=(ROOT/rel).resolve()
        if ROOT not in path.parents and path!=ROOT/'index.html': self.send_json({'error':'Not found.'},404); return
        if path.is_dir(): path=path/'index.html'
        if not path.is_file(): self.send_json({'error':'Not found.'},404); return
        mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        raw=path.read_bytes(); self._headers(200,mime+('; charset=utf-8' if mime.startswith(('text/','application/javascript')) else ''),{'Content-Length':str(len(raw))})
        self.wfile.write(raw)

    def do_PUT(self): self.send_json({'error':'Method not allowed.'},405)


def main():
    parser=argparse.ArgumentParser(description='Run AJZIO Education — free HSC learning community')
    parser.add_argument('--host',default=HOST); parser.add_argument('--port',type=int,default=PORT)
    parser.add_argument('--promote',metavar='EMAIL',help='Promote an already registered account to admin')
    parser.add_argument('--make-moderator',metavar='EMAIL',help='Give an already registered account moderator access')
    args=parser.parse_args(); init_db()
    if args.promote or args.make_moderator:
        email=(args.promote or args.make_moderator).strip().lower()
        role='admin' if args.promote else 'moderator'
        with connect() as db:
            cur=db.execute('UPDATE users SET role=? WHERE email=?',(role,email))
            if cur.rowcount: print(f"{role.title()} role granted to {email}")
            else: print('No matching account. Register that email first.'); sys.exit(1)
        return
    httpd=ThreadingHTTPServer((args.host,args.port),AppHandler)
    print(f'AJZIO Education is running at http://{args.host}:{args.port}')
    print(f'Database: {DB}')
    if args.host in ('0.0.0.0','::'):
        print('Network binding enabled. Put this service behind HTTPS before public deployment.')
    try: httpd.serve_forever()
    except KeyboardInterrupt: print('\nStopping AJZIO Education...')
    finally: httpd.server_close()

if __name__=='__main__': main()
