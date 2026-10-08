"""Small transactional journal; video and biometric images are never stored."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=.2)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS answers (question TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, utc TEXT NOT NULL, monotonic REAL NOT NULL,
                kind TEXT NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL
            );
        ''')

    def answers(self):
        return dict(self.connection.execute('SELECT question, value FROM answers'))

    def metadata(self):
        return dict(self.connection.execute('SELECT key, value FROM metadata'))

    def events(self, limit=None):
        if limit is not None:
            rows = self.connection.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))
            return list(reversed([dict(row) for row in rows]))
        return [dict(row) for row in self.connection.execute('SELECT * FROM events ORDER BY id')]

    def save_answer(self, question, value):
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO answers VALUES (?, ?)', (question, value))

    def checkpoint(self, state, events, outcome=None):
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('state', state))
            if outcome is not None:
                self.connection.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('outcome', outcome))
            utc = datetime.now(timezone.utc).isoformat()
            self.connection.executemany(
                'INSERT INTO events (utc, monotonic, kind, code, detail) VALUES (?, ?, ?, ?, ?)',
                [(utc, at, kind, code, detail) for at, kind, code, detail in events],
            )

    def export(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + '.tmp')
        temp.write_text(json.dumps({
            'metadata': self.metadata(), 'answers': self.answers(), 'events': self.events(),
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(destination)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
