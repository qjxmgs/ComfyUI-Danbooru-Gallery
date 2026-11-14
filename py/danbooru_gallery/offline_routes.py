"""Local-only storage endpoints and automatic Danbooru fallback."""
import asyncio
from contextlib import asynccontextmanager
import json
import re
from urllib.parse import urlparse
from aiohttp import web
from .offline_store import OfflineStore, fingerprint, query_spec
from .thumbnail_cache import get_thumbnail, save_thumbnail


class ThumbnailSlots:
    """Two downloads, with at most one background transfer and foreground-first admission."""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.pending = {}
        self.active = 0
        self.background = 0
        self.running = set()

    async def promote(self, key):
        await self.set_priority(key, False)

    async def set_priority(self, key, background):
        async with self.condition:
            if key in self.pending:
                self.pending[key] = background
                self.condition.notify_all()

    @asynccontextmanager
    async def claim(self, key, background):
        async with self.condition:
            self.pending[key] = background
            try:
                await self.condition.wait_for(lambda: self.active < 2 and
                    (not self.pending[key] or self.background == 0) and
                    key == min(self.pending, key=self.pending.get))
            except BaseException:
                self.pending.pop(key, None)
                self.condition.notify_all()
                raise
            background = self.pending.pop(key)
            self.active += 1
            self.background += int(background)
            self.running.add(key)
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.background -= int(background)
                self.running.discard(key)
                self.condition.notify_all()


