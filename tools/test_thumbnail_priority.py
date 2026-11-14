"""Offline scheduler checks, with no network or user database access."""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer


source = Path(__file__).resolve().parents[1] / 'py/danbooru_gallery/offline_routes.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
definitions = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in ('ThumbnailSlots', 'OfflineGallery')]
namespace = {'asyncio': asyncio, 'asynccontextmanager': asynccontextmanager}
exec(compile(ast.Module(body=definitions, type_ignores=[]), str(source), 'exec'), namespace)
ThumbnailSlots = namespace['ThumbnailSlots']
OfflineGallery = namespace['OfflineGallery']


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreground_skips_queued_background_and_retains_two_slot_limit(self):
        slots = ThumbnailSlots()
        started = []
        release = {key: asyncio.Event() for key in ('bg1', 'bg2', 'front1', 'front2')}

        async def download(key, background):
            async with slots.claim(key, background):
                started.append(key)
                self.assertLessEqual(slots.active, 2)
                self.assertLessEqual(slots.background, 1)
                await release[key].wait()

        tasks = [asyncio.create_task(download('bg1', True)), asyncio.create_task(download('bg2', True))]
        await asyncio.sleep(0)
        self.assertEqual(started, ['bg1'])
        tasks += [asyncio.create_task(download('front1', False)), asyncio.create_task(download('front2', False))]
        await asyncio.sleep(0)
        self.assertEqual(started, ['bg1', 'front1'])
        release['bg1'].set()
        await asyncio.sleep(0.01)
        self.assertEqual(started, ['bg1', 'front1', 'front2'])
        for event in release.values():
            event.set()
        await asyncio.gather(*tasks)
        self.assertEqual(slots.active, 0)
        self.assertEqual(slots.pending, {})

    async def test_background_request_can_be_promoted(self):
        slots = ThumbnailSlots()
        async with slots.claim('busy', True):
            task = asyncio.create_task(self.enter(slots, 'promoted'))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            await slots.promote('promoted')
            await asyncio.wait_for(task, 1)

    async def enter(self, slots, key):
        async with slots.claim(key, True):
            return

    async def test_cancelled_waiter_does_not_release_an_active_slot(self):
        slots = ThumbnailSlots()
        async with slots.claim('busy', True):
            task = asyncio.create_task(self.enter(slots, 'cancelled'))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(slots.active, 1)
            self.assertNotIn('cancelled', slots.pending)
        self.assertEqual(slots.active, 0)

    async def test_disconnected_browser_does_not_cancel_shared_download(self):
        gallery = OfflineGallery.__new__(OfflineGallery)
        gallery.slots = ThumbnailSlots()
        gallery.foreground_downloads = set()
        gallery.download_waiters = {}
        gallery.foreground_waiters = {}
        gallery.downloads = {}
        release = asyncio.Event()

        async def transfer(*args):
            await release.wait()
            return b'cached thumbnail'

        gallery._fetch_thumbnail = AsyncMock(side_effect=transfer)
        first = asyncio.create_task(gallery.fetch_thumbnail('scope', None, 'url', background=True))
        await asyncio.sleep(0)
        second = asyncio.create_task(gallery.fetch_thumbnail('scope', None, 'url'))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(gallery.downloads[('scope', 'url')].cancelled())
        self.assertIn(('scope', 'url'), gallery.foreground_downloads)
        release.set()
        self.assertEqual(await second, b'cached thumbnail')
        gallery._fetch_thumbnail.assert_awaited_once()
        await asyncio.sleep(0)
        self.assertEqual(gallery.downloads, {})

    async def test_closed_http_connection_removes_queued_work(self):
        gallery = OfflineGallery.__new__(OfflineGallery)
        gallery.slots = ThumbnailSlots()
        gallery.downloads = {}
        gallery.foreground_downloads = set()
        gallery.download_waiters = {}
        gallery.foreground_waiters = {}

        async def transfer(key, store, url):
            async with gallery.slots.claim((key, url), False):
                return b'thumbnail'

        gallery._fetch_thumbnail = transfer

        async def handler(request):
            data = await gallery.fetch_thumbnail('scope', None, 'url',
                check_connected=lambda: request.transport is not None and not request.transport.is_closing())
            return web.Response(body=data)

        app = web.Application()
        app.router.add_get('/thumbnail', handler)
        async with TestServer(app) as server, ClientSession() as client:
            async with gallery.slots.claim('busy1', False), gallery.slots.claim('busy2', False):
                request = asyncio.create_task(client.get(server.make_url('/thumbnail')))
                async with asyncio.timeout(2):
                    while ('scope', 'url') not in gallery.slots.pending:
                        await asyncio.sleep(.01)
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                async with asyncio.timeout(2):
                    while gallery.download_waiters or gallery.downloads:
                        await asyncio.sleep(.01)
                self.assertNotIn(('scope', 'url'), gallery.slots.pending)
        self.assertEqual(gallery.foreground_downloads, set())

    async def test_abandoned_queued_download_is_cancelled_but_started_download_finishes(self):
        gallery = OfflineGallery.__new__(OfflineGallery)
        gallery.slots = ThumbnailSlots()
        gallery.downloads = {}
        gallery.foreground_downloads = set()
        gallery.download_waiters = {}
        gallery.foreground_waiters = {}
        started = asyncio.Event()
        release = asyncio.Event()

        async def transfer(key, store, url):
            async with gallery.slots.claim((key, url), False):
                started.set()
                await release.wait()
                return b'finished cache'

        gallery._fetch_thumbnail = transfer
        connected = True
        async with gallery.slots.claim('busy1', False), gallery.slots.claim('busy2', False):
            queued = asyncio.create_task(gallery.fetch_thumbnail('scope', None, 'url', check_connected=lambda: connected))
            await asyncio.sleep(.01)
            self.assertIn(('scope', 'url'), gallery.slots.pending)
            connected = False
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(queued, 1)
            await asyncio.sleep(0)
            self.assertNotIn(('scope', 'url'), gallery.slots.pending)
            self.assertEqual(gallery.downloads, {})
            self.assertFalse(started.is_set())

        active = asyncio.create_task(gallery.fetch_thumbnail('scope', None, 'url'))
        await started.wait()
        transfer_task = gallery.downloads[('scope', 'url')]
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        self.assertFalse(transfer_task.cancelled())
        release.set()
        self.assertEqual(await transfer_task, b'finished cache')
        await asyncio.sleep(0)
        self.assertEqual(gallery.download_waiters, {})
        self.assertEqual(gallery.foreground_waiters, {})
        self.assertEqual(gallery.downloads, {})


if __name__ == '__main__':
    unittest.main()
