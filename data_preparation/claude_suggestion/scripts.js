// =============================================================================
// scripts.js  —  MapLibre GL JS + VROOM/Valhalla routing demo
// =============================================================================

// ---------------------------------------------------------------------------
// Map style definitions
// ---------------------------------------------------------------------------
const STYLES = [
    { id: 'basic-style', name: 'Default',   url: '/styles/style.json',      pitch: 0,  zoom: 12, bearing: 0  },
    { id: 'sat-style',   name: 'Satellite', url: '/styles/style_sat.json',  pitch: 0,  zoom: 12, bearing: 0  },
    { id: '3d-style',    name: '3D',        url: '/styles/style_3d.json',   pitch: 45, zoom: 14, bearing: 0  },
    { id: 'bdf-style',   name: 'BDF',       url: '/styles/style_bdf.json',  pitch: 60, zoom: 17, bearing: -20}
];

// ---------------------------------------------------------------------------
// Backend — go through the Nginx proxy so we avoid CORS issues in production.
// In development (opening index.html directly) change this to http://localhost:5000
// ---------------------------------------------------------------------------
const BACKEND_URL = '/api';   // Nginx proxies /api/ → routing-api:5000
const API_ENDPOINTS = {
    route:          `${BACKEND_URL}/route`,
    matrix:         `${BACKEND_URL}/matrix`,
    isochrone:      `${BACKEND_URL}/isochrone`,
    optimize:       `${BACKEND_URL}/optimize`,
    optimize_route: `${BACKEND_URL}/optimize_route`,
    health:         `${BACKEND_URL}/health`,
};

const REQUEST_TIMEOUT = 120_000; // 2 min — VROOM can be slow for large instances

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let map;
let currentStyleId = STYLES[0].id;
let currentCenter  = [46.597, 24.876];  // Default center Riyahd; can be updated by map move or style switch
let currentZoom    = 13;
let currentPitch   = 0;
let currentBearing = 0;

/** @type {{ id: number, location: [number, number] }[]} */
let jobs    = [];
/** @type {maplibregl.Marker[]} */
let markers = [];

// Depot coordinates — can be changed via setDepot()
let depotLngLat = [106.6297, 10.8231];
let depotMarker = null;

// Isochrone layer id
const ISO_SOURCE = 'isochrone-source';
const ISO_LAYER_FILL   = 'isochrone-fill';
const ISO_LAYER_BORDER = 'isochrone-border';

// Route layer ids
const ROUTE_SOURCE = 'route-source';
const ROUTE_LAYER  = 'route-layer';

// ---------------------------------------------------------------------------
// Map initialisation
// ---------------------------------------------------------------------------
function initMap() {
    map = new maplibregl.Map({
        container: 'map',
        style:     STYLES[0].url,
        center:    currentCenter,
        zoom:      currentZoom,
        pitch:     currentPitch,
        bearing:   currentBearing,
        maxPitch:  85
    });

    // Persist camera state so style switches keep the current view
    map.on('moveend', () => {
        currentCenter  = map.getCenter().toArray();
        currentZoom    = map.getZoom();
        currentPitch   = map.getPitch();
        currentBearing = map.getBearing();
    });

    map.addControl(new maplibregl.NavigationControl(),       'top-right');
    map.addControl(new maplibregl.ScaleControl(),            'bottom-left');
    map.addControl(new maplibregl.FullscreenControl(),       'top-right');
    map.addControl(new LayerSwitcherControl(),               'bottom-right');

    map.on('load', () => {
        console.log('Map loaded');
        _addDepotMarker(depotLngLat);

        // Left-click → add job
        map.on('click', (e) => {
            // Ignore clicks on the depot marker popup
            if (e.originalEvent.target.closest?.('.maplibregl-popup')) return;
            _addJob([e.lngLat.lng, e.lngLat.lat]);
        });
    });

    map.on('error', (e) => console.error('Map error:', e));
}

// ---------------------------------------------------------------------------
// Depot helpers
// ---------------------------------------------------------------------------
function _addDepotMarker(lngLat) {
    if (depotMarker) depotMarker.remove();
    depotLngLat = lngLat;
    depotMarker = new maplibregl.Marker({ color: '#22c55e', scale: 1.2 })
        .setLngLat(lngLat)
        .setPopup(new maplibregl.Popup().setHTML('<b>🏠 Depot</b>'))
        .addTo(map);
}

/** Allow the user to move the depot by Shift+clicking on the map */
function enableDepotPlacement() {
    setStatus('Shift+click anywhere to move the depot…', 'info');
    const handler = (e) => {
        if (!e.originalEvent.shiftKey) return;
        _addDepotMarker([e.lngLat.lng, e.lngLat.lat]);
        setStatus(`Depot moved to ${e.lngLat.lng.toFixed(5)}, ${e.lngLat.lat.toFixed(5)}`, 'success');
    };
    map.once('click', handler); // fires on next click regardless; we check shiftKey
}

