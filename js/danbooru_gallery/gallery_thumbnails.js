export const THUMBNAIL_TIERS = [64, 96, 128, 192, 256, 384];
export function thumbnailTier(pixels, current = 0) {
    const index = THUMBNAIL_TIERS.indexOf(current);
    if (index >= 0 && pixels <= current * 1.12 &&
        (index === 0 || pixels > THUMBNAIL_TIERS[index - 1] * .85)) return current;
    return THUMBNAIL_TIERS.find(size => size >= pixels) || 384;
}

async function decodeThumbnail(blob, size, dimensions) {
    let original;
    if (!dimensions.width) {
        original = await createImageBitmap(blob);
        dimensions.width = original.width;
        dimensions.height = original.height;
    }
    const scale = Math.min(1, size / Math.max(dimensions.width, dimensions.height));
    if (original && scale === 1) return original;
    try {
        return await createImageBitmap(original || blob, {
            resizeWidth: Math.max(1, Math.round(dimensions.width * scale)),
            resizeHeight: Math.max(1, Math.round(dimensions.height * scale)), resizeQuality: 'high',
        });
    } finally { original?.close(); }
}

// Shared by gallery nodes: four fetch/decode slots leave room for list/API requests.
export class ThumbnailPool {
    constructor({ concurrency = 4, maxItems = 128, maxBytes = 32 * 1024 * 1024,
        decode = decodeThumbnail } = {}) {
        this.concurrency = concurrency;
        this.maxItems = maxItems;
        this.maxBytes = maxBytes;
        this.decode = decode;
        this.entries = new Map();
        this.active = 0;
        this.clock = 0;
    }

    acquire(key, url, priority, callback, state, autoRetry = true, size = 384) {
        let entry = this.entries.get(key);
        if (!entry) {
            entry = { key, url, users: new Set(), status: 'queued', bytes: 0, bitmap: null,
                state, autoRetry, touched: ++this.clock, controller: null, timer: null,
                blob: null, dimensions: {}, targetSize: size, bitmapSize: 0 };
            this.entries.set(key, entry);
        }
        const user = { priority, callback, size };
        entry.users.add(user);
        this.resize(entry);
        entry.touched = ++this.clock;
        if (entry.status === 'error' && state.attempts < 3 && autoRetry) this.armRetry(entry);
        callback(entry);
        // Defer starting until the viewport has registered all visible cards.
        queueMicrotask(() => this.pump());
        let released = false;
        return {
            size: value => { user.size = value; this.resize(entry); callback(entry); this.pump(); },
            priority: value => {
                if (user.priority === value) return;
                user.priority = value;
                if (value === 0 && entry.status === 'error') this.armRetry(entry);
                this.pump();
            },
            retry: () => {
                if (entry.status === 'loading') return;
                clearTimeout(entry.timer);
                entry.state.attempts = 0;
                entry.state.retryAt = 0;
                entry.bitmap?.close();
                entry.bitmap = null;
                entry.blob = null;
                entry.bytes = 0;
                entry.status = 'queued';
                this.pump();
            },
            release: () => {
                if (released) return;
                released = true;
                entry.users.delete(user);
                entry.touched = ++this.clock;
                if (!entry.users.size) {
                    clearTimeout(entry.timer);
                    if (entry.status === 'loading') entry.controller.abort();
                    if (entry.status !== 'ready' && this.entries.get(key) === entry) {
                        entry.bitmap?.close();
                        this.entries.delete(key);
                    }
                    this.trim();
                } else { this.resize(entry); this.pump(); }
            },
        };
    }

    resize(entry) {
        const size = Math.max(...[...entry.users].map(user => user.size));
        if (!Number.isFinite(size) || size === entry.targetSize) return;
        entry.targetSize = size;
        if (entry.status === 'ready') entry.status = 'queued';
    }

    notify(entry) {
        for (const user of entry.users) user.callback(entry);
    }

    armRetry(entry) {
        clearTimeout(entry.timer);
        if (!entry.users.size || !entry.autoRetry || entry.state.attempts >= 3) return;
        entry.timer = setTimeout(() => {
            if (this.entries.get(entry.key) !== entry || !entry.users.size) return;
            // Offscreen overscan can wait until it becomes visible.
            if (![...entry.users].some(user => user.priority === 0)) return;
            entry.status = 'queued';
            this.pump();
        }, Math.max(0, entry.state.retryAt - Date.now()));
    }

    pump() {
        while (this.active < this.concurrency) {
            const next = [...this.entries.values()].filter(entry => entry.status === 'queued' && entry.users.size)
                .sort((a, b) => Math.min(...[...a.users].map(u => u.priority)) - Math.min(...[...b.users].map(u => u.priority)) || a.touched - b.touched)[0];
            if (!next) return;
            if (next.state.attempts >= 3 || next.state.retryAt > Date.now()) {
                next.status = 'error';
                this.notify(next);
                this.armRetry(next);
                continue;
            }
            void this.load(next);
        }
    }

    async load(entry) {
        this.active++;
        entry.status = 'loading';
        entry.controller = new AbortController();
        this.notify(entry);
        try {
            if (!entry.blob) {
                const response = await fetch(entry.url, { signal: entry.controller.signal });
                if (!response.ok) throw new Error(`thumbnail ${response.status}`);
                entry.blob = await response.blob();
            }
            const blob = entry.blob;
            if (!entry.users.size || this.entries.get(entry.key) !== entry) return;
            const size = entry.targetSize;
            const bitmap = await this.decode(blob, size, entry.dimensions);
            if (!entry.users.size || this.entries.get(entry.key) !== entry) {
                bitmap.close();
                return;
            }
            if (size !== entry.targetSize) {
                bitmap.close();
                entry.status = 'queued';
                return;
            }
            const previous = entry.bitmap;
            entry.bitmap = bitmap;
            entry.bitmapSize = size;
            // Bound decoded pixels, not just the much smaller compressed response.
            entry.bytes = blob.size + bitmap.width * bitmap.height * 4;
            entry.status = 'ready';
            entry.state.attempts = 0;
            entry.state.retryAt = 0;
            this.notify(entry);
            previous?.close();
        } catch (error) {
            if (error.name !== 'AbortError') {
                entry.bitmap?.close();
                entry.bitmap = null;
                entry.bytes = 0;
                entry.state.attempts++;
                entry.status = 'error';
                entry.state.retryAt = Date.now() + 5000 * 2 ** (entry.state.attempts - 1);
                this.notify(entry);
                this.armRetry(entry);
            }
        } finally {
            entry.controller = null;
            this.active--;
            this.trim();
            this.pump();
        }
    }

    invalidate(prefix) {
        for (const [key, entry] of this.entries) {
            if (!key.startsWith(prefix)) continue;
            clearTimeout(entry.timer);
            entry.controller?.abort();
            entry.bitmap?.close();
            this.entries.delete(key);
        }
    }

    trim() {
        const idle = [...this.entries.values()].filter(entry => !entry.users.size && entry.status === 'ready')
            .sort((a, b) => a.touched - b.touched);
        let bytes = idle.reduce((total, entry) => total + entry.bytes, 0);
        let count = idle.length;
        for (const entry of idle) {
            if (count <= this.maxItems && bytes <= this.maxBytes) break;
            entry.bitmap?.close();
            this.entries.delete(entry.key);
            bytes -= entry.bytes;
            count--;
        }
    }
}

export const galleryThumbnails = new ThumbnailPool();
