// All geometry is in the scroll container's CSS coordinates, independent of canvas zoom.
export function layoutCards(entries, width) {
    const gap = 5;
    const count = Math.max(1, Math.floor((width + gap) / 155));
    const columnWidth = (width - gap * (count - 1)) / count;
    const columns = Array.from({ length: count }, () => []);
    const bottoms = Array(count).fill(0);
    for (const entry of entries) {
        const column = bottoms.indexOf(Math.min(...bottoms));
        const sourceRatio = entry.ratio || (entry.post.image_height / entry.post.image_width);
        const ratio = Number.isFinite(sourceRatio) && sourceRatio > 0 ? sourceRatio : 1;
        const height = entry.post.error ? 198 : Math.max(4, (columnWidth - 4) * ratio + 4);
        Object.assign(entry, { top: bottoms[column], bottom: bottoms[column] + height,
            left: column * (columnWidth + gap), width: columnWidth, height });
        columns[column].push(entry);
        bottoms[column] += height + gap;
    }
    return { columns, height: Math.max(0, ...bottoms) };
}

export function cardsInRange(columns, top, bottom) {
    const result = [];
    for (const column of columns) {
        let lo = 0, hi = column.length;
        while (lo < hi) {
            const mid = (lo + hi) >>> 1;
            if (column[mid].bottom <= top) lo = mid + 1;
            else hi = mid;
        }
        for (let i = lo; i < column.length && column[i].top < bottom; i++) result.push(column[i]);
    }
    return result;
}

export class GalleryViewport {
    constructor(scroller, createCard, onRange) {
        this.scroller = scroller;
        this.createCard = createCard;
        this.onRange = onRange;
        this.content = document.createElement('div');
        this.content.className = 'danbooru-virtual-content';
        scroller.append(this.content);
        this.entries = [];
        this.columns = [];
        this.mounted = new Map();
        this.width = 0;
        this.frame = 0;
        this.dirty = true;
        this.destroyed = false;
        this.visible = true;
        this.lastScroll = 0;
        this.pixelRatio = globalThis.devicePixelRatio || 1;
        this.range = { first: null, lastIndex: -1 };
        this.onScroll = () => this.schedule();
        scroller.addEventListener('scroll', this.onScroll, { passive: true });
        this.resizeObserver = new ResizeObserver(() => this.schedule());
        this.resizeObserver.observe(scroller);
        this.visibilityObserver = new IntersectionObserver(([entry]) => {
            this.visible = entry.isIntersecting;
            this.schedule();
        });
        this.visibilityObserver.observe(scroller);
        this.onVisibility = () => this.schedule();
        document.addEventListener('visibilitychange', this.onVisibility);
    }

    append(post, page) {
        this.entries.push({ post, page, index: this.entries.length });
        this.dirty = true;
        this.schedule();
    }

    setPixelRatio(value) {
        if (Math.abs(value - this.pixelRatio) < .001) return;
        this.pixelRatio = value;
        this.schedule();
    }

    refresh(post) {
        for (const entry of this.entries) {
            if (entry.post.id != post.id || entry.post.source_site !== post.source_site) continue;
            entry.post = post;
            this.unmount(entry);
        }
        this.schedule();
    }

    setRatio(entry, width, height) {
        if (entry.post.image_width && entry.post.image_height) return;
        if (!width || !height || entry.ratio === height / width) return;
        this.anchor ||= this.captureAnchor();
        entry.ratio = height / width;
        this.dirty = true;
        this.schedule();
    }

    captureAnchor() {
        const first = this.range.first;
        return first ? { entry: first, offset: this.scroller.scrollTop - first.top } : null;
    }

    clear() {
        for (const entry of this.mounted.keys()) this.unmount(entry);
        this.entries = [];
        this.columns = [];
        this.range = { first: null, lastIndex: -1 };
        this.anchor = null;
        this.content.style.height = '0px';
        this.dirty = true;
        this.schedule();
    }

    unmount(entry) {
        const card = this.mounted.get(entry);
        if (!card) return;
        card._dispose?.();
        card.remove();
        this.mounted.delete(entry);
    }

    schedule() {
        if (!this.destroyed && !this.frame) this.frame = requestAnimationFrame(() => {
            this.frame = 0;
            this.render();
        });
    }

    render() {
        if (this.destroyed || !this.scroller.isConnected) return;
        // Read once, before any card mutations. Never measure individual cards.
        const width = this.scroller.clientWidth - 10;
        const height = this.scroller.clientHeight;
        let top = this.scroller.scrollTop;
        if (width <= 0 || height <= 0 || !this.visible || document.hidden) {
            for (const entry of this.mounted.keys()) this.unmount(entry);
            return;
        }
        if (width !== this.width || this.dirty) {
            const anchor = this.anchor || (width !== this.width ? this.captureAnchor() : null);
            const layout = layoutCards(this.entries, width);
            this.columns = layout.columns;
            this.width = width;
            this.content.style.height = `${layout.height}px`;
            if (anchor && this.entries.includes(anchor.entry)) {
                top = Math.max(0, Math.min(layout.height - height, anchor.entry.top + anchor.offset));
                this.scroller.scrollTop = top;
            }
            this.anchor = null;
            this.dirty = false;
        }
        const visible = cardsInRange(this.columns, top, top + height);
        const wanted = cardsInRange(this.columns, top - height, top + height * 2);
        const wantedSet = new Set(wanted);
        for (const entry of this.mounted.keys()) if (!wantedSet.has(entry)) this.unmount(entry);
        const down = top >= this.lastScroll;
        this.lastScroll = top;
        const priority = entry => entry.bottom > top && entry.top < top + height ? 0
            : (down ? entry.top >= top + height : entry.bottom <= top) ? 1 : 2;
        wanted.sort((a, b) => priority(a) - priority(b) || Math.abs(a.top - top) - Math.abs(b.top - top));
        const started = performance.now();
        let pending = false;
        const fragment = document.createDocumentFragment();
        const added = [];
        for (const entry of wanted) {
            let card = this.mounted.get(entry);
            if (!card) {
                if (performance.now() - started >= 4 && added.length) { pending = true; continue; }
                card = this.createCard(entry.post, entry.page, entry);
                if (!card) continue;
                this.mounted.set(entry, card);
                fragment.append(card);
                added.push(card);
            }
            const geometry = `${entry.left},${entry.top},${entry.width},${entry.height}`;
            if (card._geometry !== geometry) {
                card.style.left = `${entry.left}px`;
                card.style.top = `${entry.top}px`;
                card.style.width = `${entry.width}px`;
                card.style.height = `${entry.height}px`;
                card._geometry = geometry;
            }
            card._priority = priority(entry);
            card._setResolution?.(Math.max(entry.width - 4, entry.height - 4) * this.pixelRatio);
            card._setPriority?.(card._priority);
        }
        this.content.append(fragment);
        for (const card of added) card._mount?.(card._priority);
        const first = visible.reduce((best, entry) => !best || entry.index < best.index ? entry : best, null);
        this.range = { first, lastIndex: visible.reduce((last, entry) => Math.max(last, entry.index), -1) };
        this.onRange(this.range);
        if (pending) this.schedule();
    }

    destroy() {
        this.destroyed = true;
        cancelAnimationFrame(this.frame);
        this.scroller.removeEventListener('scroll', this.onScroll);
        document.removeEventListener('visibilitychange', this.onVisibility);
        this.resizeObserver.disconnect();
        this.visibilityObserver.disconnect();
        this.clear();
        this.content.remove();
    }
}
