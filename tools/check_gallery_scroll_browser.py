"""Real extension, isolated HTTP fixtures; all profiles and reports live in GALLERY_QA_DIR.

GALLERY_SOAK_SECONDS=1800 adds the 30-minute mixed-aspect scrolling endurance run.
"""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import time
import subprocess

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from PIL import Image
from playwright.async_api import async_playwright
try:
    import psutil
except ImportError:
    psutil = None

from check_local_favorites_browser import UI_JS, INDEX


ROOT = Path(__file__).resolve().parents[1]
QA = Path(os.environ['GALLERY_QA_DIR'])
QA.mkdir(parents=True, exist_ok=True)


def renderer_memory():
    if psutil is None:
        return None
    result = []
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            command = ' '.join(process.info['cmdline'] or [])
            if '--type=renderer' not in command or str(QA).lower() not in command.lower():
                continue
            memory = process.memory_info()
            result.append({'pid': process.pid, 'private': getattr(memory, 'private', memory.rss), 'rss': memory.rss})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def posts(start, count, tag='1girl blue_eyes', source='danbooru'):
    return [dict(id=100000 - i, source_site=source, tag_string=tag, tag_string_general=tag,
        preview_file_url=f'https://cdn.donmai.us/fixture_{i}.jpg',
        file_url=f'https://cdn.donmai.us/original_{i}.jpg', image_width=300,
        image_height=[180, 300, 470, 900][i % 4], md5=str(i), rating='g',
        _tag_categories_complete=True, _tag_categories_exact=True)
        for i in range(start, start + count)]


SCROLL = '''async (frames) => {
    const g=nodes[0]._qa.viewport.scroller;
    const gaps=[];let last=performance.now();
    for(let i=0;i<frames;i++) {
        await new Promise(requestAnimationFrame);
        const now=performance.now();gaps.push(now-last);last=now;
        if(g.scrollTop>=g.scrollHeight-g.clientHeight-2) window.scrollDirection=-1;
        if(g.scrollTop<=1) window.scrollDirection=1;
        g.scrollTop+=(window.scrollStep||32)*(window.scrollDirection||1);
    }
    return gaps;
}'''


