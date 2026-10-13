// =============================================================================
// GeoRouting Lab — agent.js   (the chat panel for the agentic layer)
//
// Load AFTER scripts.js. It reuses from scripts.js:
//   state.map, makeStopEl(), exitMode(), BACKEND_URL
//
// FLOW
//   1. User types a question (and optionally clicks "Pick point" on the map)
//   2. We POST { messages, context } to /api/agent/chat
//   3. Backend returns { reply, layers[], last_results }
//   4. We show the reply and draw each layer:
//        kind 'line'    → MapLibre line layer      (routes)
//        kind 'polygon' → fill + outline layers     (isochrones)
//        kind 'points'  → HTML markers with popups  (facilities, stops, addresses)
// =============================================================================

const AGENT_API = `${BACKEND_URL}/agent`;
const AGENT_TIMEOUT_MS = 150_000;   // agent may call several tools in a row

const agent = {
    history:     [],     // [{ role: 'user'|'assistant', content: string }]
    lastResults: null,   // compact tool results from the previous answer
    pickedPoint: null,   // { lon, lat }
    pickMarker:  null,
    picking:     false,
    busy:        false,
    layers:      [],     // last layers drawn (kept to restore after style switch)
    markers:     [],     // maplibregl.Marker[] created by the agent
};

const AGENT_EXAMPLES = [
    'Nearest hospital to my point',
    '10, 20 and 30 minute drive area from my point',
    'Route from my point to the nearest police station',
    'أقرب مستشفى إلى النقطة المحددة',
];


// -----------------------------------------------------------------------------
// 1. BUILD THE PANEL
// -----------------------------------------------------------------------------

function agentBuildPanel() {
    const launcher = document.createElement('button');
    launcher.id = 'agent-launcher';
    launcher.type = 'button';
    launcher.innerHTML = '<span aria-hidden="true">✦</span> Ask the map';
    launcher.setAttribute('aria-expanded', 'false');
    launcher.onclick = () => agentTogglePanel();
    document.body.appendChild(launcher);

    const panel = document.createElement('section');
    panel.id = 'agent-panel';
    panel.className = 'hidden';
    panel.setAttribute('aria-label', 'Map assistant');
    panel.innerHTML = `
        <header class="agent-head">
            <div>
                <div class="agent-title">Map assistant</div>
                <div class="agent-sub">Ask in English or Arabic</div>
            </div>
            <button class="agent-icon-btn" id="agent-close" title="Close" aria-label="Close">✕</button>
        </header>

        <div id="agent-log" class="agent-log" aria-live="polite">
            <div class="agent-empty">
                <p>Pick a point, then ask a question. Results are drawn on the map.</p>
                <div class="agent-chips"></div>
            </div>
        </div>

        <div class="agent-point-row">
            <button id="agent-pick" class="agent-pick" type="button">📍 Pick point</button>
            <span id="agent-point-label" class="agent-point-label">No point selected</span>
            <button id="agent-clear" class="agent-link" type="button">Clear map</button>
        </div>

        <form id="agent-form" class="agent-form">
            <textarea id="agent-input" rows="1" dir="auto"
                      placeholder="e.g. nearest clinic within 10 km of my point"></textarea>
            <button id="agent-send" class="agent-send" type="submit" aria-label="Send">➤</button>
        </form>`;
    document.body.appendChild(panel);

    // Example chips (click = send)
    const chips = panel.querySelector('.agent-chips');
    AGENT_EXAMPLES.forEach(text => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'agent-chip';
        b.dir = 'auto';
        b.textContent = text;
        b.onclick = () => agentSend(text);
        chips.appendChild(b);
    });

    panel.querySelector('#agent-close').onclick = () => agentTogglePanel(false);
    panel.querySelector('#agent-pick').onclick  = agentStartPick;
    panel.querySelector('#agent-clear').onclick = agentClearMap;

    const input = panel.querySelector('#agent-input');
    panel.querySelector('#agent-form').onsubmit = (e) => {
        e.preventDefault();
        agentSend(input.value);
    };
    // Enter = send, Shift+Enter = new line
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); agentSend(input.value); }
    });
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
}

function agentTogglePanel(open) {
    const panel = document.getElementById('agent-panel');
    const show  = open ?? panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !show);
    document.getElementById('agent-launcher').setAttribute('aria-expanded', String(show));
    if (show) document.getElementById('agent-input').focus();
}


// -----------------------------------------------------------------------------
// 2. PICK A POINT ON THE MAP
// -----------------------------------------------------------------------------

