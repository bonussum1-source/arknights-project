"""
Fetches Arknights story files from GitHub and indexes them into SQLite.

Usage:
    python ingest.py [--token GITHUB_TOKEN] [--limit N]

Environment variable GITHUB_TOKEN is also accepted.
"""

import asyncio
import os
import sys
import argparse
from pathlib import Path
import httpx
from parser import parse_story, extract_title
import db

REPO_OWNER = 'ArknightsAssets'
REPO_NAME = 'ArknightsGamedata'
STORY_PATH = 'kr/gamedata/story'
CACHE_DIR = Path(__file__).parent / 'data' / 'cache'
BRANCH = 'master'


def get_headers(token: str | None) -> dict:
    h = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    return h


def path_to_category(path: str) -> str:
    parts = path.split('/')
    # e.g. kr/gamedata/story/obt/main/level_main_00-01_beg.txt
    # remove the prefix kr/gamedata/story/
    rel = '/'.join(parts[3:]) if len(parts) > 3 else path
    segments = rel.split('/')
    if len(segments) >= 2:
        return '/'.join(segments[:-1])
    return segments[0] if segments else 'unknown'


async def list_tree(client: httpx.AsyncClient, headers: dict) -> list[dict]:
    url = f'https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/git/trees/{BRANCH}?recursive=1'
    r = await client.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    files = [
        item for item in data.get('tree', [])
        if item['type'] == 'blob'
        and item['path'].startswith(STORY_PATH)
        and item['path'].endswith('.txt')
    ]
    return files


async def fetch_raw(client: httpx.AsyncClient, headers: dict, path: str, sha: str) -> str:
    cache_file = CACHE_DIR / sha[:2] / sha
    if cache_file.exists():
        return cache_file.read_text(encoding='utf-8')

    url = f'https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{path}'
    r = await client.get(url, headers={}, timeout=30)
    r.raise_for_status()
    text = r.text

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding='utf-8')
    return text


async def fetch_name_mapping(client: httpx.AsyncClient) -> dict:
    """
    Fetches story_review_table.json and chapter_table.json.
    Returns mapping keyed by FILENAME STEM (no extension, no path)
    so it works regardless of folder structure differences.
    e.g. "level_a001_01_beg" -> {display_name, stage_code}
    """
    mapping = {}

    # ── Activity / event stories ──
    try:
        url = f'https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/kr/gamedata/excel/story_review_table.json'
        r = await client.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        for act_id, act_data in data.items():
            act_name = act_data.get('name', '')
            for entry in act_data.get('infoUnlockDatas', []):
                story_id = entry.get('storyId', '')
                story_name = entry.get('storyName', '')
                stage_code = entry.get('storyCode', '')
                if not story_id or not story_name:
                    continue
                # storyId: "{actId}_{filename_stem}"
                # Key by filename stem so path format doesn't matter
                prefix = act_id + '_'
                stem = story_id[len(prefix):] if story_id.startswith(prefix) else story_id
                mapping[stem] = {
                    'display_name': f'[{stage_code}] {story_name}' if stage_code else story_name,
                    'stage_code': stage_code or act_name,
                }
        print(f'  Name mapping: {len(mapping)} activity stories')
    except Exception as e:
        print(f'  Warning: could not fetch story_review_table: {e}')

    # ── Chapter names for main story context ──
    try:
        url = f'https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/kr/gamedata/excel/chapter_table.json'
        r = await client.get(url, timeout=30)
        r.raise_for_status()
        chapters = r.json()
        mapping['__chapters__'] = {
            v.get('chapterIndex', 0): v.get('chapterName', '')
            for v in chapters.values() if isinstance(v, dict)
        }
    except Exception as e:
        print(f'  Warning: could not fetch chapter_table: {e}')

    return mapping