class OfflineGallery:
    def __init__(self, server, load_auth, fetch, image_headers, translator):
        self.server = server
        self.load_auth = load_auth
        self.fetch = fetch
        self.image_headers = image_headers
        self.translator = translator
        self.stores = {}
        self.jobs = {}
        self.paused = set()
        self.downloads = {}
        self.write_locks = {}
        self.revisions = {}
        self.slots = ThumbnailSlots()
        self.foreground_downloads = set()
        self.download_waiters = {}
        self.foreground_waiters = {}
        routes = server.routes
        routes.get('/danbooru_gallery/offline/status')(self.status)
        routes.post('/danbooru_gallery/offline/mode')(self.set_mode)
        routes.get('/danbooru_gallery/offline/history')(self.history)
        routes.get('/danbooru_gallery/offline/search')(self.search)
        routes.get('/danbooru_gallery/offline/thumbnail')(self.thumbnail)
        routes.post('/danbooru_gallery/offline/delete')(self.delete)

    def scope(self, request):
        user_path = self.server.user_manager.get_request_user_filepath(request, 'ComfyUI-Danbooru-Gallery/offline.json')
        if not user_path:
            raise web.HTTPForbidden(text='无法确定当前 ComfyUI 用户')
        username, _ = self.load_auth()
        key = fingerprint(['danbooru', user_path, username or 'anonymous'])
        if key not in self.stores:
            self.stores[key] = OfflineStore(key)
            self.write_locks[key] = asyncio.Lock()
            self.revisions[key] = 0
        return key, self.stores[key]

    def check_image_output(self, selection_data):
        try:
            data = json.loads(selection_data or '{}')
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        key = data.get('_offline_scope', '')
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
            return
        store = self.stores.get(key) or OfflineStore(key)
        if key in self.paused or store.mode() == 'local':
            raise RuntimeError('D站处于离线模式，未缓存原图，无法输出图像。请复制提示词或重试联网。')

    async def is_local(self, key, store):
        return key in self.paused or await asyncio.to_thread(store.mode) == 'local'

    async def stop(self, key):
        task = self.jobs.pop(key, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        active = [task for (scope, _), task in list(self.downloads.items()) if scope == key]
        # Wait for already-started thread requests to finish; cancelling their asyncio
        # wrappers would release the semaphore while the HTTP request still runs.
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def start(self, key, store):
        if key in self.jobs and not self.jobs[key].done():
            return
        self.jobs[key] = asyncio.create_task(self.fill(key, store))

    async def fetch_thumbnail(self, key, store, url, background=False, check_connected=None):
        job_key = (key, url)
        self.download_waiters[job_key] = self.download_waiters.get(job_key, 0) + 1
        if not background:
            self.foreground_waiters[job_key] = self.foreground_waiters.get(job_key, 0) + 1
            self.foreground_downloads.add(job_key)
        task = self.downloads.get(job_key)
        if task is None:
            task = asyncio.create_task(self._fetch_thumbnail(key, store, url))
            self.downloads[job_key] = task
            def finished(done):
                if self.downloads.get(job_key) is done:
                    self.downloads.pop(job_key, None)
                # A disconnected browser still leaves a shielded transfer to finish caching.
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        try:
            if not background:
                await self.slots.promote(job_key)
            if check_connected:
                # ComfyUI's aiohttp runner does not cancel handlers on disconnect.
                # Drop abandoned queued work; started thread downloads still finish caching.
                while not task.done():
                    if not check_connected():
                        raise asyncio.CancelledError
                    await asyncio.wait([task], timeout=0.25)
            return await asyncio.shield(task)
        finally:
            remaining = self.download_waiters[job_key] - 1
            if remaining:
                self.download_waiters[job_key] = remaining
            else:
                self.download_waiters.pop(job_key, None)
            if not background:
                foreground = self.foreground_waiters[job_key] - 1
                if foreground:
                    self.foreground_waiters[job_key] = foreground
                else:
                    self.foreground_waiters.pop(job_key, None)
                    self.foreground_downloads.discard(job_key)
            if not remaining and job_key not in self.slots.running and not task.done():
                if self.downloads.get(job_key) is task:
                    self.downloads.pop(job_key, None)
                task.cancel()
            elif job_key not in self.foreground_downloads:
                await self.slots.set_priority(job_key, True)

    async def _fetch_thumbnail(self, key, store, url):
        data = await asyncio.to_thread(store.thumbnail, url)
        if data is not None:
            return data
        # Reuse previous proxy cache without transferring its eviction policy.
        data = await asyncio.to_thread(get_thumbnail, url)
        if data is None:
            post = await asyncio.to_thread(store.preview_post, url)
            if post and post.get('md5'):
                data = await asyncio.to_thread(get_thumbnail, f"{url}?v={post['md5']}")
        if data is None:
            if await self.is_local(key, store):
                return None
            host = (urlparse(url).hostname or '').lower()
            if urlparse(url).scheme not in ('http', 'https') or not (host == 'donmai.us' or host.endswith('.donmai.us')):
                return None
            job_key = (key, url)
            async with self.slots.claim(job_key, job_key not in self.foreground_downloads):
                if await self.is_local(key, store):
                    return None
                response = await asyncio.to_thread(self.fetch, 'GET', url,
                    headers=self.image_headers(host), timeout=(8, 15))
                response.raise_for_status()
                if await self.is_local(key, store):
                    return None
                data = await asyncio.to_thread(save_thumbnail, url, response.content)
        return await asyncio.to_thread(store.thumbnail, url, data)

    async def fill(self, key, store):
        delay = 5
        try:
            while not await self.is_local(key, store):
                urls = await asyncio.to_thread(store.missing_thumbnails, 1000)
                if not urls:
                    retry = await asyncio.to_thread(store.retry_delay)
                    if retry is None:
                        return
                    await asyncio.sleep(retry)
                    continue
                failed = False
                for offset in range(0, len(urls), 2):
                    if await self.is_local(key, store):
                        return
                    results = await asyncio.gather(*(self.fetch_thumbnail(key, store, url, background=True) for url in urls[offset:offset+2]), return_exceptions=True)
                    for url, result in zip(urls[offset:offset+2], results):
                        if isinstance(result, Exception) or result is None:
                            await asyncio.to_thread(store.thumbnail_failed, url)
                    if any(isinstance(result, Exception) or result is None for result in results):
                        failed = True
                        # Back off immediately when the upstream is unreachable.
                        await asyncio.sleep(delay)
                        delay = min(60, delay * 2)
                    else:
                        delay = 5
                if failed:
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

    @staticmethod
    def response(posts, source, saved=0, history='', missing=False):
        return web.json_response(posts, headers={
            'Cache-Control': 'no-store', 'X-Gallery-Source': source,
            'X-Gallery-Saved': str(saved), 'X-Gallery-History': history,
            'X-Gallery-Missing': '1' if missing else '0',
        })

    async def posts(self, request, load_online):
        key, store = self.scope(request)
        spec = query_spec(request.query)
        revision = self.revisions[key]
        page = max(1, int(request.query.get('page', 1)))
        cursor = request.query.get('before_id', '')
        history = request.query.get('history', '')
        explicit_history = bool(history)
        if explicit_history:
            self.paused.add(key)
            await self.stop(key)
        history = history or fingerprint(spec)
        local = await self.is_local(key, store)
        if request.query.get('retry') == '1' and await asyncio.to_thread(store.mode) == 'auto':
            self.paused.discard(key)
            local = False
        if not local and not explicit_history:
            try:
                posts = await load_online()
                if not isinstance(posts, list) or any(not isinstance(p, dict) or p.get('error') or not p.get('id') for p in posts):
                    raise ValueError('上游返回无效页面')
                async with self.write_locks[key]:
                    if revision != self.revisions[key] or await self.is_local(key, store):
                        raise ValueError('缓存状态已变化')
                    history, saved = await asyncio.to_thread(store.save_page, spec, page, cursor, posts)
                self.start(key, store)
                return self.response(posts, 'online', saved, history)
            except Exception:
                self.paused.add(key)
                await self.stop(key)
        snapshot = await asyncio.to_thread(store.page, history, page, None if explicit_history else cursor)
        if snapshot is not None:
            posts, saved = snapshot
            return self.response(posts, 'history', saved, history)
        return self.response([], 'history', history=history, missing=True)

    async def status(self, request):
        key, store = self.scope(request)
        stats = await asyncio.to_thread(store.stats, request.query.get('history') or None)
        return web.json_response({**stats, 'scope': key, 'mode': await asyncio.to_thread(store.mode), 'paused': key in self.paused})

    async def set_mode(self, request):
        key, store = self.scope(request)
        payload = await request.json()
        try:
            mode = await asyncio.to_thread(store.mode, payload.get('mode'))
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        if mode == 'local':
            # A previous account's background queue must not keep using the network.
            for active_key in list(self.stores):
                self.paused.add(active_key)
            await asyncio.gather(*(self.stop(active_key) for active_key in list(self.stores)))
        else:
            self.paused.discard(key)
            self.start(key, store)
        return web.json_response({'mode': mode})

    async def history(self, request):
        _, store = self.scope(request)
        offset = max(0, int(request.query.get('offset', 0)))
        return web.json_response({'items': await asyncio.to_thread(store.history, offset)})

    async def search(self, request):
        key, store = self.scope(request)
        self.paused.add(key)
        await self.stop(key)
        try:
            spec = query_spec(request.query)
            posts = await asyncio.to_thread(store.search, spec['tags'], spec['rating'],
                max(1, int(request.query.get('page', 1))), spec['limit'], self.translator.get_english)
            return self.response(posts, 'search')
        except ValueError as error:
            return web.json_response({'error': str(error)}, status=400)

    async def thumbnail(self, request):
        key, store = self.scope(request)
        url = request.query.get('url', '')
        post = await asyncio.to_thread(store.preview_post, url)
        if post is None:
            raise web.HTTPNotFound(text='此图片不属于当前账号的离线记录')
        data = await asyncio.to_thread(store.thumbnail, url)
        if data is None:
            # Reuse the same bounded, deduplicated download tasks as background filling.
            data = await asyncio.to_thread(get_thumbnail, url)
            if data is None and post.get('md5'):
                data = await asyncio.to_thread(get_thumbnail, f"{url}?v={post['md5']}")
            if data is not None:
                await asyncio.to_thread(store.thumbnail, url, data)
            elif not await self.is_local(key, store):
                try:
                    data = await self.fetch_thumbnail(key, store, url,
                        check_connected=lambda: request.transport is not None and not request.transport.is_closing())
                except Exception:
                    data = None
        if data is None:
            raise web.HTTPNotFound(text='缩略图尚未缓存')
        return web.Response(body=data, content_type='image/jpeg', headers={'Cache-Control': 'private, no-store'})

    async def delete(self, request):
        key, store = self.scope(request)
        payload = await request.json()
        if payload.get('confirmed') is not True or ('history' in payload and not payload['history']):
            raise web.HTTPBadRequest(text='请先确认删除范围')
        was_paused = key in self.paused
        self.paused.add(key)
        self.revisions[key] += 1
        await self.stop(key)
        async with self.write_locks[key]:
            await asyncio.to_thread(store.delete, payload.get('history') or None)
        if not was_paused:
            self.paused.discard(key)
        return web.json_response({'success': True, **await asyncio.to_thread(store.stats)})
