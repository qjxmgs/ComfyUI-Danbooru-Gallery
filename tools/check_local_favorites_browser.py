"""Browser check of the real gallery extension with isolated routes and fixture posts.

Requires Playwright; GALLERY_QA_DIR selects all databases, profiles and evidence.
The tiny Comfy node/app shell avoids changing an active ComfyUI workflow.
"""
import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image, ImageDraw
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
QA = Path(os.environ['GALLERY_QA_DIR'])
SPEC = importlib.util.spec_from_file_location('local_favorites', ROOT / 'py/danbooru_gallery/local_favorites.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

UI_JS = r'''
export function $el(selector, props = {}, children = []) {
    if (Array.isArray(props)) { children = props; props = {}; }
    const [tag, ...classes] = selector.split('.');
    const element = document.createElement(tag);
    element.classList.add(...classes);
    for (const [key, value] of Object.entries(props)) {
        if (key === 'style' || key === 'dataset') Object.assign(element[key], value);
        else if (key === 'className') { if (value) element.classList.add(...value.split(' ').filter(Boolean)); }
        else if (key === 'parent') value.appendChild(element);
        else if (key.startsWith('data-')) element.setAttribute(key, value);
        else element[key] = value;
    }
    element.append(...children);
    return element;
}
'''
INDEX = '''<!doctype html><html><head><meta charset="utf-8"><style>
:root { --comfy-menu-bg:#292929; --comfy-input-bg:#202020; --input-text:#ddd;
--comfy-input-text:#ddd; --input-border-color:#555; --border-color:#555; color-scheme:dark; }
body { font:14px Arial,sans-serif; background:#171717; color:#ddd; margin:20px; }
#nodes { display:flex; gap:16px; align-items:flex-start; } section { position:relative; border:1px solid #555; border-radius:8px; padding:12px; }
</style></head><body><h2>Danbooru Gallery · Local favorites QA</h2><div id="nodes"></div>
<script type="module">
import {globalMultiLanguageManager as language} from '/js/global/multi_language.js';
import {danbooruTranslations} from '/js/global/translations/danbooru_translations.js';
language.registerTranslations('danbooru',danbooruTranslations);
await import('/js/danbooru_gallery/danbooru_gallery.js');
class Node {
  constructor(id) { this.id=id; this.widgets=[]; this.mode=0; this.host=document.createElement('section');
    this.host.dataset.node=String(id); document.querySelector('#nodes').append(this.host); }
  setSize(size) { this.size=size; }
  addWidget(type,name,value,callback) { const widget={type,name,value,callback}; this.widgets.push(widget); return widget; }
  addDOMWidget(name,type,element) { this.host.append(element); }
}
await window.extension.beforeRegisterNodeDef(Node,{name:'DanbooruGalleryNode'});
window.nodes = [new Node(1),new Node(2)];
for (const node of window.nodes) { node.onNodeCreated(); node.onResize([690,700]); }
window.ready=true;
</script></body></html>'''


async def main():
    QA.mkdir(parents=True, exist_ok=True)
    requests = []
    image = Image.new('RGB', (384, 384), '#34495e')
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((65, 55, 319, 329), radius=30, fill='#68a894')
    draw.ellipse((125, 105, 259, 239), fill='#e5d1ad')
    output = io.BytesIO()
    image.save(output, 'JPEG')
    thumbnail = output.getvalue()
    store = MODULE.LocalFavoritesStore(QA / 'browser-favorites.sqlite')
    # The run directory must be fresh: never clear a user's collection.
    assert not store.ids('danbooru') and not store.ids('gelbooru')
    async def cached(request, source, post):
        return None if str(post['id']) == '3' else thumbnail
    async def complete(request, source, post):
        return post, thumbnail
    routes = web.RouteTableDef()
    MODULE.LocalFavorites(routes, store, cached, complete, lambda tag: {'女孩': '1girl'}.get(tag, tag))
    @web.middleware
    async def record(request, handler):
        requests.append(str(request.rel_url))
        if request.path.endswith('/favorites/add'):
            await asyncio.sleep(0.15)
        return await handler(request)
    app = web.Application(middlewares=[record])
    app.add_routes(routes)

    async def shell(request):
        if request.path == '/':
            return web.Response(text=INDEX, content_type='text/html')
        if request.path == '/scripts/ui.js':
            return web.Response(text=UI_JS, content_type='text/javascript')
        if request.path == '/scripts/app.js':
            return web.Response(text='export const app={registerExtension: e => window.extension=e,queuePrompt:async()=>{}};', content_type='text/javascript')
        raise web.HTTPNotFound()
    app.router.add_get('/', shell)
    app.router.add_get('/scripts/{name}', shell)
    app.router.add_static('/js/', ROOT / 'js')

    async def fixture(request):
        name = request.match_info.get('name', 'logs')
        if name in ('image_proxy', 'offline/thumbnail'):
            return web.Response(body=thumbnail, content_type='image/jpeg')
        if name == 'posts':
            source = request.query.get('source', 'danbooru')
            page = int(request.query.get('page', 1))
            posts = [] if page > 1 or request.query.get('before_id') else [dict(
                id=i, source_site=source, tag_string='1girl blue_eyes', tag_string_general='1girl blue_eyes',
                tag_string_character='', tag_string_copyright='', tag_string_artist='', tag_string_meta='',
                preview_file_url='https://cdn.donmai.us/fixture.jpg', file_url='https://cdn.donmai.us/original.jpg',
                image_width=384, image_height=384, md5='fixture', rating='g', created_at='2026-11-14T00:00:00Z',
                _tag_categories_complete=True, _tag_categories_exact=True,
            ) for i in (1, 2, 3)]
            return web.json_response(posts, headers={'X-Gallery-Source':'online'})
        values = {
            'ui_settings': {'success':True, 'settings':{'source_site':'danbooru','tooltip_enabled':False,'preload_count':20}},
            'translate_tags_batch': {'success':True,'translations':{}},
            'offline/status': {'mode':'local','paused':True,'scope':'fixture'},
            'user_auth': {'username':'','api_key':'','has_auth':False,'gelbooru_has_auth':False},
            'language': {'language':'zh'}, 'blacklist': {'blacklist':[]},
            'filter_tags': {'filter_tags':[],'enabled':False},
            'check_network': {'connected':True},
        }
        if 'autocomplete' in name or name == 'search_chinese':
            return web.json_response([])
        return web.json_response(values.get(name, {'success':True}))
    app.router.add_route('*','/danbooru_gallery/{name:.*}',fixture)
    app.router.add_post('/danbooru/logs/batch', fixture)
    server = TestServer(app)
    await server.start_server()
    errors, checks = [], []
    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(str(QA / 'browser-profile'),
                executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True,
                viewport={'width':1510,'height':900}, permissions=['clipboard-read','clipboard-write'])
            context.set_default_timeout(12000)
            try:
                page = context.pages[0]
                page.on('pageerror', lambda error: errors.append(str(error)))
                await context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(str(server.make_url('/'))) else route.abort())
                async def click_card_button(button):
                    await button.locator('xpath=ancestor::*[contains(@class,"danbooru-image-wrapper")]').hover()
                    await button.click()
                await page.goto(str(server.make_url('/')))
                await page.wait_for_function('window.ready === true')
                one, two = page.locator('section[data-node="1"]'), page.locator('section[data-node="2"]')
                await one.locator('.danbooru-image-wrapper[data-post-id="1"]').wait_for()
                await two.locator('.danbooru-image-wrapper[data-post-id="1"]').wait_for()
                await click_card_button(one.locator('.danbooru-image-wrapper[data-post-id="1"] .danbooru-favorite-button'))
                await two.locator('.danbooru-image-wrapper[data-post-id="1"] .danbooru-favorite-button.favorited').wait_for()
                assert store.ids('danbooru') == ['1']
                checks.append('no-account offline add and two-node heart sync')
                search = one.locator('.danbooru-search-container input')
                await search.fill('order:rank')
                await one.locator('.danbooru-favorites-button').click()
                await page.wait_for_function("document.querySelector('[data-node=\"1\"] .danbooru-image-wrapper')?._galleryPost?._local_favorite")
                assert await search.input_value() == ''
                assert await one.locator('.danbooru-ranking-button').is_disabled()
                start = len(requests)
                await search.fill('1girl -red_eyes')
                await search.press('Enter')
                await click_card_button(one.locator('.danbooru-copy-prompt-button'))
                await asyncio.sleep(0.5)
                forbidden = [p for p in requests[start:] if p.startswith('/danbooru_gallery/posts?') or 'check_network' in p or 'autocomplete' in p]
                assert not forbidden, forbidden
                checks.append('local search and clipboard do not request website posts, network checks, or autocomplete')
                await one.locator('.danbooru-favorites-button').click()
                assert await search.input_value() == 'order:rank'
                checks.append('online query restored on exit')
                await one.locator('.danbooru-image-wrapper[data-post-id="3"]').wait_for()
                await click_card_button(one.locator('.danbooru-image-wrapper[data-post-id="3"] .danbooru-favorite-button'))
                await one.locator('.danbooru-favorites-button').click()
                await search.fill('')
                await search.press('Enter')
                await one.locator('.danbooru-favorite-retry').wait_for()
                await page.screenshot(path=str(QA/'local-favorites-incomplete.png'), full_page=True)
                await click_card_button(one.locator('.danbooru-favorite-retry'))
                await page.wait_for_function("!document.querySelector('[data-node=\"1\"] .danbooru-favorite-retry')")
                checks.append('missing thumbnail stays removable and explicit retry completes save')
                # Switching source inside favorites must use local data with no online preflight.
                source_select = one.locator('select').first
                start=len(requests)
                await source_select.select_option('gelbooru')
                await one.locator('.danbooru-status').filter(has_text='未找到').wait_for()
                assert not any('check_network' in p or p.startswith('/danbooru_gallery/posts?') for p in requests[start:])
                assert not await one.locator('.danbooru-image-wrapper').count()
                await source_select.select_option('danbooru')
                await one.locator('.danbooru-image-wrapper[data-post-id="1"]').wait_for()
                checks.append('source switch isolates same IDs without network checks')
                await click_card_button(one.locator('.danbooru-image-wrapper[data-post-id="1"] .danbooru-favorite-button'))
                await two.locator('.danbooru-image-wrapper[data-post-id="1"] .danbooru-favorite-button:not(.favorited)').wait_for()
                assert store.ids('danbooru') == ['3']
                checks.append('remove refreshes favorites and other gallery node')
                await page.screenshot(path=str(QA/'local-favorites.png'), full_page=True)
                # Reload with browser storage cleared: collection comes from the SQLite database.
                await page.evaluate('localStorage.clear()')
                await page.reload()
                await page.locator('[data-node="1"] .danbooru-image-wrapper[data-post-id="3"] .danbooru-favorite-button.favorited').wait_for()
                checks.append('collection persists across reload and cleared browser storage')
                one = page.locator('section[data-node="1"]')
                await click_card_button(one.locator('.danbooru-image-wrapper[data-post-id="2"] .danbooru-favorite-button'))
                await one.locator('select').first.select_option('gelbooru')
                await one.locator('.danbooru-image-wrapper[data-post-id="2"]').wait_for()
                await asyncio.sleep(0.3)
                assert '2' in store.ids('danbooru') and store.ids('gelbooru') == []
                assert await one.locator('.danbooru-favorite-button.favorited').count() == 0
                checks.append('in-flight add remains attached to its original source after switching')
                assert not errors, errors
            except BaseException:
                await page.screenshot(path=str(QA/'browser-failure.png'), full_page=True)
                (QA/'browser-failure.html').write_text(await page.content(),encoding='utf-8')
                raise
            finally:
                await context.close()
    finally:
        (QA/'browser-results.json').write_text(json.dumps(dict(checks=checks,errors=errors,requests=requests),ensure_ascii=False,indent=2),encoding='utf-8')
        await server.close()
    print(json.dumps(dict(checks=checks,errors=errors), ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
