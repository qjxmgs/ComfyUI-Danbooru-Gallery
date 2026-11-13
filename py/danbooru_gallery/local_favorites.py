"""Account-independent saved posts and thumbnails; never evicted as cache."""
import asyncio
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

from aiohttp import web


POST_FIELDS = {
    'id', 'source_site', 'tag_string', 'rating', 'created_at', 'score', 'md5',
    'preview_file_url', 'large_file_url', 'file_url', 'file_ext',
    'image_width', 'image_height', 'source', '_gelbooru_preview_only',
    '_gelbooru_hydrated', '_tag_categories_complete', '_tag_categories_exact',
    '_tag_categories_source', '_uncategorized_tags',
    *(f'tag_string_{category}' for category in ('artist', 'copyright', 'character', 'general', 'meta')),
}


def identity(source, post_id=None):
    if source not in ('danbooru', 'gelbooru'):
        raise ValueError('Invalid gallery source')
    if post_id is not None and not re.fullmatch(r'[1-9][0-9]*', str(post_id)):
        raise ValueError('Invalid post ID')
    return source, str(post_id) if post_id is not None else None


def has_details(post):
    return 'tag_string' in post and not post.get('_gelbooru_preview_only', False)


class LocalFavoritesStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(__file__).parent / 'data' / 'local_favorites.sqlite'
        self._lock = threading.Lock()
        self._initialized = False

    def connect(self):
        with self._lock:
            if not self._initialized:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
                    conn.executescript('''
                        PRAGMA journal_mode=WAL;
                        CREATE TABLE IF NOT EXISTS favorites(
                            source TEXT NOT NULL, id TEXT NOT NULL, saved TEXT NOT NULL,
                            payload TEXT NOT NULL, thumbnail BLOB,
                            PRIMARY KEY(source,id));
                        CREATE TABLE IF NOT EXISTS tags(
                            source TEXT NOT NULL, id TEXT NOT NULL, tag TEXT NOT NULL,
                            PRIMARY KEY(source,id,tag));
                        CREATE INDEX IF NOT EXISTS favorite_tags ON tags(source,tag,id);
                    ''')
                self._initialized = True
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _post(row):
        if row is None:
            return None
        post = json.loads(row['payload'])
        post.update(_local_favorite=True, _favorite_saved_at=row['saved'],
                    _favorite_incomplete=not row['has_thumbnail'] or not has_details(post),
                    _favorite_thumbnail_url=f"/danbooru_gallery/favorites/thumbnail?source={row['source']}&post_id={row['id']}")
        return post

    def get(self, source, post_id):
        identity(source, post_id)
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT *, thumbnail IS NOT NULL AS has_thumbnail FROM favorites WHERE source=? AND id=?',
                               (source, str(post_id))).fetchone()
        return self._post(row)

    def save(self, source, post, thumbnail=None, expected_saved=None):
        source, post_id = identity(source, post.get('id', ''))
        payload = {key: value for key, value in post.items() if key in POST_FIELDS}
        payload.update(id=post_id, source_site=source)
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT payload,saved FROM favorites WHERE source=? AND id=?', (source, post_id)).fetchone()
            # A download that finishes after remove/re-add must not restore old data.
            if expected_saved is not None and (old is None or old['saved'] != expected_saved):
                return None
            if old:
                previous = json.loads(old['payload'])
                payload = previous if has_details(previous) and not has_details(payload) else {**previous, **payload}
            saved = old['saved'] if old else str(time.time_ns())
            conn.execute('''INSERT INTO favorites VALUES (?,?,?,?,?) ON CONFLICT(source,id)
                DO UPDATE SET payload=excluded.payload, thumbnail=COALESCE(excluded.thumbnail,favorites.thumbnail)''',
                (source, post_id, saved, json.dumps(payload, ensure_ascii=False), thumbnail))
            tags = set(str(payload.get('tag_string') or '').lower().split())
            for category in ('artist', 'copyright', 'character', 'general', 'meta'):
                tags.update(str(payload.get(f'tag_string_{category}') or '').lower().split())
            conn.execute('DELETE FROM tags WHERE source=? AND id=?', (source, post_id))
            conn.executemany('INSERT INTO tags VALUES (?,?,?)', ((source, post_id, tag) for tag in tags))
        return self.get(source, post_id)

    def remove(self, source, post_id):
        identity(source, post_id)
        with closing(self.connect()) as conn, conn:
            conn.execute('DELETE FROM favorites WHERE source=? AND id=?', (source, str(post_id)))
            conn.execute('DELETE FROM tags WHERE source=? AND id=?', (source, str(post_id)))

    def ids(self, source):
        identity(source)
        with closing(self.connect()) as conn:
            return [row[0] for row in conn.execute('SELECT id FROM favorites WHERE source=? ORDER BY saved DESC,id DESC', (source,))]

    def thumbnail(self, source, post_id):
        identity(source, post_id)
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT thumbnail FROM favorites WHERE source=? AND id=?', (source, str(post_id))).fetchone()
        return row[0] if row else None

    def search(self, source, query='', rating='', page=1, limit=42, translate=lambda tag: tag):
        identity(source)
        page, limit = max(1, int(page)), max(1, min(200, int(limit)))
        where, args = ['f.source=?'], [source]
        ratings = {'general': 'g', 'sensitive': 's', 'questionable': 'q', 'explicit': 'e'}
        selected = [ratings.get(r, r) for r in rating.split(',') if r and r != 'all']
        if selected:
            where.append("json_extract(f.payload,'$.rating') IN (" + ','.join('?' for _ in selected) + ')')
            args.extend(selected)
        order = 'f.saved DESC,CAST(f.id AS INTEGER) DESC'
        for token in re.split(r'[\s,]+', query.strip()):
            if not token:
                continue
            if token.startswith('order:'):
                orders = {'saved': order, 'id': 'CAST(f.id AS INTEGER) DESC', 'id_desc': 'CAST(f.id AS INTEGER) DESC',
                          'id_asc': 'CAST(f.id AS INTEGER) ASC', 'newest': "json_extract(f.payload,'$.created_at') DESC,f.id DESC",
                          'oldest': "json_extract(f.payload,'$.created_at') ASC,f.id ASC", 'score': "json_extract(f.payload,'$.score') DESC,f.id DESC"}
                if token[6:] not in orders:
                    raise ValueError(f'Unsupported local favorites search: {token}')
                order = orders[token[6:]]
            elif token.startswith('rating:'):
                value = ratings.get(token[7:], token[7:])
                if value not in ('g', 's', 'q', 'e'):
                    raise ValueError('Invalid rating')
                where.append("json_extract(f.payload,'$.rating')=?")
                args.append(value)
            elif token.startswith('date:'):
                values = token[5:].split('..')
                if len(values) > 2 or not any(values):
                    raise ValueError('Date format: date:YYYY-MM-DD or date:start..end')
                for value in values:
                    if value:
                        date.fromisoformat(value)
                where.append("substr(json_extract(f.payload,'$.created_at'),1,10) BETWEEN ? AND ?")
                args.extend((values[0] or '0001-01-01', values[-1] or '9999-12-31'))
            else:
                if ':' in token or token.startswith('~') or '*' in token:
                    raise ValueError(f'Unsupported local favorites search: {token}')
                negative = token.startswith('-')
                tag = token[1:] if negative else token
                if re.search(r'[\u3400-\u9fff]', tag):
                    tag = translate(tag)
                    if not tag:
                        raise ValueError(f'Unknown tag: {token}')
                where.append(('NOT ' if negative else '') + 'EXISTS (SELECT 1 FROM tags t WHERE t.source=f.source AND t.id=f.id AND t.tag=?)')
                args.append(tag.lower())
        sql = 'SELECT f.*, thumbnail IS NOT NULL AS has_thumbnail FROM favorites f WHERE ' + ' AND '.join(where)
        with closing(self.connect()) as conn:
            rows = conn.execute(sql + f' ORDER BY {order} LIMIT ? OFFSET ?', (*args, limit, (page - 1) * limit)).fetchall()
        return [self._post(row) for row in rows]


