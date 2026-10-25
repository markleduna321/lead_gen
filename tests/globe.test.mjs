import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createCanvas, loadImage } from '@napi-rs/canvas';
import { SphereGeometry } from 'three';
import { FEATURED, MOBILE_FEATURED, SPIN, regionVector, facingAt, shouldIgnite,
    initialRotation, pinOpacity, setRegionProgress, GlobeSequence } from '../frontend/globe-state.mjs';

const assets = new URL('../static/globe/', import.meta.url);
const regions = JSON.parse(fs.readFileSync(new URL('regions.json', assets), 'utf8'));
const close = (a, b) => assert.ok(Math.abs(a - b) < 0.00001, `${a} should equal ${b}`);

test('coordinates agree with Three.js sphere UVs and north-up equirectangular maps', () => {
    const sphere = new SphereGeometry(1, 64, 32);
    const uv = sphere.attributes.uv;
    const position = sphere.attributes.position;
    for (let i = 0; i < uv.count; i++) {
        if (uv.getY(i) === 0 || uv.getY(i) === 1) continue;
        const vector = regionVector((uv.getY(i) - 0.5) * 180, (uv.getX(i) - 0.5) * 360);
        vector.forEach((v, axis) => close(v, position.array[i * 3 + axis]));
    }
    sphere.dispose();
});

test('all ten countries including the UK ignite on the front, never the back', () => {
    for (const region of regions) {
        const r = { ...region, state: 'idle' };
        const rotation = initialRotation(r);
        assert.equal(shouldIgnite(r, rotation), true, r.name);
        assert.equal(shouldIgnite(r, rotation + Math.PI), false, r.name);
        close(facingAt(r, rotation), Math.cos(r.lat * Math.PI / 180));
        assert.equal(shouldIgnite({ ...r, state: 'burnt' }, rotation), false);
    }
});

test('per-country progress updates never affect another country or metadata', () => {
    const data = new Float32Array(256 * 2 * 4);
    data[(256 + 1) * 4] = 0.4;
    setRegionProgress(data, 1, 0.5);
    setRegionProgress(data, 2, 0.8);
    close(data[7], 0.5);
    close(data[11], 0.8);
    close(data[(256 + 1) * 4], 0.4);
    setRegionProgress(data, 1, 0);
    close(data[11], 0.8);
    assert.throws(() => setRegionProgress(data, 0, 1), RangeError);
    assert.throws(() => setRegionProgress(data, 1, NaN), RangeError);
});

test('concurrent burns progress independently, hold, fade, then reset cleanly', () => {
    const pair = [regions[0], { ...regions[0], id: 2 }];
    const sequence = new GlobeSequence(pair, t => t);
    const angle = initialRotation(pair[0]);
    sequence.step(1, angle);
    assert.ok(sequence.regions.every(r => r.state === 'burning' && r.progress > 0 && r.progress < 1));
    sequence.step(1.7, angle);
    assert.ok(sequence.regions.every(r => r.state === 'burnt' && r.progress === 1));
    sequence.step(2.5, angle);
    assert.equal(sequence.opacity, 1);
    sequence.step(0.8, angle);
    close(sequence.opacity, 0.5);
    // Fade the result, rather than running the paper burn backwards.
    assert.ok(sequence.regions.every(r => r.progress === 1));
    sequence.step(0.81, angle);
    assert.equal(sequence.opacity, 1);
    assert.ok(sequence.regions.every(r => r.state === 'idle' && r.progress === 0 && r.elapsed === 0));
});

test('desktop and mobile sequences both finish and restart within two turns', () => {
    for (const featured of [FEATURED, MOBILE_FEATURED]) {
        const sequence = new GlobeSequence(regions.filter(r => featured.includes(r.iso2)), t => t);
        let resetSeen = false;
        let restarted = false;
        let angle = initialRotation(sequence.regions[0]);
        for (let tick = 0; tick < 7200; tick++) {
            angle += SPIN / 60;
            sequence.step(1 / 60, angle);
            if (sequence.resetting) resetSeen = true;
            if (resetSeen && !sequence.resetting) { restarted = true; break; }
        }
        assert.equal(resetSeen, true);
        assert.equal(restarted, true);
    }
});

test('reduced-motion snapshot completes regions and pin fades hide far-side flags', () => {
    const sequence = new GlobeSequence(regions, t => t);
    sequence.finish();
    assert.ok(sequence.regions.every(r => r.progress === 1 && r.state === 'burnt'));
    assert.equal(pinOpacity(-1), 0);
    assert.equal(pinOpacity(0.05), 0);
    assert.equal(pinOpacity(0.3), 1);
    assert.ok(pinOpacity(0.15) > 0 && pinOpacity(0.15) < 1);
    sequence.reset();
    assert.ok(sequence.regions.every(r => r.progress === 0));
});

for (const [suffix, width] of [['', 4096], ['-mobile', 2048]]) {
    test(`baked ${width}px country IDs are exact, align with land, and include mainland Australia`, async () => {
        const image = await loadImage(fs.readFileSync(new URL(`region-id${suffix}.png`, assets)));
        const map = await loadImage(fs.readFileSync(new URL(`world-mask${suffix}.png`, assets)));
        assert.equal(image.width, width);
        assert.equal(image.height, width / 2);
        const canvas = createCanvas(width, width / 2);
        const ctx = canvas.getContext('2d');
        ctx.drawImage(image, 0, 0);
        const ids = ctx.getImageData(0, 0, width, width / 2).data;
        ctx.clearRect(0, 0, width, width / 2);
        ctx.drawImage(map, 0, 0);
        const land = ctx.getImageData(0, 0, width, width / 2).data;
        const counts = new Map();
        for (let i = 0; i < ids.length; i += 4) {
            const id = ids[i];
            assert.ok(id >= 0 && id <= regions.length);
            assert.equal(ids[i + 1], 0);
            assert.equal(ids[i + 2], 0);
            if (id) {
                counts.set(id, (counts.get(id) || 0) + 1);
                assert.ok(land[i] > 0, `Country ${id} spilled into the ocean at pixel ${i / 4}`);
            }
        }
        assert.equal(counts.size, 10);
        const pixel = (lon, lat) => ids[(Math.floor((90 - lat) / 180 * image.height) * width
            + Math.floor((lon + 180) / 360 * width)) * 4];
        for (const [lon, lat, id] of [[133, -25, 3], [-98, 39, 8], [79, 23, 2],
            [-53, -10, 4], [30, 26, 9], [0, 0, 0], [81, 7, 0]]) {
            assert.equal(pixel(lon, lat), id);
        }
        const australia = regions.find(r => r.iso2 === 'au');
        assert.equal(australia.name, 'Australia');
        assert.ok(australia.radius > 0.2);
    });
}

test('same-origin fallback, flags, license notices and browser bundle are shipped', async () => {
    for (const region of regions) {
        assert.match(fs.readFileSync(new URL(`flags/${region.iso2}.svg`, assets), 'utf8'), /<svg/);
    }
    const image = await loadImage(fs.readFileSync(new URL('globe-fallback.png', assets)));
    assert.equal(image.width, 600);
    assert.equal(image.height, 600);
    assert.match(fs.readFileSync(new URL('THIRD-PARTY-NOTICES.txt', assets), 'utf8'), /GSAP/);
    assert.ok(fs.statSync(new URL('globe.js', assets)).size < 650_000);
});