// ---------------------------------------------------------------------------
// Job helpers
// ---------------------------------------------------------------------------
function _addJob(lngLat) {
    const id = jobs.length + 1;
    jobs.push({ id, location: lngLat });

    const el = document.createElement('div');
    el.className = 'job-marker';
    el.textContent = id;

    const marker = new maplibregl.Marker({ element: el })
        .setLngLat(lngLat)
        .setPopup(new maplibregl.Popup({ offset: 20 }).setHTML(
            `<b>Job ${id}</b><br>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}`
        ))
        .addTo(map);
    markers.push(marker);

    updateJobCount();
    console.log('Job added:', jobs);
}

window.addRandomJob = () => {
    // Random point within ~5 km of the depot
    const lng = depotLngLat[0] + (Math.random() - 0.5) * 0.08;
    const lat = depotLngLat[1] + (Math.random() - 0.5) * 0.08;
    _addJob([+lng.toFixed(6), +lat.toFixed(6)]);
    setStatus(`Random job added at ${lng.toFixed(4)}, ${lat.toFixed(4)}`, 'success');
};

window.addRandomJobs = (n = 5) => {
    for (let i = 0; i < n; i++) window.addRandomJob();
};

window.clearMap = () => {
    jobs = [];
    markers.forEach(m => m.remove());
    markers = [];
    _removeRouteLayer();
    _removeIsochroneLayer();
    updateJobCount();
    setStatus('Map cleared', 'info');
};

function updateJobCount() {
    const el = document.getElementById('job-count');
    if (el) el.textContent = `Jobs: ${jobs.length}`;
}

// ---------------------------------------------------------------------------
// Route optimisation (VROOM)
// ---------------------------------------------------------------------------
window.optimizeRoute = async () => {
    if (jobs.length < 1) {
        setStatus('Add at least one job first', 'warn');
        return;
    }

    setStatus('Optimising…', 'loading');

    const payload = {
        vehicles: [{
            id:      1,
            start:   depotLngLat,
            end:     depotLngLat,
            profile: 'car'          // Valhalla costing profile
        }],
        jobs: jobs.map(j => ({
            id:       j.id,
            location: j.location,
            service:  300           // 5 min per stop
        }))
        // options.g = true is injected server-side by /optimize_route
    };

    try {
        const result = await fetchWithTimeout(API_ENDPOINTS.optimize_route, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        });

        console.log('Optimisation result:', result);

        if (result.geojson && result.geojson.features.length > 0) {
            _drawGeoJsonRoute(result.geojson);
            const r = result.vroom.routes[0];
            const km    = (r.distance / 1000).toFixed(1);
            const mins  = Math.round(r.duration / 60);
            setStatus(`✅ Route optimised — ${km} km · ${mins} min`, 'success');
        } else {
            setStatus('No route returned — check jobs are within the routable area', 'warn');
        }
    } catch (err) {
        console.error(err);
        setStatus(`Error: ${err.message}`, 'error');
    }
};

// ---------------------------------------------------------------------------
// Simple A→B route (Valhalla)
// ---------------------------------------------------------------------------
window.routeAtoB = async () => {
    if (jobs.length < 2) {
        setStatus('Add at least 2 jobs for A→B routing', 'warn');
        return;
    }
    setStatus('Calculating route…', 'loading');

    // Build Valhalla locations from first → last job
    const locations = jobs.map(j => ({ lon: j.location[0], lat: j.location[1] }));
    const payload = {
        locations,
        costing: 'auto',
        directions_options: { language: 'en-US' }
    };

    try {
        const result = await fetchWithTimeout(API_ENDPOINTS.route, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        });

        const trip = result.trip;
        if (!trip) { setStatus('No trip returned from Valhalla', 'warn'); return; }

        // Valhalla returns one encoded polyline per leg; concatenate them
        const allCoords = trip.legs.flatMap(leg => decodePolyline6(leg.shape));

        _drawCoordinatesRoute(allCoords);

        const km   = (trip.summary.length).toFixed(1);
        const mins = Math.round(trip.summary.time / 60);
        setStatus(`Route: ${km} km · ${mins} min`, 'success');
    } catch (err) {
        console.error(err);
        setStatus(`Error: ${err.message}`, 'error');
    }
};

// ---------------------------------------------------------------------------
// Isochrone (Valhalla)
// ---------------------------------------------------------------------------
window.showIsochrone = async () => {
    setStatus('Calculating service area…', 'loading');

    const payload = {
        locations: [{ lon: depotLngLat[0], lat: depotLngLat[1] }],
        costing:   'auto',
        contours:  [
            { time: 5,  color: 'ff0000' },
            { time: 10, color: 'ffaa00' },
            { time: 15, color: '00cc44' }
        ],
        polygons: true
    };

    try {
        const geojson = await fetchWithTimeout(API_ENDPOINTS.isochrone, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        });

        _drawIsochrone(geojson);
        setStatus('✅ Isochrone shown (5 / 10 / 15 min from depot)', 'success');
    } catch (err) {
        console.error(err);
        setStatus(`Error: ${err.message}`, 'error');
    }
};

window.clearIsochrone = () => {
    _removeIsochroneLayer();
    setStatus('Isochrone removed', 'info');
};

