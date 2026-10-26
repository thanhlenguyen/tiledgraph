// =============================================================================
// GeoRouting Lab — analytics.js   (the "Analytics" tab: Phase 3 + Phase 5)
//
// Load AFTER scripts.js. Reuses from scripts.js: state.map, makeStopEl(),
// setStatus(), BACKEND_URL.
//
// FLOW
//   1. User picks an analysis and its options in the sidebar
//   2. "Analyze this view" sends the visible map area (bbox) to /api/analytics/…
//   3. Backend returns { stats, legend, layers }   (same layer format as the agent)
//   4. We draw the layers, fill the stats table and legend.
//      Click any hexagon to see its numbers.
// =============================================================================

const ANA_API = `${BACKEND_URL}/analytics`;
const ANA_TIMEOUT_MS = 290_000;
const ANA_PREFIX = 'ana-lyr-';

const ana = {
    controller: null, // AbortController of the running request (Clear / tab switch cancels it)
    layers:  [],      // last layers drawn (restored after a basemap switch)
    markers: [],
    running: false,
    popup:   null,
};

// What each analysis calls, and a one-line explanation shown under the selector
const ANA_TYPES = {
    demand:   { path: 'hexbin',
                help: 'Where demand is: addresses per hexagon, weighted.' },
    failrate: { path: 'hexbin',
                help: 'Share of delivery attempts that failed, per hexagon (cells with 5+ attempts).' },
    coverage: { path: 'coverage',
                help: 'Share of demand within the time limit of the nearest facility. Green is covered.' },
    hotspots: { path: 'hotspots',
                help: 'Places where failures are unusually common, with statistical confidence.' },
    sites:    { path: 'site_selection',
                help: 'Best locations for new sites to cover the most uncovered demand. Candidates only: check land and zoning.' },
    pins:     { path: 'delivery_points/offsets',
                help: 'Addresses where drivers consistently deliver away from the official pin.' },
};


// -----------------------------------------------------------------------------
// 1. FORM
// -----------------------------------------------------------------------------

/** Show only the fields that belong to the chosen analysis (data-for="…"). */
window.analyticsShowFields = function () {
    const type = document.getElementById('ana-type').value;
    document.querySelectorAll('#tab-analytics [data-for]').forEach(el => {
        el.style.display = el.dataset.for.split(' ').includes(type) ? '' : 'none';
    });
    document.getElementById('ana-help').textContent = ANA_TYPES[type].help;

    // Facility selector means "existing facilities to keep" for site selection
    const ftypeLabel = document.getElementById('ana-ftype-label');
    const noneOpt = document.querySelector('#ana-ftype option[value=""]');
    if (type === 'sites') {
        ftypeLabel.textContent = 'Keep existing facilities';
        noneOpt.hidden = false;
    } else {
        ftypeLabel.textContent = 'Facility type';
        noneOpt.hidden = true;
        if (!document.getElementById('ana-ftype').value) document.getElementById('ana-ftype').value = 'hospital';
    }
};

const segValue = (id) => document.querySelector(`#${id} .seg.active`)?.dataset.v;

function analyticsBody(type) {
    const b = state.map.getBounds();
    const area = { bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(v => +v.toFixed(5)) };
    const res = +segValue('ana-res');
    const minutes = +document.getElementById('ana-minutes').value;
    const ftype = document.getElementById('ana-ftype').value;

    switch (type) {
        case 'demand':   return { area, dataset: 'demand', metric: 'count', resolution: res };
        case 'failrate': return { area, dataset: 'deliveries', metric: 'failure_rate', resolution: res, days: +segValue('ana-days') };
        case 'coverage': return { area, facility_type: ftype || 'hospital', minutes, resolution: res,
                                  profile: segValue('ana-profile'), direction: segValue('ana-direction') };
        case 'hotspots': return { area, method: segValue('ana-method'), resolution: res, days: +segValue('ana-days') };
        case 'sites':    return { area, n_sites: +document.getElementById('ana-nsites').value, minutes, resolution: res,
                                  profile: segValue('ana-profile'), direction: segValue('ana-direction'),
                                  existing_facility_type: ftype || null, method: segValue('ana-solver') };
        case 'pins':     return { area, min_offset_m: +document.getElementById('ana-minoff').value };
    }
}


// -----------------------------------------------------------------------------
// 2. RUN
// -----------------------------------------------------------------------------