def _make_display_name(path: str, mapping: dict) -> tuple[str | None, str | None]:
    """Derive display_name and stage_code for a story file path."""
    import re

    filename = path.split('/')[-1].replace('.txt', '')

    # Check activity mapping by filename stem (path-independent)
    if filename in mapping:
        m = mapping[filename]
        return m['display_name'], m['stage_code']

    # Main story: level_main_XX-YY_beg / _end
    m = re.match(r'level_main_(\d+)-(\d+)(?:_(\w+))?', filename)
    if m:
        chapter_num = int(m.group(1))
        stage_num = int(m.group(2))
        suffix_raw = m.group(3) or ''
        suffix_map = {'beg': '시작', 'end': '종료', 'st': ''}
        suffix = suffix_map.get(suffix_raw, suffix_raw)

        chapters = mapping.get('__chapters__', {})
        chapter_name = chapters.get(chapter_num // 4, '')  # rough mapping

        code = f'{chapter_num}-{stage_num}'
        name = f'{code} {suffix}'.strip()
        if chapter_name:
            name = f'{chapter_name} {name}'
        return name, code

    # Other obt stories: clean up filename
    # level_act2mainss_01_beg → "act2mainss 01 시작"
    clean = re.sub(r'^level_', '', filename)
    clean = re.sub(r'_(beg|end)$', lambda x: ' ' + ('시작' if x.group(1) == 'beg' else '종료'), clean)
    clean = clean.replace('_', ' ')
    return clean if clean != filename else None, None


async def ingest(token: str | None = None, limit: int | None = None):
    conn = db.get_conn()
    db.init_db(conn)

    headers = get_headers(token)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as client:
        print('Fetching name mapping...')
        name_mapping = await fetch_name_mapping(client)

        print('Fetching file tree from GitHub...')
        files = await list_tree(client, headers)
        print(f'Found {len(files)} story files.')

        if limit:
            files = files[:limit]

        semaphore = asyncio.Semaphore(5)
        total = 0
        updated = 0

        async def process(item):
            nonlocal total, updated
            async with semaphore:
                path = item['path']
                sha = item['sha']
                try:
                    raw = await fetch_raw(client, headers, path, sha)
                    scenes = parse_story(raw)
                    title = extract_title(raw, path)
                    category = path_to_category(path)
                    display_name, stage_code = _make_display_name(path, name_mapping)
                    changed = db.upsert_story(
                        conn, path, title, category, raw, sha, scenes,
                        display_name=display_name, stage_code=stage_code
                    )
                    total += 1
                    if changed:
                        updated += 1
                        print(f'  [+] {path} ({len(scenes)} scenes)')
                except Exception as e:
                    print(f'  [!] Error {path}: {e}')

        tasks = [process(item) for item in files]
        await asyncio.gather(*tasks)

    print(f'\nDone. {total} files processed, {updated} updated.')


async def update_names_only():
    """Fetch name mapping and apply to existing DB entries without re-downloading stories."""
    conn = db.get_conn()
    db.init_db(conn)
    async with httpx.AsyncClient() as client:
        print('Fetching name mapping...')
        name_mapping = await fetch_name_mapping(client)

    rows = conn.execute('SELECT path FROM stories').fetchall()
    mapping = {}
    for row in rows:
        path = row['path']
        display_name, stage_code = _make_display_name(path, name_mapping)
        if display_name or stage_code:
            mapping[path] = {'display_name': display_name, 'stage_code': stage_code}

    db.update_display_names(conn, mapping)
    print(f'Updated names for {len(mapping)} stories.')


async def build_embeddings():
    """Build semantic embeddings for all scenes that don't have them yet."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print('sentence-transformers not installed. Skipping embeddings.')
        return

    conn = db.get_conn()
    scenes = db.get_scenes_without_embeddings(conn)
    if not scenes:
        print('All embeddings up to date.')
        return

    print(f'Building embeddings for {len(scenes)} scenes...')
    model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

    batch_size = 256
    for start in range(0, len(scenes), batch_size):
        batch = scenes[start:start + batch_size]
        texts = [f"{s['speaker'] or ''}: {s['text']}" if s['speaker'] else s['text'] for s in batch]
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        for s, vec in zip(batch, vecs):
            db.save_embedding(conn, s['id'], vec.tolist())
        conn.commit()
        print(f'  {min(start + batch_size, len(scenes))}/{len(scenes)}')

    print('Embeddings complete.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--token', default=os.environ.get('GITHUB_TOKEN'))
    parser.add_argument('--limit', type=int, default=None, help='Limit number of files for testing')
    parser.add_argument('--embeddings-only', action='store_true')
    parser.add_argument('--names-only', action='store_true', help='Update display names without re-fetching stories')
    args = parser.parse_args()

    if args.names_only:
        asyncio.run(update_names_only())
    elif not args.embeddings_only:
        asyncio.run(ingest(token=args.token, limit=args.limit))
        asyncio.run(build_embeddings())
    else:
        asyncio.run(build_embeddings())
