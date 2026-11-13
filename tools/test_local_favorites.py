"""Local favorites persistence and HTTP behavior without any website account."""
import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('local_favorites', ROOT / 'py/danbooru_gallery/local_favorites.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
QA = Path(os.environ.get('GALLERY_QA_DIR', 'D:/Develop/Temp/Codex/local-favorites-tests'))
QA.mkdir(parents=True, exist_ok=True)


def post(pid=1, **fields):
    return dict(id=pid, tag_string='1girl blue_eyes', tag_string_general='1girl blue_eyes',
                preview_file_url='https://cdn.donmai.us/preview.jpg', rating='g',
                created_at='2026-11-14T00:00:00Z', **fields)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=QA)
        self.path = Path(self.temp.name) / 'favorites.sqlite'
        self.store = MODULE.LocalFavoritesStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_starts_empty_and_survives_reopen(self):
        self.assertEqual(self.store.ids('danbooru'), [])
        saved = self.store.save('danbooru', post(), b'jpeg')
        self.assertFalse(saved['_favorite_incomplete'])
        reopened = MODULE.LocalFavoritesStore(self.path)
        self.assertEqual(reopened.ids('danbooru'), ['1'])
        self.assertEqual(reopened.thumbnail('danbooru', 1), b'jpeg')

    def test_sites_are_separate(self):
        self.store.save('danbooru', post(), b'd')
        self.store.save('gelbooru', post(), b'g')
        self.store.remove('danbooru', 1)
        self.assertEqual(self.store.ids('danbooru'), [])
        self.assertEqual(self.store.thumbnail('gelbooru', 1), b'g')

    def test_duplicate_does_not_change_order_or_remove_thumbnail(self):
        first = self.store.save('danbooru', post(), b'jpeg')
        self.store.save('danbooru', post(2))
        again = self.store.save('danbooru', post())
        self.assertEqual(first['_favorite_saved_at'], again['_favorite_saved_at'])
        self.assertEqual(self.store.ids('danbooru'), ['2', '1'])
        self.assertEqual(self.store.thumbnail('danbooru', 1), b'jpeg')

    def test_partial_duplicate_does_not_replace_complete_details(self):
        self.store.save('gelbooru', post(), b'jpeg')
        saved = self.store.save('gelbooru', dict(id=1, _gelbooru_preview_only=True, tag_string='1girl'))
        self.assertEqual(saved['tag_string'], '1girl blue_eyes')
        self.assertFalse(saved['_favorite_incomplete'])

    def test_concurrent_adds_do_not_lose_data(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.store.save('danbooru', post(i % 12 + 1)), range(48)))
        self.assertEqual(len(self.store.ids('danbooru')), 12)

    def test_late_completion_cannot_resurrect_or_replace_readded_item(self):
        first = self.store.save('danbooru', post())
        self.store.remove('danbooru', 1)
        self.assertIsNone(self.store.save('danbooru', post(), b'old', first['_favorite_saved_at']))
        self.store.save('danbooru', post())
        self.assertIsNone(self.store.save('danbooru', post(), b'old', first['_favorite_saved_at']))
        self.assertIsNone(self.store.thumbnail('danbooru', 1))

    def test_partial_snapshot_and_credentials_are_not_persisted(self):
        saved = self.store.save('gelbooru', dict(id=1, _gelbooru_preview_only=True, api_key='secret'))
        self.assertTrue(saved['_favorite_incomplete'])
        self.assertNotIn('api_key', saved)
        completed = self.store.save('gelbooru', post(_gelbooru_preview_only=False), b'jpeg', saved['_favorite_saved_at'])
        self.assertFalse(completed['_favorite_incomplete'])

    def test_search_filters_and_pagination(self):
        for i in range(1, 6):
            self.store.save('danbooru', post(i))
        self.store.save('gelbooru', post(99))
        self.assertEqual([p['id'] for p in self.store.search('danbooru', '1girl -red_eyes date:2026-11-14', 'general', 2, 2)], ['3', '2'])
        self.assertEqual(self.store.search('danbooru', '-blue_eyes'), [])
        self.assertEqual(self.store.search('danbooru', rating='explicit'), [])
        self.assertEqual(len(self.store.search('danbooru', '女孩', translate=lambda _: '1girl')), 5)
        self.store.remove('danbooru', 5)
        self.assertEqual([p['id'] for p in self.store.search('danbooru', limit=2)], ['4', '3'])

    def test_unsupported_query_and_invalid_identity(self):
        for query in ('order:rank', 'ordfav:alice', '~1girl', 'date:..'):
            with self.assertRaises(ValueError):
                self.store.search('danbooru', query)
        with self.assertRaises(ValueError):
            self.store.save('../account', post())
        with self.assertRaises(ValueError):
            self.store.save('danbooru', post('../../file'))

    def test_remove_is_idempotent_and_deletes_owned_data(self):
        self.store.save('danbooru', post(), b'jpeg')
        self.store.remove('danbooru', 1)
        self.store.remove('danbooru', 1)
        self.assertIsNone(self.store.thumbnail('danbooru', 1))
        self.assertEqual(self.store.search('danbooru', '1girl'), [])


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=QA)
        self.store = MODULE.LocalFavoritesStore(Path(self.temp.name) / 'favorites.sqlite')
        self.cached = AsyncMock(return_value=b'jpeg')
        self.complete = AsyncMock(side_effect=lambda request, source, value: (value, b'jpeg'))
        routes = web.RouteTableDef()
        self.handler = MODULE.LocalFavorites(routes, self.store, self.cached, self.complete, lambda value: value)
        app = web.Application()
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def add(self, source='danbooru'):
        return await self.client.post('/danbooru_gallery/favorites/add', json=dict(source=source, post_id=1, post=post()))

    async def test_account_free_add_browse_thumbnail_remove_never_fetches_remote(self):
        response = await self.add()
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())['post']['_favorite_incomplete'])
        ids = await self.client.get('/danbooru_gallery/favorites?source=danbooru')
        self.assertEqual((await ids.json())['favorites'], ['1'])
        response = await self.client.get('/danbooru_gallery/favorites/posts?source=danbooru')
        self.assertEqual((await response.json())[0]['id'], '1')
        thumb = await self.client.get('/danbooru_gallery/favorites/thumbnail?source=danbooru&post_id=1')
        self.assertEqual(await thumb.read(), b'jpeg')
        response = await self.client.post('/danbooru_gallery/favorites/remove', json=dict(source='danbooru', post_id=1))
        self.assertTrue((await response.json())['success'])
        self.complete.assert_not_called()

    async def test_missing_thumbnail_keeps_bookmark_and_explicit_retry_completes(self):
        self.cached.return_value = None
        response = await self.add('gelbooru')
        self.assertTrue((await response.json())['post']['_favorite_incomplete'])
        self.complete.assert_not_called()
        response = await self.client.post('/danbooru_gallery/favorites/retry', json=dict(source='gelbooru', post_id=1))
        self.assertFalse((await response.json())['post']['_favorite_incomplete'])
        self.complete.assert_awaited_once()

    async def test_cache_read_failure_does_not_lose_bookmark(self):
        self.cached.side_effect = OSError('cache unavailable')
        response = await self.add()
        self.assertTrue((await response.json())['success'])
        self.assertEqual(self.store.ids('danbooru'), ['1'])

    async def test_disk_failure_is_not_reported_as_success(self):
        with patch.object(self.store, 'save', side_effect=sqlite3.OperationalError('disk full')):
            response = await self.add()
        self.assertEqual(response.status, 500)
        self.assertFalse((await response.json())['success'])
        self.assertEqual(self.store.ids('danbooru'), [])

    async def test_invalid_requests_and_unsupported_search(self):
        response = await self.client.post('/danbooru_gallery/favorites/add', json=dict(source='danbooru', post_id=1))
        self.assertEqual(response.status, 400)
        response = await self.client.get('/danbooru_gallery/favorites/posts?search%5Btags%5D=ordfav:alice')
        self.assertEqual(response.status, 400)
        response = await self.client.post('/danbooru_gallery/favorites/add', json=[])
        self.assertEqual(response.status, 400)
        self.complete.assert_not_called()

    async def test_removal_during_retry_cannot_restore_favorite(self):
        await self.add()
        started, finish = asyncio.Event(), asyncio.Event()
        async def delayed(request, source, value):
            started.set()
            await finish.wait()
            return value, b'jpeg'
        self.complete.side_effect = delayed
        request = asyncio.create_task(self.client.post('/danbooru_gallery/favorites/retry', json=dict(source='danbooru', post_id=1)))
        await started.wait()
        await self.client.post('/danbooru_gallery/favorites/remove', json=dict(source='danbooru', post_id=1))
        finish.set()
        response = await request
        self.assertIsNone((await response.json())['post'])
        self.assertEqual(self.store.ids('danbooru'), [])


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    """Exercise production completion callbacks without importing ComfyUI/torch."""
    def setUp(self):
        module = ast.parse((ROOT / 'py/danbooru_gallery/danbooru_gallery.py').read_text(encoding='utf-8'))
        functions = [node for node in module.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name == '_complete_local_favorite']
        self.offline = SimpleNamespace(scope=Mock(return_value=('scope', object())), is_local=AsyncMock(return_value=False))
        self.fetch = Mock(side_effect=AssertionError('unexpected external request'))
        self.cached = AsyncMock(return_value=None)
        self.stored_thumbnail = Mock(return_value=b'permanent')
        self.namespace = dict(asyncio=asyncio, requests=requests, json=__import__('json'),
            local_favorites=SimpleNamespace(store=SimpleNamespace(thumbnail=self.stored_thumbnail)),
            offline_gallery=self.offline, _favorite_cached_thumbnail=self.cached,
            has_details=MODULE.has_details, _fetch_supported_media=self.fetch,
            _danbooru_request=self.fetch, BASE_URL='https://danbooru.donmai.us', logger=Mock())
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'favorite_completion', 'exec'), self.namespace)
        self.complete = self.namespace['_complete_local_favorite']

    async def test_permanent_thumbnail_is_used_after_cache_eviction(self):
        updated, data = await self.complete(object(), 'danbooru', post())
        self.assertEqual(data, b'permanent')
        self.cached.assert_not_called()
        self.fetch.assert_not_called()

    async def test_explicit_local_mode_prevents_completion_network_requests(self):
        self.stored_thumbnail.return_value = None
        self.offline.is_local.return_value = True
        partial = dict(id=1, preview_file_url='https://cdn.donmai.us/a.jpg')
        updated, data = await self.complete(object(), 'danbooru', partial)
        self.assertEqual(updated, partial)
        self.assertIsNone(data)
        self.fetch.assert_not_called()

    async def test_network_failure_preserves_saved_metadata(self):
        self.stored_thumbnail.return_value = None
        self.fetch.side_effect = requests.ConnectionError('offline')
        value = post()
        updated, data = await self.complete(object(), 'danbooru', value)
        self.assertEqual(updated, value)
        self.assertIsNone(data)


if __name__ == '__main__':
    unittest.main()