window.analyticsRun = async function () {
    if (ana.running) return;
    const type = document.getElementById('ana-type').value;
    const btn = document.getElementById('ana-run');
    const statusEl = document.getElementById('ana-status');

    ana.running = true;
    btn.disabled = true;
    btn.textContent = 'Analysing…';
    statusEl.textContent = type === 'sites'
        ? 'Computing travel times to every candidate. This can take a minute.'
        : 'Computing…';
    setStatus('Running analysis on the current view…', 'loading');

    // Cancelling the request also stops the work on the server (it watches for disconnects)
    ana.controller = new AbortController();
    const timer = setTimeout(() => ana.controller?.abort('timeout'), ANA_TIMEOUT_MS);

    try {
        const res = await fetch(`${ANA_API}/${ANA_TYPES[type].path}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(analyticsBody(type)),
            signal: ana.controller.signal,
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 429) throw new Error('Busy: another coverage or site analysis is running. Try again in a moment.');
        if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);

        analyticsDraw(data.layers || []);
        analyticsShowStats(data.stats || [], data.legend || []);
        statusEl.textContent = `Done in ${data.seconds ?? '?'} s. ` +
            (type === 'pins' ? 'Click a green dot for details.' : 'Click a hexagon for details.');
        setStatus('✅ Analysis complete', 'success');

    } catch (err) {
        if (err.name === 'AbortError' && ana.controller?.signal.reason !== 'timeout') {
            statusEl.textContent = 'Cancelled.';
            setStatus('Analysis cancelled', 'info');
        } else {
            const msg = err.name === 'AbortError' ? 'Took too long. Zoom in or use larger hexagons.' : err.message;
            statusEl.textContent = msg;
            setStatus(`Analysis failed: ${msg}`, 'error');
        }
    } finally {
        clearTimeout(timer);
        ana.controller = null;
        ana.running = false;
        btn.disabled = false;
        btn.textContent = 'Analyze this view';
    }
};

window.analyticsLearn = async function () {
    if (!confirm('Recompute learned delivery points for all addresses from delivery GPS?')) return;
    const statusEl = document.getElementById('ana-status');
    statusEl.textContent = 'Learning delivery points…';
    const send = (token) => fetch(`${ANA_API}/delivery_points/learn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Admin-Token': token } : {}) },
        body: '{}',
    });
    try {
        let res = await send();
        if (res.status === 401) {
            const token = prompt('Admin token (ANALYTICS_ADMIN_TOKEN):');
            if (!token) { statusEl.textContent = 'Cancelled.'; return; }
            res = await send(token);
        }
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        analyticsShowStats(data.stats, []);
        statusEl.textContent = `Learned in ${data.seconds} s. Press "Analyze this view" to see corrections.`;
    } catch (err) {
        statusEl.textContent = `Learning failed: ${err.message}`;
    }
};


// -----------------------------------------------------------------------------
// 3. SIDEBAR RESULTS
// -----------------------------------------------------------------------------

function analyticsShowStats(stats, legend) {
    const statsEl = document.getElementById('ana-stats');
    statsEl.innerHTML = '';
    stats.forEach(([label, value]) => {
        const row = document.createElement('div');
        row.className = 'ana-stat';
        row.innerHTML = '<span class="ana-stat-label"></span><span class="ana-stat-value"></span>';
        row.children[0].textContent = label;
        row.children[1].textContent = value;
        statsEl.appendChild(row);
    });
    statsEl.classList.toggle('hidden', !stats.length);

    const legendEl = document.getElementById('ana-legend');
    legendEl.innerHTML = '';
    legend.forEach(({ color, label }) => {
        const item = document.createElement('div');
        item.className = 'legend-item';
        item.innerHTML = `<div class="ana-swatch" style="background:${color}"></div><div class="legend-label"></div>`;
        item.lastChild.textContent = label;
        legendEl.appendChild(item);
    });
    legendEl.classList.toggle('hidden', !legend.length);
}


// -----------------------------------------------------------------------------
// 4. MAP
// -----------------------------------------------------------------------------

function analyticsDraw(layers) {
    analyticsRemoveMap();
    ana.layers = layers;
    analyticsAddMapLayers();

    layers.filter(l => l.kind === 'points').forEach(l => {
        (l.geojson.features || []).forEach(f => {
            const p = f.properties || {};
            const el = makeStopEl(p.label || '•', p.color || l.color || '#a855f7');   // per-feature colour wins
            ana.markers.push(new maplibregl.Marker({ element: el })
                .setLngLat(f.geometry.coordinates)
                .setPopup(new maplibregl.Popup({ offset: 14, closeButton: false }).setText(p.title || l.label))
                .addTo(state.map));
        });
    });
}