async def native_soak(url, report):
    """CDP without Network.enable: inspector request history otherwise distorts native memory."""
    profile = QA/'native-browser-profile'
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        'C:/Program Files/Google/Chrome/Application/chrome.exe', '--headless', '--no-first-run',
        '--disable-background-networking', '--disable-default-apps', '--disable-extensions',
        '--window-size=1300,1100', '--remote-debugging-port=0', f'--user-data-dir={profile}', str(url),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    socket = None
    try:
        async with asyncio.timeout(20):
            while not (profile/'DevToolsActivePort').exists():
                await asyncio.sleep(.1)
        port = (profile/'DevToolsActivePort').read_text().splitlines()[0]
        async with ClientSession() as client:
            async with client.get(f'http://127.0.0.1:{port}/json/list') as response:
                targets = await response.json()
            target = next(t for t in targets if t['type'] == 'page')
            socket = await client.ws_connect(target['webSocketDebuggerUrl'])
            sequence = 0

            async def send(method, params=None):
                nonlocal sequence
                sequence += 1
                await socket.send_json({'id': sequence, 'method': method, 'params': params or {}})
                async with asyncio.timeout(30):
                    while True:
                        packet = await socket.receive_json()
                        if packet.get('method') == 'Runtime.exceptionThrown':
                            report['errors'].append(str(packet['params']['exceptionDetails'])[:500])
                        if packet.get('id') == sequence:
                            if 'error' in packet:
                                raise RuntimeError(packet['error'])
                            return packet.get('result', {})

            async def evaluate(expression):
                result = await send('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True})
                if result.get('exceptionDetails'):
                    raise RuntimeError(result['exceptionDetails'])
                return result.get('result', {}).get('value')

            await send('Runtime.enable')
            async with asyncio.timeout(20):
                while not await evaluate('window.ready && nodes[0]._qa.count()>=42 && !nodes[0]._qa.loading()'):
                    await asyncio.sleep(.1)
            count = await evaluate('nodes[0]._qa.count()')
            for start in range(count, 10000, 420):
                await evaluate(f'nodes[0]._qa.seed({json.dumps(posts(start, min(420,10000-start)))})')
            await evaluate('nodes[0].onResize([900,760]);window.longTasks=[];new PerformanceObserver(l=>window.longTasks.push(...l.getEntries().map(e=>e.duration))).observe({type:"longtask"});')
            gaps = await evaluate(f'({SCROLL})(600)')
            report['ten_thousand'] = {'posts': 10000, 'frame_p95_ms': sorted(gaps)[int(len(gaps)*.95)], 'frame_max_ms': max(gaps)}
            report['network_inspector_enabled'] = False
            seconds = int(os.environ.get('GALLERY_SOAK_SECONDS', '1800'))
            step = int(os.environ.get('GALLERY_SCROLL_STEP', '32'))
            await evaluate(f'window.scrollStep={step}')
            started = last_sample = time.monotonic()
            while time.monotonic()-started < seconds:
                await evaluate(f'({SCROLL})(120)')
                if time.monotonic()-last_sample >= 60:
                    await send('HeapProfiler.collectGarbage')
                    sample = await evaluate('({cards:document.querySelectorAll(".danbooru-image-wrapper").length,heap:performance.memory.usedJSHeapSize,resources:nodes[0]._qa.stats(),long:window.longTasks.splice(0)})')
                    sample['dom'] = await send('Memory.getDOMCounters')
                    sample['renderer_memory'] = await asyncio.to_thread(renderer_memory)
                    sample['seconds'] = round(time.monotonic()-started)
                    report['soak'].append(sample)
                    (QA/'scroll-results.json').write_text(json.dumps(report,indent=2), encoding='utf-8')
                    assert sample['cards'] <= 200
                    assert sample['resources']['idleBytes'] <= 32*1024**2
                    assert all(p['private'] < 2*1024**3 for p in sample['renderer_memory'] or [])
                    print(json.dumps(sample), flush=True)
                    last_sample = time.monotonic()
            screenshot = await send('Page.captureScreenshot', {'format': 'png'})
            (QA/'native-soak.png').write_bytes(base64.b64decode(screenshot['data']))
            report['checks'].append(f'{seconds}-second native browser soak without network inspector completed')
            assert not report['errors'], report['errors']
            await send('Browser.close')
    finally:
        if socket and not socket.closed:
            await socket.close()
        try:
            await asyncio.to_thread(process.wait, timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()


async def main():
    errors, checks, requests = [], [], []
    image_delay = .015
    active_images = 0
    peak_images = 0
    thumbs = []
    for height in [180, 300, 470, 900]:
        out = io.BytesIO()
        image = Image.new('RGB', (300, height), '#547c83')
        image.thumbnail((384, 384))
        image.save(out, 'JPEG')
        thumbs.append(out.getvalue())
    script = (ROOT / 'js/danbooru_gallery/danbooru_gallery.js').read_text(encoding='utf-8')
    needle = '\n                initializeApp();'
    assert script.count(needle) == 1
    script = script.replace(needle, '''
                this._qa={viewport,selectedPosts,prepareSelectionForQueue,
                    stats:()=>({blobs:galleryThumbnails.entries.size,active:galleryThumbnails.active,
                        bytes:[...galleryThumbnails.entries.values()].reduce((n,e)=>n+e.bytes,0),
                        idleBytes:[...galleryThumbnails.entries.values()].filter(e=>!e.users.size).reduce((n,e)=>n+e.bytes,0),
                        recycledCards:recycledCards.length,timers:timers.size}),
                    count:()=>posts.length,loading:()=>isLoading,
                    seed:data=>{endOfResults=true;posts.push(...data);data.forEach((post)=>renderPost(post,Math.floor((100000-post.id)/42)+1));}};
                initializeApp();''')
    index = INDEX.replace('[new Node(1),new Node(2)]', '[new Node(1)]').replace('Local favorites QA', 'Scroll QA')
    index = index.replace('node.onNodeCreated();', '''node.onNodeCreated();
        node.widgets.find(w=>w.name==='selection_data').value=JSON.stringify({selections:[{
            post_id:'saved-workflow-post',prompt:'saved workflow prompt',source_site:'danbooru',
            image_url:'https://cdn.donmai.us/saved-workflow.jpg'}]});''')

    async def handler(request):
        nonlocal active_images, peak_images
        path = request.path
        requests.append(path)
        if path == '/':
            return web.Response(text=index, content_type='text/html')
        if path == '/scripts/ui.js':
            return web.Response(text=UI_JS, content_type='text/javascript')
        if path == '/scripts/app.js':
            return web.Response(text='export const app={registerExtension:e=>window.extension=e,queuePrompt:async()=>{}};', content_type='text/javascript')
        if path == '/js/danbooru_gallery/danbooru_gallery.js':
            return web.Response(text=script, content_type='text/javascript')
        if path.startswith('/js/'):
            return web.FileResponse(ROOT / path[1:])
        name = path.removeprefix('/danbooru_gallery/')
        if name in ('offline/thumbnail', 'image_proxy'):
            active_images += 1
            peak_images = max(peak_images, active_images)
            try:
                await asyncio.sleep(image_delay)
                i = int(request.query['url'].split('fixture_')[1].split('.')[0])
                return web.Response(body=thumbs[i % 4], content_type='image/jpeg', headers={'Cache-Control': 'private, no-store'})
            finally:
                active_images -= 1
        if name == 'posts':
            tag = request.query.get('search[tags]', '')
            if tag == 'fail':
                return web.Response(status=503, text='fixture failure')
            await asyncio.sleep(.35 if tag == 'slow' else .005)
            start = 100001-int(request.query['before_id']) if request.query.get('before_id') else (int(request.query.get('page', 1))-1)*42
            data = posts(start, min(42, max(0, 252-start)), tag or '1girl blue_eyes', request.query.get('source','danbooru'))
            return web.json_response(data, headers={'X-Gallery-Source': 'online'})
        values = {
            'favorites': {'success': True, 'favorites': []},
            'ui_settings': {'success': True, 'settings': {'source_site': 'danbooru', 'tooltip_enabled': True, 'multi_select_enabled': True}},
            'offline/status': {'mode': 'auto', 'paused': False, 'scope': 'fixture'},
            'user_auth': {'has_auth': False}, 'language': {'language': 'zh'},
            'blacklist': {'blacklist': []}, 'filter_tags': {'filter_tags': [], 'enabled': False},
            'translate_tags_batch': {'success': True, 'translations': {}},
            'check_network': {'connected': True},
        }
        if 'autocomplete' in name or name == 'search_chinese':
            return web.json_response([])
        return web.json_response(values.get(name, {'success': True}))

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    server = TestServer(app)
    await server.start_server()
    report = {'checks': checks, 'errors': errors, 'soak': []}
    try:
        if os.environ.get('GALLERY_NATIVE_SOAK') == '1':
            await native_soak(server.make_url('/'), report)
            return
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(str(QA/'browser-profile'),
                executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True,
                viewport={'width': 1300, 'height': 1100}, permissions=['clipboard-read','clipboard-write'])
            page = context.pages[0]
            page.set_default_timeout(12000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            await context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(str(server.make_url('/'))) else route.abort())
            await page.add_init_script("window.longTasks=[];new PerformanceObserver(l=>{window.longTasks.push(...l.getEntries().map(e=>e.duration));if(window.longTasks.length>1000)window.longTasks.splice(0,500);}).observe({type:'longtask'});")
            cdp = await context.new_cdp_session(page)
            try:
                await page.goto(str(server.make_url('/')))
                await page.wait_for_function('window.ready && nodes[0]._qa.count()>=42 && !nodes[0]._qa.loading()')
                await page.wait_for_function("document.querySelector('.danbooru-image-grid .danbooru-thumbnail')?.dataset.loaded==='true'")
                assert await page.evaluate("""[...document.querySelectorAll('.danbooru-thumbnail')]
                    .filter(c => c.dataset.loaded === 'true')
                    .every(c => c.getContext('2d').getContextAttributes().willReadFrequently)""")
                assert await page.locator('.danbooru-pagination-controls').evaluate("e => getComputedStyle(e).backdropFilter === 'none'")
                checks.append('static thumbnails use software canvases and pagination has no backdrop blur')
                await page.evaluate('nodes[0]._qa.prepareSelectionForQueue()')
                restored = await page.evaluate("JSON.parse(nodes[0].widgets.find(w=>w.name==='selection_data').value).selections")
                assert restored[0]['post_id'] == 'saved-workflow-post' and restored[0]['prompt'] == 'saved workflow prompt'
                checks.append('initial list loading preserves serialized workflow selections')
                first = page.locator('.danbooru-image-wrapper .danbooru-thumbnail').first
                await first.click()
                await page.locator('.danbooru-image-wrapper .danbooru-thumbnail').nth(1).click()
                ids = await page.evaluate('()=>[...nodes[0]._qa.selectedPosts.values()].map(p=>String(p.id))')
                assert len(ids) == 2, ids
                # Exercise real continuation requests first.
                for _ in range(8):
                    await page.evaluate('()=>{const g=nodes[0]._qa.viewport.scroller;g.scrollTop=g.scrollHeight;}')
                    await asyncio.sleep(.15)
                assert await page.evaluate('nodes[0]._qa.count()') == 252
                assert await page.locator('.danbooru-image-wrapper.selected').count() == 0
                await page.evaluate('nodes[0]._qa.prepareSelectionForQueue()')
                selected = await page.evaluate("JSON.parse(nodes[0].widgets.find(w=>w.name==='selection_data').value).selections")
                assert [p['post_id'] for p in selected] == ids, selected
                checks.append('real pagination and offscreen multi-selection retain queue output')

                visible_id = await page.evaluate('String(nodes[0]._qa.viewport.range.first.post.id)')
                recycled_image = page.locator(f'.danbooru-image-wrapper[data-post-id="{visible_id}"] .danbooru-thumbnail')
                await recycled_image.click()
                await page.evaluate('nodes[0]._qa.prepareSelectionForQueue()')
                recycled_selection = await page.evaluate("JSON.parse(nodes[0].widgets.find(w=>w.name==='selection_data').value).selections")
                assert len(recycled_selection) == 3 and recycled_selection[-1]['post_id'] == visible_id
                assert recycled_selection[-1]['image_url'].endswith(f'original_{100000-int(visible_id)}.jpg')
                await recycled_image.click()
                checks.append('recycled card actions target the new post rather than the previous occupant')

                await page.evaluate('()=>{nodes[0]._qa.viewport.scroller.scrollTop=0;}')
                card = page.locator(f'.danbooru-image-wrapper[data-post-id="{ids[0]}"]')
                await card.hover()
                await card.locator('.danbooru-edit-button').click()
                await page.locator('.danbooru-clickable-tag').filter(has_text='blue_eyes').click()
                await page.locator('.danbooru-edit-panel-close-button').click()
                await page.evaluate('()=>{const g=nodes[0]._qa.viewport.scroller;g.scrollTop=g.scrollHeight;}')
                await asyncio.sleep(.15)
                await page.evaluate('nodes[0]._qa.prepareSelectionForQueue()')
                selected = await page.evaluate("JSON.parse(nodes[0].widgets.find(w=>w.name==='selection_data').value).selections")
                assert len(selected) == 2 and 'blue' not in selected[0]['prompt'], selected
                checks.append('edited disabled tags and other selections survive card recycling')

                # Warm a short range and prove remounts reuse blobs despite no-store responses.
                await page.evaluate('()=>{nodes[0]._qa.viewport.scroller.scrollTop=0;}')
                await page.wait_for_function("[...document.querySelectorAll('.danbooru-image-grid .danbooru-thumbnail')].every(i=>i.dataset.loaded==='true')")
                await page.evaluate('()=>{nodes[0]._qa.viewport.scroller.scrollTop=750;}')
                await asyncio.sleep(.4)
                await page.evaluate('()=>{nodes[0]._qa.viewport.scroller.scrollTop=0;}')
                await asyncio.sleep(.4)
                before = requests.count('/danbooru_gallery/offline/thumbnail')
                for position in (750, 0, 750, 0):
                    await page.evaluate('y=>{nodes[0]._qa.viewport.scroller.scrollTop=y;}', position)
                    await asyncio.sleep(.12)
                assert requests.count('/danbooru_gallery/offline/thumbnail') == before
                checks.append('warm nearby backtracking sends zero thumbnail requests')

                await page.evaluate('''async()=>{const {app}=await import('/scripts/app.js');
                    app.canvas={ds:{scale:.05}};nodes[0].onDrawBackground();}''')
                await asyncio.sleep(.6)
                await page.wait_for_function("[...document.querySelectorAll('.danbooru-thumbnail')].every(c=>Math.max(c.width,c.height)<=64&&c.dataset.loaded==='true')")
                for scale in (.8, .2, 1, .05):
                    await page.evaluate('async s=>{const {app}=await import("/scripts/app.js");app.canvas.ds.scale=s;nodes[0].onDrawBackground();}', scale)
                    await asyncio.sleep(.05)
                await asyncio.sleep(.4)
                assert await page.evaluate("[...document.querySelectorAll('.danbooru-thumbnail')].every(c=>Math.max(c.width,c.height)<=64)")
                await page.evaluate('async()=>{const {app}=await import("/scripts/app.js");app.canvas.ds.scale=1;nodes[0].onDrawBackground();}')
                await page.wait_for_function("[...document.querySelectorAll('.danbooru-thumbnail')].some(c=>Math.max(c.width,c.height)>64)")
                assert requests.count('/danbooru_gallery/offline/thumbnail') == before
                checks.append('zoom downscales decoded pixels, debounces rapid changes and restores detail without refetching')

                await page.locator('.danbooru-image-wrapper .danbooru-thumbnail').first.hover()
                await page.locator('.danbooru-tag-tooltip').wait_for(state='visible')
                translations = requests.count('/danbooru_gallery/translate_tags_batch')
                await page.evaluate(SCROLL, 120)
                assert not await page.locator('.danbooru-tag-tooltip').is_visible()
                assert requests.count('/danbooru_gallery/translate_tags_batch') <= translations+1
                checks.append('scrolling suppresses tooltip rendering and translation requests')

                anchor = await page.evaluate('()=>{const v=nodes[0]._qa.viewport;return {id:v.range.first.post.id,offset:v.scroller.scrollTop-v.range.first.top};}')
                await page.evaluate('nodes[0].onResize([900,760])')
                await asyncio.sleep(.1)
                after = await page.evaluate('id=>{const v=nodes[0]._qa.viewport,e=v.entries.find(e=>e.post.id===id);return v.scroller.scrollTop-e.top;}', anchor['id'])
                assert abs(after-anchor['offset']) < 2
                checks.append('resize preserves the visible post anchor')

                # Populate metadata without asking a real site for thousands of pages.
                for start in range(252, 10000, 420):
                    await page.evaluate('data=>nodes[0]._qa.seed(data)', posts(start, min(420,10000-start)))
                await asyncio.sleep(.3)
                await page.evaluate('window.longTasks.length=0')
                gaps = await page.evaluate(SCROLL, 600)
                sizes = await page.evaluate("({cards:document.querySelectorAll('.danbooru-image-wrapper').length,posts:nodes[0]._qa.count(),long:window.longTasks})")
                assert sizes['posts'] == 10000 and sizes['cards'] <= 200, sizes
                report['ten_thousand'] = {**sizes, 'frame_p95_ms': sorted(gaps)[int(len(gaps)*.95)], 'frame_max_ms': max(gaps)}
                checks.append('10,000 mixed-aspect posts keep mounted cards bounded')
                await page.screenshot(path=str(QA/'gallery-10000.png'))

                # Destroyed gallery nodes must release document/window listeners and observers.
                await page.evaluate('()=>{window.NodeType=nodes[0].constructor;}')
                await cdp.send('HeapProfiler.collectGarbage')
                baseline = await cdp.send('Memory.getDOMCounters')
                for _ in range(20):
                    await page.evaluate('()=>{window.tempNode=new NodeType(2);tempNode.onNodeCreated();tempNode.onResize([690,700]);}')
                    await asyncio.sleep(.12)
                    await page.evaluate('()=>{tempNode.onRemoved();tempNode.host.remove();window.tempNode=null;}')
                await asyncio.sleep(.5)
                await cdp.send('HeapProfiler.collectGarbage')
                end = await cdp.send('Memory.getDOMCounters')
                report['lifecycle'] = {'before': baseline, 'after': end}
                assert end['jsEventListeners'] <= baseline['jsEventListeners']+10, (baseline,end)
                checks.append('20 node create/remove cycles do not accumulate listeners')

                seconds = int(os.environ.get('GALLERY_SOAK_SECONDS', '0'))
                await page.evaluate('step=>window.scrollStep=step', int(os.environ.get('GALLERY_SCROLL_STEP', '32')))
                started = last_sample = time.monotonic()
                while time.monotonic()-started < seconds:
                    await page.evaluate(SCROLL, 120)
                    if time.monotonic()-last_sample >= 60:
                        await cdp.send('HeapProfiler.collectGarbage')
                        sample = await page.evaluate("({cards:document.querySelectorAll('.danbooru-image-wrapper').length,heap:performance.memory.usedJSHeapSize,scrollTop:nodes[0]._qa.viewport.scroller.scrollTop,long:window.longTasks.splice(0)})")
                        sample['seconds'] = round(time.monotonic()-started)
                        sample['resources'] = await page.evaluate('nodes[0]._qa.stats()')
                        sample['dom'] = await cdp.send('Memory.getDOMCounters')
                        sample['renderer_memory'] = await asyncio.to_thread(renderer_memory)
                        assert sample['cards'] <= 200
                        assert sample['resources']['blobs'] <= 128+sample['cards']
                        assert all(p['private'] < 2*1024**3 for p in sample['renderer_memory'] or [])
                        report['soak'].append(sample)
                        (QA/'scroll-results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
                        print(json.dumps({'soak_seconds':sample['seconds'],'cards':sample['cards'],'heap':sample['heap']}),flush=True)
                        last_sample = time.monotonic()
                if seconds:
                    checks.append(f'{seconds}-second continuous scroll soak completed')

                if os.environ.get('GALLERY_MEMORY_TRACE') == '1':
                    trace = []
                    complete = asyncio.Event()
                    cdp.on('Tracing.dataCollected', lambda data: trace.extend(data['value']))
                    cdp.on('Tracing.tracingComplete', lambda _: complete.set())
                    await cdp.send('Tracing.start', {'categories': 'disabled-by-default-memory-infra'})
                    await cdp.send('Tracing.requestMemoryDump', {'deterministic': True, 'levelOfDetail': 'detailed'})
                    await cdp.send('Tracing.end')
                    await asyncio.wait_for(complete.wait(), 10)
                    (QA/'native-memory-trace.json').write_text(json.dumps(trace), encoding='utf-8')
                    report['before_memory_pressure'] = await asyncio.to_thread(renderer_memory)
                    await cdp.send('Memory.simulatePressureNotification', {'level': 'critical'})
                    await asyncio.sleep(2)
                    report['after_memory_pressure'] = await asyncio.to_thread(renderer_memory)
                    if os.environ.get('GALLERY_HEAP_SNAPSHOT') == '1':
                        with (QA/'renderer.heapsnapshot').open('w', encoding='utf-8') as snapshot:
                            cdp.on('HeapProfiler.addHeapSnapshotChunk', lambda data: snapshot.write(data['chunk']))
                            await cdp.send('HeapProfiler.takeHeapSnapshot', {'captureNumericValue': True})

                # A superseded query must never replace the new result set.
                search = page.locator('.danbooru-search-container input')
                await search.fill('slow'); await search.press('Enter')
                await asyncio.sleep(.025)
                await search.fill('fast'); await search.press('Enter')
                await asyncio.sleep(.6)
                assert await page.evaluate("nodes[0]._qa.viewport.entries.every(e=>e.post.tag_string==='fast')")
                checks.append('late query responses cannot overwrite the current search')

                await search.fill('fail'); await search.press('Enter')
                await asyncio.sleep(.2)
                assert await page.evaluate("nodes[0]._qa.viewport.entries.every(e=>e.post.tag_string==='fast')")
                checks.append('failed replacement search preserves the previous view')

                image_delay = .5
                await search.fill(''); await search.press('Enter')
                await page.reload()
                await page.wait_for_function('window.ready && nodes[0]._qa.count()>0')
                await asyncio.sleep(.1)
                skeleton = await page.evaluate("[...document.querySelectorAll('.danbooru-image-wrapper')].map(c=>({height:c.clientHeight,placeholder:c.querySelector('.danbooru-thumbnail-placeholder')?.style.display}))")
                assert skeleton and all(c['height'] > 0 for c in skeleton)
                assert any(c['placeholder'] == 'flex' for c in skeleton)
                checks.append('slow thumbnails retain nonzero geometry and visible placeholders')
                await page.screenshot(path=str(QA/'slow-thumbnails.png'))
                assert not errors, errors
            except BaseException:
                await page.screenshot(path=str(QA/'scroll-failure.png'))
                raise
            finally:
                report['server_peak_image_handlers'] = peak_images
                await context.close()
    finally:
        (QA/'scroll-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        await server.close()
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    asyncio.run(main())
