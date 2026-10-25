export const FEATURED = ['ph', 'in', 'au', 'br', 'za', 'jp', 'gb', 'us', 'eg', 'id'];
export const MOBILE_FEATURED = ['ph', 'in', 'au', 'jp', 'gb', 'us'];
export const SPIN = 0.12;
export const BURN_SECONDS = 2.6;

export function regionVector(lat, lon) {
    const latitude = lat * Math.PI / 180;
    const longitude = lon * Math.PI / 180;
    return [Math.cos(latitude) * Math.cos(longitude), Math.sin(latitude),
        -Math.cos(latitude) * Math.sin(longitude)];
}

export function facingAt(region, rotation) {
    const [x, , z] = regionVector(region.lat, region.lon);
    return -Math.sin(rotation) * x + Math.cos(rotation) * z;
}

export function shouldIgnite(region, rotation) {
    // A fixed dot threshold of .93 would never trigger high-latitude countries.
    const maximumFacing = Math.cos(region.lat * Math.PI / 180);
    return region.state === 'idle' && facingAt(region, rotation) > maximumFacing * 0.93;
}

export function initialRotation(region) {
    return -Math.PI / 2 - region.lon * Math.PI / 180;
}

export function pinOpacity(facing) {
    const t = Math.max(0, Math.min(1, (facing - 0.05) / 0.25));
    return t * t * (3 - 2 * t);
}

export function setRegionProgress(data, id, progress) {
    if (!Number.isInteger(id) || id < 1 || id > 255 || !Number.isFinite(progress)) {
        throw new RangeError('Invalid globe region progress.');
    }
    data[id * 4 + 3] = Math.max(0, Math.min(1, progress));
}

export class GlobeSequence {
    constructor(regions, ease) {
        this.regions = regions.map(region => ({ ...region, state: 'idle', elapsed: 0, progress: 0 }));
        this.ease = ease;
        this.resetElapsed = 0;
        this.resetting = false;
        this.opacity = 1;
    }

    step(dt, rotation) {
        if (this.resetting) {
            this.resetElapsed += dt;
            this.opacity = 1 - this.ease(Math.max(0, Math.min(1, (this.resetElapsed - 2.5) / 1.6)));
            if (this.resetElapsed >= 4.1) {
                for (const region of this.regions) Object.assign(region, { state: 'idle', elapsed: 0, progress: 0 });
                this.resetting = false;
                this.resetElapsed = 0;
                this.opacity = 1;
            }
            return;
        }
        for (const region of this.regions) {
            if (shouldIgnite(region, rotation)) region.state = 'burning';
            if (region.state !== 'burning') continue;
            region.elapsed += dt;
            region.progress = this.ease(Math.min(1, region.elapsed / BURN_SECONDS));
            if (region.elapsed >= BURN_SECONDS) {
                region.progress = 1;
                region.state = 'burnt';
            }
        }
        if (this.regions.every(region => region.state === 'burnt')) this.resetting = true;
    }

    finish() {
        for (const region of this.regions) Object.assign(region, { state: 'burnt', progress: 1, elapsed: BURN_SECONDS + 1 });
        this.opacity = 1;
    }

    reset() {
        for (const region of this.regions) Object.assign(region, { state: 'idle', progress: 0, elapsed: 0 });
        this.resetting = false;
        this.resetElapsed = 0;
        this.opacity = 1;
    }
}