function agentStartPick() {
    if (agent.picking) return;        // already waiting for a click
    exitMode();                       // stop any tab's picking mode first
    agent.picking = true;
    const btn = document.getElementById('agent-pick');
    btn.classList.add('picking');
    btn.textContent = '📍 Click the map…';
    state.map.getCanvas().style.cursor = 'crosshair';

    state.map.once('click', (e) => {
        agent.picking = false;
        btn.classList.remove('picking');
        btn.textContent = '📍 Pick point';
        state.map.getCanvas().style.cursor = '';
        agentSetPoint(e.lngLat.lng, e.lngLat.lat);
    });
}

function agentSetPoint(lon, lat) {
    agent.pickedPoint = { lon: +lon.toFixed(6), lat: +lat.toFixed(6) };
    if (agent.pickMarker) agent.pickMarker.remove();
    const el = makeStopEl('★', '#a855f7');
    agent.pickMarker = new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(state.map);
    document.getElementById('agent-point-label').textContent =
        `${agent.pickedPoint.lat.toFixed(5)}, ${agent.pickedPoint.lon.toFixed(5)}`;
}


// -----------------------------------------------------------------------------
// 3. SEND A QUESTION
// -----------------------------------------------------------------------------

async function agentSend(rawText) {
    const text = (rawText || '').trim();
    if (!text || agent.busy) return;

    const input = document.getElementById('agent-input');
    input.value = '';
    input.style.height = 'auto';
    document.querySelector('#agent-log .agent-empty')?.remove();

    agentAddMessage('user', text);
    agent.history.push({ role: 'user', content: text });
    agentSetBusy(true);
    const thinking = agentAddMessage('thinking', 'Working on it…');

    const center = state.map.getCenter();
    const body = {
        messages: agent.history.slice(-20),
        context: {
            picked_point: agent.pickedPoint,
            map_center:   { lon: +center.lng.toFixed(6), lat: +center.lat.toFixed(6) },
            map_zoom:     +state.map.getZoom().toFixed(1),
            last_results: agent.lastResults,
        },
    };

    try {
        const res = await fetch(`${AGENT_API}/chat`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(body),
            signal:  AbortSignal.timeout(AGENT_TIMEOUT_MS),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `Request failed (HTTP ${res.status})`);

        thinking.remove();
        agent.history.push({ role: 'assistant', content: data.reply });
        agent.lastResults = data.last_results || null;
        agentAddMessage('assistant', data.reply, data.request_id, data.tools_used);
        if (data.layers?.length) agentDrawLayers(data.layers);

    } catch (err) {
        thinking.remove();
        agent.history.pop();   // drop the unanswered question so history stays valid
        const msg = err.name === 'TimeoutError'
            ? 'The request took too long. Try a smaller question.'
            : err.message;
        agentAddMessage('error', msg);
    } finally {
        agentSetBusy(false);
    }
}

function agentSetBusy(on) {
    agent.busy = on;
    document.getElementById('agent-send').disabled = on;
    document.getElementById('agent-input').disabled = on;
    if (!on) document.getElementById('agent-input').focus();
}


// -----------------------------------------------------------------------------
// 4. CHAT MESSAGES
// -----------------------------------------------------------------------------

/** Safe mini-markdown: escape HTML, then **bold** and line breaks only. */
function agentFormat(text) {
    const esc = String(text).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    return esc.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
}

const TOOL_NAMES = {
    search_address: 'address search', search_places: 'place search', get_route: 'route',
    compute_isochrone: 'isochrone', find_nearest_facility: 'nearest facility', optimize_stops: 'stop optimisation',
};

function agentAddMessage(role, text, requestId, toolsUsed) {
    const log = document.getElementById('agent-log');
    const el  = document.createElement('div');
    el.className = `agent-msg agent-${role}`;
    el.dir = 'auto';                          // Arabic → right-to-left automatically
    el.innerHTML = agentFormat(text);

    if (role === 'assistant') {
        const meta = document.createElement('div');
        meta.className = 'agent-meta';
        const used = [...new Set(toolsUsed || [])].map(t => TOOL_NAMES[t] || t);
        meta.innerHTML = `<span class="agent-tools">${used.length ? 'Used ' + used.join(', ') : ''}</span>`;
        if (requestId) {
            ['up', 'down'].forEach(rating => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'agent-rate';
                b.title = rating === 'up' ? 'Helpful' : 'Not helpful';
                b.textContent = rating === 'up' ? '👍' : '👎';
                b.onclick = () => agentRate(requestId, rating, meta);
                meta.appendChild(b);
            });
        }
        el.appendChild(meta);
    }
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
}

