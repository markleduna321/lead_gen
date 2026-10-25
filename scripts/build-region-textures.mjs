import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createCanvas, loadImage } from '@napi-rs/canvas';
import { geoEquirectangular, geoOrthographic, geoPath, geoCentroid, geoDistance, geoArea } from 'd3-geo';
import { feature } from 'topojson-client';
import countries from 'i18n-iso-countries';
import { FEATURED } from '../frontend/globe-state.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.join(root, 'static', 'globe');
const modules = path.join(root, 'node_modules');
fs.mkdirSync(path.join(output, 'flags'), { recursive: true });
const world = JSON.parse(fs.readFileSync(path.join(modules, 'world-atlas', 'countries-50m.json'), 'utf8'));
const features = feature(world, world.objects.countries).features;
const isoFor = f => countries.numericToAlpha2(String(f.id).padStart(3, '0'))?.toLowerCase();
const byIso = new Map();
for (const f of features) {
    const iso = isoFor(f);
    if (!iso) continue;
    const existing = byIso.get(iso);
    if (!existing) {
        byIso.set(iso, f);
        continue;
    }
    const polygons = geometry => geometry.type === 'MultiPolygon' ? geometry.coordinates : [geometry.coordinates];
    // Natural Earth can have several features with one ISO code (Australia and
    // its territories). Preserve all polygons, not just the final tiny island.
    byIso.set(iso, {
        type: 'Feature',
        properties: geoArea(existing) >= geoArea(f) ? existing.properties : f.properties,
        geometry: { type: 'MultiPolygon', coordinates: [...polygons(existing.geometry), ...polygons(f.geometry)] }
    });
}
const regions = FEATURED.map((iso2, i) => {
    const country = byIso.get(iso2);
    if (!country) throw new Error(`Missing featured country: ${iso2}`);
    const polygons = country.geometry.type === 'MultiPolygon'
        ? country.geometry.coordinates.map(coordinates => ({ type: 'Polygon', coordinates }))
        : [country.geometry];
    const main = polygons.reduce((a, b) => geoArea(a) >= geoArea(b) ? a : b);
    const [lon, lat] = geoCentroid(main);
    const radius = Math.max(...main.coordinates[0].map(p => geoDistance([lon, lat], p)));
    fs.copyFileSync(path.join(modules, 'flag-icons', 'flags', '4x3', `${iso2}.svg`),
        path.join(output, 'flags', `${iso2}.svg`));
    return { id: i + 1, iso2, name: country.properties.name, lat, lon, radius };
});
fs.writeFileSync(path.join(output, 'regions.json'), JSON.stringify(regions, null, 2) + '\n');

function bake(width, suffix) {
    const height = width / 2;
    const projection = geoEquirectangular().fitSize([width, height], { type: 'Sphere' });
    const canvas = createCanvas(width, height);
    const ctx = canvas.getContext('2d');
    const draw = geoPath(projection, ctx);
    const pixels = ctx.createImageData(width, height);
    function mask(country, stroke = false) {
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = '#fff';
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = width / 4096 * 1.5;
        ctx.beginPath();
        draw(country);
        if (stroke) ctx.stroke();
        else ctx.fill();
        return ctx.getImageData(0, 0, width, height).data;
    }
    const collection = { type: 'FeatureCollection', features };
    const land = mask(collection);
    const borders = mask(collection, true);
    for (let i = 0; i < pixels.data.length; i += 4) {
        pixels.data[i] = land[i + 3];
        pixels.data[i + 1] = borders[i + 3];
        pixels.data[i + 3] = 255;
    }
    ctx.putImageData(pixels, 0, 0);
    fs.writeFileSync(path.join(output, `world-mask${suffix}.png`), canvas.toBuffer('image/png'));
    pixels.data.fill(0);
    for (let i = 3; i < pixels.data.length; i += 4) pixels.data[i] = 255;
    for (const region of regions) {
        const coverage = mask(byIso.get(region.iso2));
        for (let i = 0; i < coverage.length; i += 4) {
            // Threshold each country's alpha separately: antialiased edges must
            // never blend two IDs and accidentally identify a different country.
            if (coverage[i + 3] >= 128) pixels.data[i] = region.id;
        }
    }
    ctx.putImageData(pixels, 0, 0);
    fs.writeFileSync(path.join(output, `region-id${suffix}.png`), canvas.toBuffer('image/png'));
}
bake(4096, '');
bake(2048, '-mobile');

const projection = geoOrthographic().translate([300, 300]).scale(240).rotate([-123, -12]);
const draw = geoPath(projection);
const paths = features.map(f => `<path d="${draw(f) || ''}" fill="${FEATURED.includes(isoFor(f)) ? '#16b875' : '#526b7c'}"/>`).join('');
const pins = regions.filter(r => geoDistance([123, 12], [r.lon, r.lat]) < Math.PI / 2 - 0.1).map(r => {
    const [x, y] = projection([r.lon, r.lat]);
    const flag = fs.readFileSync(path.join(output, 'flags', `${r.iso2}.svg`)).toString('base64');
    return `<g transform="translate(${x},${y})"><path d="M0 0v-17" stroke="#bcfce1"/><circle cy="-27" r="13" fill="#fff"/><clipPath id="f${r.id}"><circle cy="-27" r="11"/></clipPath><image x="-15" y="-38" width="30" height="22" href="data:image/svg+xml;base64,${flag}" clip-path="url(#f${r.id})"/></g>`;
}).join('');
const fallbackSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600"><defs><radialGradient id="o" cx="35%" cy="25%"><stop stop-color="#1a3b4b"/><stop offset="1" stop-color="#09151f"/></radialGradient><radialGradient id="s" cx="30%" cy="25%" r="80%"><stop offset=".4" stop-color="#02111c" stop-opacity="0"/><stop offset="1" stop-color="#02111c" stop-opacity=".7"/></radialGradient></defs><circle cx="300" cy="300" r="243" fill="none" stroke="#79dbc7" stroke-opacity=".3" stroke-width="3"/><circle cx="300" cy="300" r="240" fill="url(#o)"/><g stroke="#203c49" stroke-width=".5">${paths}</g><circle cx="300" cy="300" r="240" fill="url(#s)"/>${pins}</svg>`;
const fallback = createCanvas(600, 600);
fallback.getContext('2d').drawImage(await loadImage(Buffer.from(fallbackSvg)), 0, 0);
fs.writeFileSync(path.join(output, 'globe-fallback.png'), fallback.toBuffer('image/png'));
fs.rmSync(path.join(output, 'globe-fallback.svg'), { force: true });

const licenses = [
    ['three', 'LICENSE'], ['d3-geo', 'LICENSE'],
    ['topojson-client', 'LICENSE'], ['world-atlas', 'LICENSE'],
    ['i18n-iso-countries', 'LICENSE'], ['flag-icons', 'LICENSE']
];
fs.writeFileSync(path.join(output, 'THIRD-PARTY-NOTICES.txt'), licenses.map(([name, file]) =>
    `${name}\n${'='.repeat(name.length)}\n${fs.readFileSync(path.join(modules, name, file), 'utf8')}`
).join('\n\n') + '\n\nGSAP 3.14.2: Copyright (c) 2008-2025, GreenSock. All rights reserved.\nStandard license: https://gsap.com/standard-license\n');
console.log(`Built ${regions.length} regions, desktop/mobile masks, flags, and static fallback.`);
