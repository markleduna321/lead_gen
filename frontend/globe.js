import * as THREE from 'three';
import { gsap } from 'gsap';
import { MOBILE_FEATURED, SPIN, BURN_SECONDS, regionVector, initialRotation,
    pinOpacity, GlobeSequence, setRegionProgress } from './globe-state.mjs';

const vertexShader = `
varying vec2 vUv;
varying vec3 vObjPos;
varying vec3 vNormal;
varying vec3 vView;
void main() {
    vUv = uv;
    vObjPos = position;
    vec4 viewPos = modelViewMatrix * vec4(position, 1.0);
    vNormal = normalize(normalMatrix * normal);
    vView = -viewPos.xyz;
    gl_Position = projectionMatrix * viewPos;
}`;

const fragmentShader = `
precision highp float;
uniform sampler2D uMap;
uniform sampler2D uRegionId;
uniform sampler2D uRegionData;
uniform vec3 uLand;
uniform vec3 uOcean;
uniform vec3 uBorder;
uniform vec3 uGreen;
uniform vec3 uEdgeColor;
uniform float uReset;
varying vec2 vUv;
varying vec3 vObjPos;
varying vec3 vNormal;
varying vec3 vView;
float hash(vec3 p) {
    p = fract(p * 0.3183099 + 0.1);
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
}
float noise(vec3 x) {
    vec3 i = floor(x), f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
        mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x),
            mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
        mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x),
            mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
}
float fbm(vec3 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 4; i++) { v += a * noise(p); p *= 2.0; a *= 0.5; }
    return v;
}
void main() {
    vec2 mask = texture2D(uMap, vUv).rg;
    vec3 base = mix(uOcean, uLand, mask.r);
    base = mix(base, uBorder, mask.g * 0.65);
    vec3 col = base;
    float id = floor(texture2D(uRegionId, vUv).r * 255.0 + 0.5);
    if (id > 0.5) {
        float u = (id + 0.5) / 256.0;
        vec4 d0 = texture2D(uRegionData, vec2(u, 0.25));
        vec4 d1 = texture2D(uRegionData, vec2(u, 0.75));
        if (d0.w > 0.0) {
            vec3 n = normalize(vObjPos);
            float dist = min(acos(clamp(dot(n, d0.xyz), -1.0, 1.0)) / max(d1.x, 0.02), 1.0);
            float front = dist + (fbm(n * (2.0 / max(d1.x, 0.02)) + d1.y * 17.0) - 0.5) * 0.2;
            float t = mix(-0.2, 1.28, d0.w) - front;
            float burnt = smoothstep(0.0, 0.005, t);
            float edge = smoothstep(0.0, 0.04, t) - smoothstep(0.04, 0.08, t);
            float scorch = smoothstep(-0.06, 0.0, t) * (1.0 - burnt);
            vec3 green = uGreen * (0.9 - mask.g * 0.25);
            col = mix(col, col * 0.25, scorch);
            col = mix(col, green, burnt);
            col += uEdgeColor * edge * 1.6;
        }
    }
    col = mix(base, col, uReset);
    vec3 normal = normalize(vNormal);
    float light = 0.43 + 0.57 * max(dot(normal, normalize(vec3(-0.8, 1.0, 1.5))), 0.0);
    float rim = pow(1.0 - max(dot(normal, normalize(vView)), 0.0), 3.0);
    col = col * light + vec3(0.04, 0.23, 0.25) * rim * 0.5;
    gl_FragColor = vec4(col, 1.0);
    #include <colorspace_fragment>
}`;

function loadImage(url) {
    return new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error(`Globe image could not be loaded: ${url}`));
        image.src = url;
    });
}

function flagTexture(image) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 128;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Globe flag canvas is unavailable.');
    ctx.save();
    ctx.beginPath();
    ctx.arc(64, 64, 58, 0, Math.PI * 2);
    ctx.clip();
    const width = 116 * image.naturalWidth / image.naturalHeight;
    ctx.drawImage(image, (128 - width) / 2, 6, width, 116);
    ctx.restore();
    ctx.lineWidth = 5;
    ctx.strokeStyle = '#eefdf5';
    ctx.beginPath();
    ctx.arc(64, 64, 58, 0, Math.PI * 2);
    ctx.stroke();
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
}