async function agentRate(requestId, rating, metaEl) {
    let comment = null;
    if (rating === 'down') comment = prompt('What was wrong? (optional)') || null;
    metaEl.querySelectorAll('.agent-rate').forEach(b => b.remove());
    metaEl.insertAdjacentHTML('beforeend', '<span class="agent-thanks">Feedback saved</span>');
    try {
        await fetch(`${AGENT_API}/feedback`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ request_id: requestId, rating, comment }),
        });
    } catch { /* feedback is best-effort */ }
}


// -----------------------------------------------------------------------------
// 5. DRAW RESULTS ON THE MAP
// -----------------------------------------------------------------------------

const AGENT_PREFIX = 'agent-lyr-';

function agentDrawLayers(layers) {
    agentClearMap({ keepPoint: true });
    agent.layers = layers;
    agentAddMapLayers();

    // Markers (they survive style switches, so they are added only once)
    layers.filter(l => l.kind === 'points').forEach(l => {
        (l.geojson.features || []).forEach(f => {
            const p  = f.properties || {};
            const el = makeStopEl(p.label || '•', l.color || '#3b9eff');
            const popup = new maplibregl.Popup({ offset: 14, closeButton: false })
                .setText(p.title || l.label);
            agent.markers.push(
                new maplibregl.Marker({ element: el })
                    .setLngLat(f.geometry.coordinates).setPopup(popup).addTo(state.map));
        });
    });

    // Zoom to everything that was drawn
    const bounds = new maplibregl.LngLatBounds();
    const extend = c => (typeof c[0] === 'number' ? bounds.extend(c) : c.forEach(extend));
    layers.forEach(l => (l.geojson.features || []).forEach(f => f.geometry && extend(f.geometry.coordinates)));
    if (!bounds.isEmpty()) state.map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 800 });
}

/** Add line and polygon layers. Also called after a basemap style switch. */
function agentAddMapLayers() {
    const map = state.map;
    agent.layers.forEach((l, i) => {
        if (l.kind === 'points') return;
        const src = `${AGENT_PREFIX}${i}`;
        if (map.getSource(src)) return;
        map.addSource(src, { type: 'geojson', data: l.geojson });
        const color = ['coalesce', ['get', 'color'], l.color || '#3b9eff'];

        if (l.kind === 'polygon') {
            map.addLayer({ id: `${src}-fill`, type: 'fill', source: src,
                           paint: { 'fill-color': color, 'fill-opacity': 0.14 } });
            map.addLayer({ id: `${src}-line`, type: 'line', source: src,
                           paint: { 'line-color': color, 'line-width': 2 } });
        } else {
            map.addLayer({ id: `${src}-casing`, type: 'line', source: src,
                           layout: { 'line-cap': 'round', 'line-join': 'round' },
                           paint: { 'line-color': '#080e1a', 'line-width': 8, 'line-opacity': 0.6 } });
            map.addLayer({ id: `${src}-line`, type: 'line', source: src,
                           layout: { 'line-cap': 'round', 'line-join': 'round' },
                           paint: {
                               'line-color': color,
                               // nearest facility route drawn thicker than the others
                               'line-width': ['case', ['==', ['get', 'facility_rank'], 1], 6,
                                              ['has', 'facility_rank'], 3.5, 5],
                           } });
        }
    });
}

function agentClearMap(opts = {}) {
    const map = state.map;
    (map.getStyle()?.layers || []).filter(l => l.id.startsWith(AGENT_PREFIX))
        .forEach(l => map.removeLayer(l.id));
    Object.keys(map.getStyle()?.sources || {}).filter(id => id.startsWith(AGENT_PREFIX))
        .forEach(id => map.removeSource(id));
    agent.markers.forEach(m => m.remove());
    agent.markers = [];
    agent.layers  = [];
    if (!opts.keepPoint && agent.pickMarker) {
        agent.pickMarker.remove();
        agent.pickMarker  = null;
        agent.pickedPoint = null;
        document.getElementById('agent-point-label').textContent = 'No point selected';
    }
}

// Called from restoreActiveLayers() in scripts.js after a style switch
window.agentRestoreLayers = agentAddMapLayers;


// -----------------------------------------------------------------------------
// 6. BOOT — runs after scripts.js has created the map
// -----------------------------------------------------------------------------

window.addEventListener('load', () => {
    agentBuildPanel();
    // Keyboard shortcut: "/" opens the assistant (when not typing elsewhere)
    document.addEventListener('keydown', (e) => {
        if (e.key === '/' && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) {
            e.preventDefault();
            agentTogglePanel(true);
        }
    });
});