// ---------------------------------------------------------------------------
// Drawing helpers
// ---------------------------------------------------------------------------
function _removeRouteLayer() {
    if (map.getLayer(ROUTE_LAYER))  map.removeLayer(ROUTE_LAYER);
    if (map.getSource(ROUTE_SOURCE)) map.removeSource(ROUTE_SOURCE);
}

function _removeIsochroneLayer() {
    if (map.getLayer(ISO_LAYER_BORDER)) map.removeLayer(ISO_LAYER_BORDER);
    if (map.getLayer(ISO_LAYER_FILL))   map.removeLayer(ISO_LAYER_FILL);
    if (map.getSource(ISO_SOURCE))       map.removeSource(ISO_SOURCE);
}

function _drawGeoJsonRoute(geojson) {
    _removeRouteLayer();

    map.addSource(ROUTE_SOURCE, { type: 'geojson', data: geojson });

    map.addLayer({
        id:     ROUTE_LAYER,
        type:   'line',
        source: ROUTE_SOURCE,
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint:  { 'line-color': '#0ea5e9', 'line-width': 5, 'line-opacity': 0.9 }
    });
}

function _drawCoordinatesRoute(coordinates) {
    _removeRouteLayer();

    map.addSource(ROUTE_SOURCE, {
        type: 'geojson',
        data: {
            type: 'Feature',
            geometry: { type: 'LineString', coordinates }
        }
    });

    map.addLayer({
        id:     ROUTE_LAYER,
        type:   'line',
        source: ROUTE_SOURCE,
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint:  { 'line-color': '#f97316', 'line-width': 5, 'line-opacity': 0.9 }
    });
}

function _drawIsochrone(geojson) {
    _removeIsochroneLayer();

    map.addSource(ISO_SOURCE, { type: 'geojson', data: geojson });

    map.addLayer({
        id:     ISO_LAYER_FILL,
        type:   'fill',
        source: ISO_SOURCE,
        paint:  { 'fill-color': ['get', 'color'], 'fill-opacity': 0.2 }
    });

    map.addLayer({
        id:     ISO_LAYER_BORDER,
        type:   'line',
        source: ISO_SOURCE,
        paint:  { 'line-color': ['get', 'color'], 'line-width': 2 }
    });
}

// ---------------------------------------------------------------------------
// Polyline decoder — precision 6 (Valhalla / VROOM)
// Returns [[lng, lat], …]
// ---------------------------------------------------------------------------
function decodePolyline6(encoded) {
    const coords = [];
    let index = 0, lat = 0, lng = 0;
    const len = encoded.length;

    while (index < len) {
        let b, shift = 0, result = 0;
        do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
        const dlat = (result & 1) ? ~(result >> 1) : (result >> 1);
        lat += dlat;

        shift = 0; result = 0;
        do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
        const dlng = (result & 1) ? ~(result >> 1) : (result >> 1);
        lng += dlng;

        coords.push([lng / 1e6, lat / 1e6]);
    }
    return coords;
}

// ---------------------------------------------------------------------------
// Style switcher control
// ---------------------------------------------------------------------------
class LayerSwitcherControl {
    onAdd(map) {
        this._map = map;
        this._container = document.createElement('div');
        this._container.className = 'maplibregl-ctrl maplibregl-ctrl-group layer-switcher';

        STYLES.forEach((s, idx) => {
            const btn = document.createElement('button');
            btn.type        = 'button';
            btn.title       = s.name;
            btn.textContent = s.name;
            if (idx === 0) btn.classList.add('active');

            btn.addEventListener('click', () => {
                this._container.querySelectorAll('button').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentStyleId = s.id;
                map.setStyle(s.url);
                map.once('styledata', () => {
                    // Re-add any visible route / isochrone after style swap
                    applyViewForStyle(s);
                });
            });

            this._container.appendChild(btn);
        });

        return this._container;
    }

    onRemove() {
        this._container.parentNode?.removeChild(this._container);
    }
}

function applyViewForStyle(style) {
    map.easeTo({
        center:   currentCenter,
        zoom:     style.zoom    ?? currentZoom,
        pitch:    style.pitch   ?? currentPitch,
        bearing:  style.bearing ?? currentBearing,
        duration: 1000
    });
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------
function setStatus(msg, type = 'info') {
    const el = document.getElementById('status-bar');
    if (!el) return;
    el.textContent = msg;
    el.className   = `status-bar status-${type}`;
    if (type === 'success' || type === 'info') {
        setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 6000);
    }
}

// ---------------------------------------------------------------------------
// Fetch with timeout
// ---------------------------------------------------------------------------
async function fetchWithTimeout(url, options = {}) {
    const controller = new AbortController();
    const tid = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);

    try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        clearTimeout(tid);

        if (!response.ok) {
            let detail = `${response.status} ${response.statusText}`;
            try { const b = await response.json(); if (b.detail) detail = JSON.stringify(b.detail); } catch {}
            throw new Error(detail);
        }
        return response.json();  // ← returns parsed JSON directly (no double-parse)

    } catch (err) {
        clearTimeout(tid);
        if (err.name === 'AbortError') throw new Error('Request timed out — try again');
        throw err;
    }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
window.addEventListener('load', initMap);