/** Add polygon + line layers. Also called after a basemap style switch. */
function analyticsAddMapLayers() {
    const map = state.map;
    ana.layers.forEach((l, i) => {
        if (l.kind === 'points') return;
        const src = `${ANA_PREFIX}${i}`;
        if (map.getSource(src)) return;
        map.addSource(src, { type: 'geojson', data: l.geojson });
        const color = ['coalesce', ['get', 'color'], l.color || '#3b9eff'];

        if (l.kind === 'polygon') {
            map.addLayer({ id: `${src}-fill`, type: 'fill', source: src,
                           paint: { 'fill-color': color, 'fill-opacity': l.opacity ?? 0.35 } });
            map.addLayer({ id: `${src}-line`, type: 'line', source: src,
                           paint: { 'line-color': color, 'line-width': 0.6, 'line-opacity': 0.9 } });
            // off() first: handlers survive a style switch, so avoid adding them twice
            for (const [ev, fn] of [['click', analyticsHexPopup], ['mouseenter', anaCursorOn], ['mouseleave', anaCursorOff]]) {
                map.off(ev, `${src}-fill`, fn);
                map.on(ev, `${src}-fill`, fn);
            }
        } else {
            map.addLayer({ id: `${src}-line`, type: 'line', source: src,
                           layout: { 'line-cap': 'round' },
                           paint: { 'line-color': color, 'line-width': 2.5 } });
        }
    });
}

function anaCursorOn()  { if (state.activeMode === 'none') state.map.getCanvas().style.cursor = 'pointer'; }
function anaCursorOff() { if (state.activeMode === 'none') state.map.getCanvas().style.cursor = ''; }

// Readable names for hexagon properties in the click popup
const ANA_PROP_LABELS = {
    value: 'Value', demand: 'Demand', minutes: 'Minutes to nearest', nearest: 'Nearest facility',
    z: 'Gi* z-score', failed: 'Failed attempts', attempts: 'Attempts', addresses: 'Addresses',
    served_by: 'Served by',
};

function analyticsHexPopup(e) {
    if (state.activeMode !== 'none' || state.infoPointerActive) return;   // other tools are picking
    const p = e.features?.[0]?.properties;
    if (!p) return;
    const rows = Object.entries(ANA_PROP_LABELS)
        .filter(([k]) => p[k] !== undefined && p[k] !== null && p[k] !== 'null')
        .map(([k, label]) => {
            let v = p[k];
            if (k === 'value' && document.getElementById('ana-type').value === 'failrate') v = `${(v * 100).toFixed(1)}%`;
            return `<tr><td>${label}</td><td>${String(v).replace(/</g, '&lt;')}</td></tr>`;
        }).join('');
    ana.popup?.remove();
    ana.popup = new maplibregl.Popup({ closeButton: true, className: 'ana-popup' })
        .setLngLat(e.lngLat)
        .setHTML(`<table>${rows || '<tr><td>No details</td></tr>'}</table>`)
        .addTo(state.map);
}

function analyticsRemoveMap() {
    const map = state.map;
    if (!map) return;
    (map.getStyle()?.layers || []).filter(l => l.id.startsWith(ANA_PREFIX)).forEach(l => {
        if (l.id.endsWith('-fill')) {
            map.off('click', l.id, analyticsHexPopup);
            map.off('mouseenter', l.id, anaCursorOn);
            map.off('mouseleave', l.id, anaCursorOff);
        }
        map.removeLayer(l.id);
    });
    Object.keys(map.getStyle()?.sources || {}).filter(id => id.startsWith(ANA_PREFIX))
        .forEach(id => map.removeSource(id));
    ana.markers.forEach(m => m.remove());
    ana.markers = [];
    ana.popup?.remove();
}

window.analyticsClear = function () {
    ana.controller?.abort('cleared');     // stop a running analysis, on the server too
    analyticsRemoveMap();
    ana.layers = [];
    ['ana-stats', 'ana-legend'].forEach(id => document.getElementById(id)?.classList.add('hidden'));
    const s = document.getElementById('ana-status');
    if (s) s.textContent = '';
};

// Called from restoreActiveLayers() in scripts.js after a style switch
window.analyticsRestoreLayers = analyticsAddMapLayers;

window.addEventListener('load', () => analyticsShowFields());