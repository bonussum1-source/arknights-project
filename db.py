import sqlite3
import json
import struct
from pathlib import Path

DB_PATH = Path(__file__).parent / 'data' / 'arknights.db'


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS stories (
            path TEXT PRIMARY KEY,
            title TEXT,
            display_name TEXT,
            stage_code TEXT,
            category TEXT,
            raw_text TEXT,
            sha TEXT,
            fetched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS scenes (
            id INTEGER PRIMARY KEY,
            story_path TEXT REFERENCES stories(path) ON DELETE CASCADE,
            type TEXT,
            speaker TEXT,
            text TEXT,
            line_num INTEGER,
            background TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS scenes_fts USING fts5(
            text, speaker, story_path UNINDEXED,
            content=scenes, content_rowid=id,
            tokenize="unicode61"
        );

        CREATE TABLE IF NOT EXISTS embeddings (
            scene_id INTEGER PRIMARY KEY REFERENCES scenes(id) ON DELETE CASCADE,
            vector BLOB
        );

        CREATE TRIGGER IF NOT EXISTS scenes_ai AFTER INSERT ON scenes BEGIN
            INSERT INTO scenes_fts(rowid, text, speaker, story_path)
            VALUES (new.id, new.text, new.speaker, new.story_path);
        END;

        CREATE TRIGGER IF NOT EXISTS scenes_ad AFTER DELETE ON scenes BEGIN
            INSERT INTO scenes_fts(scenes_fts, rowid, text, speaker, story_path)
            VALUES ('delete', old.id, old.text, old.speaker, old.story_path);
        END;
    ''')
    # Migration: add columns if they don't exist yet
    for col in ('display_name TEXT', 'stage_code TEXT'):
        try:
            conn.execute(f'ALTER TABLE stories ADD COLUMN {col}')
        except Exception:
            pass
    conn.commit()


def upsert_story(conn, path: str, title: str, category: str, raw_text: str, sha: str, scenes: list[dict],
                 display_name: str = None, stage_code: str = None):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute('SELECT sha FROM stories WHERE path=?', (path,)).fetchone()
    if existing and existing['sha'] == sha:
        return False  # unchanged

    conn.execute('DELETE FROM scenes WHERE story_path=?', (path,))
    conn.execute('''
        INSERT OR REPLACE INTO stories(path, title, display_name, stage_code, category, raw_text, sha, fetched_at)
        VALUES (?,?,?,?,?,?,?,?)
    ''', (path, title, display_name, stage_code, category, raw_text, sha, now))

    for i, s in enumerate(scenes):
        conn.execute('''
            INSERT INTO scenes(story_path, type, speaker, text, line_num, background)
            VALUES (?,?,?,?,?,?)
        ''', (path, s['type'], s.get('speaker'), s['text'], i, s.get('background')))

    conn.commit()
    return True


def _natural_sort_key(path: str) -> list:
    """Split path into alternating string/int parts for natural sort."""
    import re
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', path)]


def update_display_names(conn, mapping: dict):
    """mapping: {path: {display_name, stage_code}}"""
    for path, info in mapping.items():
        conn.execute(
            'UPDATE stories SET display_name=?, stage_code=? WHERE path=?',
            (info.get('display_name'), info.get('stage_code'), path)
        )
    conn.commit()


def get_story_tree(conn) -> list[dict]:
    rows = conn.execute(
        'SELECT path, title, display_name, stage_code, category FROM stories'
    ).fetchall()
    result = [dict(r) for r in rows]
    result.sort(key=lambda r: _natural_sort_key(r['path']))
    return result


def get_story(conn, path: str) -> dict | None:
    story = conn.execute('SELECT * FROM stories WHERE path=?', (path,)).fetchone()
    if not story:
        return None
    scenes = conn.execute(
        'SELECT id, type, speaker, text, background FROM scenes WHERE story_path=? ORDER BY line_num',
        (path,)
    ).fetchall()
    return {'story': dict(story), 'scenes': [dict(s) for s in scenes]}


def fts_search(conn, query: str, limit: int = 50) -> list[dict]:
    rows = conn.execute('''
        SELECT s.id, s.story_path, s.type, s.speaker, s.text, s.background,
               st.title, st.display_name, st.stage_code, st.category,
               snippet(scenes_fts, 0, '<mark>', '</mark>', '…', 20) AS snippet
        FROM scenes_fts f
        JOIN scenes s ON s.id = f.rowid
        JOIN stories st ON st.path = s.story_path
        WHERE scenes_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    ''', (query, limit)).fetchall()
    return [dict(r) for r in rows]


def get_all_scene_ids(conn) -> list[int]:
    return [r[0] for r in conn.execute('SELECT id FROM scenes ORDER BY id').fetchall()]


def get_scenes_without_embeddings(conn) -> list[dict]:
    rows = conn.execute('''
        SELECT s.id, s.text, s.speaker
        FROM scenes s
        LEFT JOIN embeddings e ON e.scene_id = s.id
        WHERE e.scene_id IS NULL AND s.text IS NOT NULL AND s.text != ''
    ''').fetchall()
    return [dict(r) for r in rows]


def save_embedding(conn, scene_id: int, vector: list[float]):
    blob = struct.pack(f'{len(vector)}f', *vector)
    conn.execute(
        'INSERT OR REPLACE INTO embeddings(scene_id, vector) VALUES (?,?)',
        (scene_id, blob)
    )


def load_all_embeddings(conn) -> tuple[list[int], list]:
    import numpy as np
    rows = conn.execute(
        'SELECT scene_id, vector FROM embeddings ORDER BY scene_id'
    ).fetchall()
    if not rows:
        return [], np.array([])
    ids = []
    vecs = []
    dim = None
    for r in rows:
        blob = r[1]
        n = len(blob) // 4
        if dim is None:
            dim = n
        vec = struct.unpack(f'{n}f', blob)
        ids.append(r[0])
        vecs.append(vec)
    return ids, np.array(vecs, dtype=np.float32)


def get_scenes_by_ids(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ','.join('?' * len(ids))
    rows = conn.execute(f'''
        SELECT s.id, s.story_path, s.type, s.speaker, s.text, s.background,
               st.title, st.display_name, st.stage_code, st.category
        FROM scenes s
        JOIN stories st ON st.path = s.story_path
        WHERE s.id IN ({placeholders})
    ''', ids).fetchall()
    by_id = {r['id']: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]
