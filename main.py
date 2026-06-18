import os
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

from contextlib import asynccontextmanager
from pathlib import Path
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import db

_conn = None
_embed_model = None
_embed_ids: list[int] = []
_embed_matrix: np.ndarray = np.array([])


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _conn, _embed_model, _embed_ids, _embed_matrix
    _conn = db.get_conn()
    db.init_db(_conn)

    # Try loading embedding model
    try:
        from sentence_transformers import SentenceTransformer
        print('Loading embedding model...')
        _embed_model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
        print('Loading embedding vectors...')
        _embed_ids, _embed_matrix = db.load_all_embeddings(_conn)
        print(f'Loaded {len(_embed_ids)} embeddings.')
    except Exception as e:
        print(f'Semantic search unavailable: {e}')

    yield
    if _conn:
        _conn.close()


app = FastAPI(lifespan=lifespan)


@app.get('/', response_class=HTMLResponse)
async def root():
    html = Path(__file__).parent / 'index.html'
    return HTMLResponse(html.read_text(encoding='utf-8'))


@app.get('/api/tree')
async def api_tree():
    stories = db.get_story_tree(_conn)
    # Build nested tree
    tree: dict = {}
    for s in stories:
        category = s['category']
        if category not in tree:
            tree[category] = []
        tree[category].append({'path': s['path'], 'title': s['title'], 'display_name': s['display_name'], 'stage_code': s['stage_code']})
    return {'tree': tree, 'total': len(stories)}


@app.get('/api/story')
async def api_story(path: str = Query(...)):
    result = db.get_story(_conn, path)
    if not result:
        raise HTTPException(404, 'Story not found')
    return result


@app.get('/api/search')
async def api_search(
    q: str = Query(..., min_length=1),
    mode: str = Query('text', pattern='^(text|semantic)$'),
    limit: int = Query(30, ge=1, le=100),
):
    if mode == 'text':
        # Escape FTS5 special chars
        safe_q = q.replace('"', '""')
        results = db.fts_search(_conn, f'"{safe_q}"', limit=limit)
        return {'results': results, 'mode': 'text', 'query': q}

    # Semantic search
    if _embed_model is None or len(_embed_ids) == 0:
        raise HTTPException(503, 'Semantic search not available. Run: python ingest.py --embeddings-only')

    qvec = _embed_model.encode([q], normalize_embeddings=True)[0]
    scores = _embed_matrix @ qvec  # cosine similarity (vectors are normalized)
    top_idx = np.argsort(scores)[::-1][:limit]
    top_ids = [_embed_ids[i] for i in top_idx]
    top_scores = [float(scores[i]) for i in top_idx]

    scenes = db.get_scenes_by_ids(_conn, top_ids)
    score_map = dict(zip(top_ids, top_scores))
    for s in scenes:
        s['score'] = score_map.get(s['id'], 0.0)

    return {'results': scenes, 'mode': 'semantic', 'query': q}


@app.get('/api/status')
async def api_status():
    row = _conn.execute('SELECT COUNT(*) as n FROM stories').fetchone()
    scenes_row = _conn.execute('SELECT COUNT(*) as n FROM scenes').fetchone()
    emb_row = _conn.execute('SELECT COUNT(*) as n FROM embeddings').fetchone()
    return {
        'stories': row['n'],
        'scenes': scenes_row['n'],
        'embeddings': emb_row['n'],
        'semantic_search': _embed_model is not None,
    }