async function mountGlobe(host) {
    const canvas = host.querySelector('canvas');
    const button = host.querySelector('button');
    const caption = host.querySelector('[data-globe-caption]');
    const resources = new Set();
    const geometries = new Set();
    const materials = new Set();
    let renderer;
    let disposed = false;
    let frame = 0;
    let previousTime = 0;
    let visible = true;
    let paused = false;
    let sequence;
    let rotation;
    let observer;
    let resizeObserver;
    let themeObserver;
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    const mobile = matchMedia('(max-width: 800px)').matches;
    const ease = gsap.parseEase('power1.inOut');
    const flagEase = gsap.parseEase('back.out(2.2)');
    const asset = name => new URL(name, new URL(host.dataset.assetBase, location.href)).href;
    const track = (set, item) => { set.add(item); return item; };
    function dispose() {
        if (disposed) return;
        disposed = true;
        cancelAnimationFrame(frame);
        observer?.disconnect();
        resizeObserver?.disconnect();
        themeObserver?.disconnect();
        document.removeEventListener('visibilitychange', syncPlayback);
        motion.removeEventListener('change', updateMotion);
        window.removeEventListener('pagehide', onPageHide);
        window.removeEventListener('pageshow', syncPlayback);
        button.removeEventListener('click', togglePause);
        canvas.removeEventListener('webglcontextlost', contextLost);
        for (const item of resources) item.dispose();
        for (const item of geometries) item.dispose();
        for (const item of materials) item.dispose();
        renderer?.dispose();
    }
    function fail(error) {
        console.error('Hero globe unavailable; displaying the static illustration.', error);
        host.dataset.state = 'fallback';
        caption.textContent = 'Global possibilities. One focused workspace.';
        button.hidden = true;
        dispose();
    }
    function contextLost(event) {
        event.preventDefault();
        fail(new Error('WebGL context lost.'));
    }
    function onPageHide(event) {
        if (event.persisted) {
            cancelAnimationFrame(frame);
            frame = 0;
        } else dispose();
    }
    function togglePause() {
        paused = !paused;
        button.textContent = paused ? 'Resume globe' : 'Pause globe';
        button.setAttribute('aria-pressed', String(paused));
        syncPlayback();
    }
    function syncPlayback() {
        cancelAnimationFrame(frame);
        frame = 0;
        previousTime = 0;
        if (!disposed && visible && !document.hidden && !paused && !motion.matches) {
            frame = requestAnimationFrame(tick);
        }
    }
    function updateMotion() {
        if (!sequence) return;
        if (motion.matches) {
            sequence.finish();
            rotation = initialRotation(sequence.regions[0]);
        } else sequence.reset();
        button.hidden = motion.matches;
        draw();
        syncPlayback();
    }
    let draw = () => {};
    let tick = () => {};
    try {
        renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'low-power' });
        renderer.setPixelRatio(Math.min(devicePixelRatio, mobile ? 1.5 : 2));
        renderer.setClearColor(0x000000, 0);
        const response = await fetch(asset('regions.json'));
        if (!response.ok) throw new Error(`Globe regions returned HTTP ${response.status}.`);
        const catalog = await response.json();
        if (!Array.isArray(catalog) || catalog.length !== 10 || catalog.some(r =>
            !Number.isInteger(r.id) || r.id < 1 || r.id > 255 || !/^[a-z]{2}$/.test(r.iso2)
            || !Number.isFinite(r.lat) || !Number.isFinite(r.lon) || !(r.radius > 0))) {
            throw new Error('Invalid globe region catalog.');
        }
        const regions = catalog.filter(r => !mobile || MOBILE_FEATURED.includes(r.iso2));
        sequence = new GlobeSequence(regions, ease);
        rotation = initialRotation(regions[0]);
        const suffix = mobile ? '-mobile' : '';
        const images = await Promise.all([
            loadImage(asset(`world-mask${suffix}.png`)), loadImage(asset(`region-id${suffix}.png`)),
            ...regions.map(r => loadImage(asset(`flags/${r.iso2}.svg`)))
        ]);
        const texture = (image, exact = false) => {
            const item = track(resources, new THREE.Texture(image));
            item.colorSpace = THREE.NoColorSpace;
            item.minFilter = item.magFilter = exact ? THREE.NearestFilter : THREE.LinearFilter;
            item.generateMipmaps = false;
            item.needsUpdate = true;
            return item;
        };
        const data = new Float32Array(256 * 2 * 4);
        sequence.regions.forEach(r => {
            r.center = new THREE.Vector3(...regionVector(r.lat, r.lon));
            data.set([...r.center, 0], r.id * 4);
            data.set([r.radius, r.id / 11, 0, 0], (256 + r.id) * 4);
        });
        const dataTexture = track(resources, new THREE.DataTexture(data, 256, 2, THREE.RGBAFormat, THREE.FloatType));
        dataTexture.minFilter = dataTexture.magFilter = THREE.NearestFilter;
        dataTexture.needsUpdate = true;
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 30);
        camera.position.z = 4;
        const group = new THREE.Group();
        scene.add(group);
        const uniforms = {
            uMap: { value: texture(images[0]) }, uRegionId: { value: texture(images[1], true) },
            uRegionData: { value: dataTexture }, uReset: { value: 1 },
            uLand: { value: new THREE.Color() }, uOcean: { value: new THREE.Color() },
            uBorder: { value: new THREE.Color() }, uGreen: { value: new THREE.Color('#00d975') },
            uEdgeColor: { value: new THREE.Color('#b8ffd0') }
        };
        const material = track(materials, new THREE.ShaderMaterial({ vertexShader, fragmentShader, uniforms }));
        renderer.debug.onShaderError = () => fail(new Error('Globe shader compilation failed.'));
        group.add(new THREE.Mesh(track(geometries, new THREE.SphereGeometry(1, mobile ? 64 : 128, mobile ? 64 : 128)), material));
        const atmosphere = track(materials, new THREE.ShaderMaterial({
            transparent: true, depthWrite: false, side: THREE.BackSide,
            vertexShader: `varying vec3 n; varying vec3 v;
                void main(){ vec4 p=modelViewMatrix*vec4(position,1.0); n=normalize(normalMatrix*normal);
                v=normalize(-p.xyz); gl_Position=projectionMatrix*p; }`,
            fragmentShader: `varying vec3 n; varying vec3 v;
                void main(){float rim=pow(1.0-abs(dot(normalize(n),normalize(v))),3.0);
                gl_FragColor=vec4(0.17,0.75,0.72,rim*0.18);}`
        }));
        scene.add(new THREE.Mesh(track(geometries, new THREE.SphereGeometry(1.035, 64, 64)), atmosphere));
        const starPoints = [];
        for (let i = 0; i < (mobile ? 35 : 80); i++) {
            const angle = i * 2.399963;
            const distance = 1.22 + ((i * 37) % 31) / 55;
            starPoints.push(Math.cos(angle) * distance, Math.sin(angle) * distance, -0.8);
        }
        const stars = track(geometries, new THREE.BufferGeometry());
        stars.setAttribute('position', new THREE.Float32BufferAttribute(starPoints, 3));
        const starMaterial = track(materials, new THREE.PointsMaterial({ size: 0.008, transparent: true, opacity: 0.5, color: '#529a99', depthWrite: false }));
        scene.add(new THREE.Points(stars, starMaterial));
        sequence.regions.forEach((r, index) => {
            const pin = new THREE.Group();
            const stemMaterial = track(materials, new THREE.LineBasicMaterial({ color: '#d6ffeb', transparent: true }));
            const stem = new THREE.Line(track(geometries, new THREE.BufferGeometry().setFromPoints([
                r.center.clone(), r.center.clone().multiplyScalar(1.095)
            ])), stemMaterial);
            const flag = track(resources, flagTexture(images[index + 2]));
            const spriteMaterial = track(materials, new THREE.SpriteMaterial({ map: flag, depthWrite: false, transparent: true }));
            const sprite = new THREE.Sprite(spriteMaterial);
            sprite.position.copy(r.center).multiplyScalar(1.12);
            pin.add(stem, sprite);
            pin.visible = false;
            group.add(pin);
            r.pin = { group: pin, sprite, stem };
        });
        const worldCenter = new THREE.Vector3();
        draw = () => {
            if (disposed) return;
            group.rotation.y = rotation;
            group.updateMatrixWorld(true);
            let highlighted = null;
            for (const r of sequence.regions) {
                setRegionProgress(data, r.id, r.progress);
                worldCenter.copy(r.center).applyQuaternion(group.quaternion);
                const opacity = pinOpacity(worldCenter.z) * sequence.opacity;
                const pop = motion.matches ? 1 : flagEase(Math.max(0, Math.min(1, (r.elapsed - (BURN_SECONDS - 0.5)) / 0.6)));
                r.pin.group.visible = pop > 0 && opacity > 0;
                r.pin.sprite.scale.setScalar(pop * 0.125);
                r.pin.sprite.material.opacity = opacity;
                r.pin.stem.material.opacity = opacity * Math.min(1, pop) * 0.8;
                if (r.state === 'burning' && worldCenter.z > 0.3) highlighted = r;
            }
            dataTexture.needsUpdate = true;
            uniforms.uReset.value = sequence.opacity;
            const text = highlighted ? `${highlighted.name} / a world of possibility` : 'Global possibilities. One focused workspace.';
            if (caption.textContent !== text) caption.textContent = text;
            renderer.render(scene, camera);
        };
        tick = now => {
            if (disposed) return;
            if (motion.matches) {
                updateMotion();
                return;
            }
            const dt = previousTime ? Math.min((now - previousTime) / 1000, 0.05) : 0;
            previousTime = now;
            rotation = (rotation + SPIN * dt) % (Math.PI * 2);
            sequence.step(dt, rotation);
            draw();
            if (!disposed) frame = requestAnimationFrame(tick);
        };
        function resize() {
            const { width, height } = host.getBoundingClientRect();
            if (!width || !height || disposed) return;
            camera.aspect = width / height;
            camera.updateProjectionMatrix();
            renderer.setSize(width, height, false);
            draw();
        }
        function theme() {
            const dark = document.documentElement.dataset.theme === 'dark';
            uniforms.uLand.value.set(dark ? '#506875' : '#657b87');
            uniforms.uOcean.value.set(dark ? '#071520' : '#102532');
            uniforms.uBorder.value.set(dark ? '#1a3541' : '#2b4350');
            starMaterial.opacity = dark ? 0.65 : 0.4;
            draw();
        }
        themeObserver = new MutationObserver(theme);
        themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        resizeObserver = new ResizeObserver(resize);
        resizeObserver.observe(host);
        observer = new IntersectionObserver(entries => {
            visible = entries[0].isIntersecting;
            syncPlayback();
        }, { threshold: 0.05 });
        observer.observe(host);
        document.addEventListener('visibilitychange', syncPlayback);
        motion.addEventListener('change', updateMotion);
        window.addEventListener('pagehide', onPageHide);
        window.addEventListener('pageshow', syncPlayback);
        canvas.addEventListener('webglcontextlost', contextLost);
        button.addEventListener('click', togglePause);
        theme();
        resize();
        if (disposed) return;
        host.dataset.state = 'ready';
        button.hidden = motion.matches;
        updateMotion();
    } catch (error) {
        fail(error);
    }
}

const host = document.getElementById('hero-globe');
if (host) mountGlobe(host);