class LocalFavorites:
    def __init__(self, routes, store, cached_thumbnail, complete, translate):
        self.store, self.cached_thumbnail, self.complete, self.translate = store, cached_thumbnail, complete, translate
        self.semaphore = asyncio.Semaphore(2)
        routes.get('/danbooru_gallery/favorites')(self.ids)
        routes.post('/danbooru_gallery/favorites/add')(self.add)
        routes.post('/danbooru_gallery/favorites/remove')(self.remove)
        routes.get('/danbooru_gallery/favorites/posts')(self.posts)
        routes.get('/danbooru_gallery/favorites/thumbnail')(self.thumbnail)
        routes.post('/danbooru_gallery/favorites/retry')(self.retry)

    async def ids(self, request):
        try:
            source = request.query.get('source', 'danbooru')
            ids = await asyncio.to_thread(self.store.ids, source)
            return web.json_response(dict(success=True, source=source, favorites=ids), headers={'Cache-Control': 'no-store'})
        except (ValueError, sqlite3.Error, OSError) as error:
            return self.error(error)

    @staticmethod
    def error(error):
        return web.json_response({'success': False, 'error': str(error)}, status=400 if isinstance(error, (ValueError, TypeError)) else 500)

    async def add(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            source, post_id = identity(body.get('source', 'danbooru'), body.get('post_id', ''))
            post = body.get('post')
            if not isinstance(post, dict) or str(post.get('id')) != post_id:
                raise ValueError('Post metadata is required')
            # Commit first. Failure to acquire a thumbnail never loses a bookmark.
            saved = await asyncio.to_thread(self.store.save, source, post)
            try:
                thumb = await self.cached_thumbnail(request, source, saved)
                if thumb is not None:
                    saved = await asyncio.to_thread(self.store.save, source, saved, thumb, saved['_favorite_saved_at'])
            except (OSError, sqlite3.Error, ValueError):
                pass
            return web.json_response(dict(success=True, post=saved))
        except (ValueError, TypeError, sqlite3.Error, OSError) as error:
            return self.error(error)

    async def remove(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            source, post_id = identity(body.get('source', 'danbooru'), body.get('post_id', ''))
            await asyncio.to_thread(self.store.remove, source, post_id)
            return web.json_response(dict(success=True))
        except (ValueError, TypeError, sqlite3.Error, OSError) as error:
            return self.error(error)

    async def posts(self, request):
        try:
            q = request.query
            posts = await asyncio.to_thread(self.store.search, q.get('source', 'danbooru'), q.get('search[tags]', ''),
                                           q.get('search[rating]', ''), q.get('page', 1), q.get('limit', 42), self.translate)
            return web.json_response(posts, headers={'Cache-Control': 'no-store', 'X-Gallery-Source': 'favorites'})
        except (ValueError, sqlite3.Error, OSError) as error:
            return self.error(error)

    async def thumbnail(self, request):
        try:
            data = await asyncio.to_thread(self.store.thumbnail, request.query.get('source', 'danbooru'), request.query.get('post_id', ''))
            if data is None:
                raise web.HTTPNotFound(text='Thumbnail not saved')
            return web.Response(body=data, content_type='image/jpeg', headers={'Cache-Control': 'private, no-store'})
        except (ValueError, sqlite3.Error, OSError) as error:
            return self.error(error)

    async def retry(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            source, post_id = identity(body.get('source', 'danbooru'), body.get('post_id', ''))
            async with self.semaphore:
                post = await asyncio.to_thread(self.store.get, source, post_id)
                if post is None:
                    raise ValueError('Favorite no longer exists')
                saved = post['_favorite_saved_at']
                updated, thumb = await self.complete(request, source, post)
                post = await asyncio.to_thread(self.store.save, source, updated, thumb, saved)
            return web.json_response(dict(success=True, post=post))
        except (ValueError, TypeError, sqlite3.Error, OSError) as error:
            return self.error(error)
