import test from 'node:test';
import assert from 'node:assert/strict';
import { layoutCards, cardsInRange } from '../js/danbooru_gallery/gallery_viewport.js';
import { ThumbnailPool } from '../js/danbooru_gallery/gallery_thumbnails.js';

test('10,000 mixed aspect cards have bounded viewport ranges and stable append positions', () => {
    const entries = Array.from({ length: 10000 }, (_, index) => ({ index, post: {
        id: index, image_width: 300, image_height: [180, 300, 470, 900][index % 4],
    } }));
    layoutCards(entries.slice(0, 1000), 790);
    const previous = entries.slice(0, 1000).map(e => [e.left, e.top, e.height]);
    const { columns, height } = layoutCards(entries, 790);
    assert.deepEqual(entries.slice(0, 1000).map(e => [e.left, e.top, e.height]), previous);
    for (let top = 0; top < height; top += 1700) {
        const range = cardsInRange(columns, top - 1000, top + 2000);
        assert.ok(range.length < 200);
        assert.deepEqual(new Set(range), new Set(entries.filter(e => e.bottom > top - 1000 && e.top < top + 2000)));
    }
    for (const column of columns) for (let i = 1; i < column.length; i++) {
        assert.ok(column[i].top >= column[i - 1].bottom + 4.99);
    }
});

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const state = () => ({ attempts: 0, retryAt: 0 });
const makePool = options => new ThumbnailPool({ decode: async () => ({ width: 1, height: 1, close() {} }), ...options });

test('missing or invalid dimensions retain finite placeholder geometry', () => {
    const entries = [
        { post: { image_width: 0, image_height: 300 } },
        { post: {} },
        { post: { image_width: 300, image_height: -1 } },
    ];
    const { height } = layoutCards(entries, 790);
    assert.ok(Number.isFinite(height) && height > 0);
    for (const entry of entries) assert.ok(Number.isFinite(entry.height) && entry.height > 0);
});

test('thumbnail queue prioritizes visible requests, deduplicates and reuses ready blobs', async () => {
    const original = globalThis.fetch;
    const requests = [];
    const pending = [];
    globalThis.fetch = (url, options) => new Promise((resolve, reject) => {
        requests.push(url);
        pending.push(() => resolve(new Response(new Blob(['image']))));
        options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
    const pool = makePool({ concurrency: 1 });
    try {
        const back = pool.acquire('back', '/back', 2, () => {}, state());
        const front = pool.acquire('front', '/front', 0, () => {}, state());
        const duplicate = pool.acquire('front', '/front', 0, () => {}, state());
        await tick();
        assert.deepEqual(requests, ['/front']);
        pending.shift()();
        await tick();
        assert.deepEqual(requests, ['/front', '/back']);
        pending.shift()();
        await tick();
        front.release(); duplicate.release(); back.release();
        let ready;
        const revisit = pool.acquire('front', '/front', 0, e => { ready = e.status; }, state());
        assert.equal(ready, 'ready');
        await tick();
        assert.equal(requests.length, 2);
        revisit.release();
    } finally { pool.invalidate(''); globalThis.fetch = original; }
});

test('leaving the viewport aborts orphan requests without spending retry budget', async () => {
    const original = globalThis.fetch;
    let signal;
    globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
        signal = options.signal;
        signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
    const pool = makePool();
    const retry = state();
    try {
        const handle = pool.acquire('one', '/one', 0, () => {}, retry);
        await tick();
        handle.release();
        await tick();
        assert.equal(signal.aborted, true);
        assert.equal(pool.active, 0);
        assert.equal(retry.attempts, 0);
        assert.equal(pool.entries.size, 0);
    } finally { pool.invalidate(''); globalThis.fetch = original; }
});

test('idle thumbnails obey both item and byte limits', async () => {
    const original = globalThis.fetch;
    globalThis.fetch = async () => new Response(new Blob(['123456']));
    const pool = makePool({ maxItems: 2, maxBytes: 10 });
    try {
        for (let i = 0; i < 6; i++) {
            const handle = pool.acquire(String(i), '/' + i, 0, () => {}, state());
            await tick(); handle.release();
        }
        assert.equal(pool.entries.size, 1);
        assert.ok(pool.entries.has('5'));
    } finally { pool.invalidate(''); globalThis.fetch = original; }
});

test('decoded pixels count toward the budget and eviction closes the bitmap', async () => {
    const original = globalThis.fetch;
    globalThis.fetch = async () => new Response(new Blob(['small compressed file']));
    let closed = 0;
    const pool = new ThumbnailPool({ maxBytes: 100, decode: async () => ({ width: 10, height: 10, close() { closed++; } }) });
    try {
        const handle = pool.acquire('pixels', '/pixels', 0, () => {}, state());
        await tick();
        assert.equal(pool.entries.get('pixels').bytes, 400);
        handle.release();
        assert.equal(pool.entries.size, 0);
        assert.equal(closed, 1);
    } finally { pool.invalidate(''); globalThis.fetch = original; }
});

test('a bitmap decoded after its card leaves is closed instead of cached', async () => {
    const original = globalThis.fetch;
    globalThis.fetch = async () => new Response(new Blob(['image']));
    let finishDecode, closed = 0;
    const retry = state();
    const pool = new ThumbnailPool({ decode: () => new Promise(resolve => { finishDecode = resolve; }) });
    try {
        const handle = pool.acquire('gone', '/gone', 0, () => {}, retry);
        await tick();
        handle.release();
        finishDecode({ width: 10, height: 10, close() { closed++; } });
        await tick();
        assert.equal(closed, 1);
        assert.equal(pool.entries.size, 0);
        assert.equal(pool.active, 0);
        assert.equal(retry.attempts, 0);
    } finally { pool.invalidate(''); globalThis.fetch = original; }
});

test('releasing an invalidated subscriber cannot delete the replacement request', async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
    const pool = makePool();
    try {
        const old = pool.acquire('scope:image', '/image', 0, () => {}, state());
        await tick();
        pool.invalidate('scope:');
        const replacement = pool.acquire('scope:image', '/image', 0, () => {}, state());
        old.release();
        assert.equal(pool.entries.size, 1);
        await tick();
        replacement.release();
        await tick();
        assert.equal(pool.active, 0);
    } finally { pool.invalidate(''); globalThis.fetch = original; }
});

test('remounting a failed thumbnail does not reset its attempt limit', async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => { calls++; throw new Error('offline'); };
    const pool = makePool();
    const retry = state();
    try {
        for (let i = 0; i < 6; i++) {
            retry.retryAt = 0;
            const handle = pool.acquire('one', '/one', 0, () => {}, retry, false);
            await tick(); handle.release();
        }
        assert.equal(calls, 3);
        assert.equal(retry.attempts, 3);
    } finally { pool.invalidate(''); globalThis.fetch = original; }
});
