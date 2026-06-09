// ╔═══════════════════════════════════════════════════════════════════════════╗
// ║  KSA ROUTING & SERVICES — scripts.js                                     ║
// ║  Backend: pgRouting (PostGIS) + FastAPI + Elasticsearch                  ║
// ╠═══════════════════════════════════════════════════════════════════════════╣
// ║  SECTION INDEX  (Ctrl+F a section number to jump)                        ║
// ║  §1  Configuration & Constants      — URLs, colors, limits               ║
// ║  §2  Application State              — single state object                ║
// ║  §3  Map Initialization             — MapLibre setup                     ║
// ║  §4  Map Controls & UI Helpers      — layer switcher, panel toggle       ║
// ║  §5  Info Pointer Feature           — click-to-inspect any map layer     ║
// ║  §6  Draggable Info Panel           — mouse/touch drag logic             ║
// ║  §7  UI Initialization & Events     — sliders, search panel, listeners   ║
// ║  §8  Mode Switching                 — route / tsp / facility / service   ║
// ║  §9  Route Mode (A→B)               — pgRouting /route endpoint          ║
// ║  §10 TSP Mode                       — pgRouting /route/tsp endpoint      ║
// ║  §11 Nearest Facility Mode          — pgRouting /nearest_facility        ║
// ║  §12 Service Area Mode              — pgRouting /service_area            ║
// ║  §13 Utility Functions              — fetch, markers, showInfo           ║
// ║  §14 Restore Functions              — redraw layers after style switch    ║
// ║  §15 Cleanup Functions              — remove layers/markers, clearAll    ║
// ║  §16 Elasticsearch Search           — building/floor search + 3D zoom    ║
// ║  §17 Application Boot               — entry point                       ║
// ╠═══════════════════════════════════════════════════════════════════════════╣
// ║  BACKEND SWAP GUIDE                                                       ║
// ║  ─────────────────────────────────────────────────────────────────────   ║
// ║  This app uses pgRouting (PostGIS) for all routing.                       ║
// ║  To switch to Valhalla/VROOM (App 2 style):                              ║
// ║    §9  Replace calculateRoute()   → call POST /api/route (Valhalla)       ║
// ║         Response uses decodePolyline6() instead of GeoJSON features       ║
// ║    §10 Replace calculateTSP()     → call POST /api/optimize_route (VROOM) ║
// ║    §11 Replace calculateNearestFacilities() → call GET /nearest_facility  ║
// ║         (the FastAPI endpoint that uses Valhalla matrix + sources_targets) ║
// ║    §12 Replace calculateServiceArea() → call POST /api/isochrone          ║
// ║    §1  Update API_ENDPOINTS to match new backend paths                    ║
// ║  All map drawing functions (addRouteLayer, _renderFacilityRoutes, etc.)   ║
// ║  accept plain GeoJSON and need no changes.                                ║
// ╚═══════════════════════════════════════════════════════════════════════════╝


// ============================================================================
// §1  CONFIGURATION & CONSTANTS
// ============================================================================
// ─── Change values here to reconfigure the app without touching logic. ───────

// ── Map Styles ───────────────────────────────────────────────────────────────
// Add / remove entries to change which style buttons appear in the switcher.
// id       → internal key used by currentStyleId
// url      → path to the MapLibre style JSON served by your tile server
// pitch    → camera tilt applied when switching to this style (0 = top-down)
// bearing  → compass rotation applied when switching to this style
const STYLES = [
    { id: 'basic-style', name: 'Default',   url: 'styles/martin/style.json',     pitch: 0,  bearing: 0   },
    { id: 'sat-style',   name: 'Satellite', url: 'styles/martin/style_sat.json', pitch: 0,  bearing: 0   },
    { id: '3d-style',    name: '3D',        url: 'styles/martin/style_3d.json',  pitch: 45, bearing: 0   },
    { id: 'bdf-style',   name: 'BDF',       url: 'styles/martin/style_bdf.json', pitch: 60, bearing: -20 },
];

// ── Backend API Base URL ──────────────────────────────────────────────────────
// Nginx proxies /api/ → FastAPI (pgRouting wrapper) at routing-api:5000.
// SWAP GUIDE: Change BACKEND_URL to point at a different API host.
const BACKEND_URL = '/api';

// ── pgRouting API Endpoints ───────────────────────────────────────────────────
// SWAP GUIDE: Replace these paths if migrating to Valhalla/VROOM.
//   route          → POST /api/route        (Valhalla format)
//   tsp            → POST /api/optimize_route (VROOM format)
//   nearestFacility→ GET  /api/nearest_facility
//   serviceArea    → POST /api/isochrone    (Valhalla format)
const API_ENDPOINTS = {
    route:           `${BACKEND_URL}/route`,            // A→B routing (§9)
    tsp:             `${BACKEND_URL}/route/tsp`,         // Multi-point TSP (§10)
    nearestFacility: `${BACKEND_URL}/nearest_facility`,  // POI search + routing (§11)
    serviceArea:     `${BACKEND_URL}/service_area`,      // Reachable area polygon (§12)
};

// ── Elasticsearch URL ─────────────────────────────────────────────────────────
// SWAP GUIDE: Change to your Nginx proxy path (e.g. '/es') or a direct URL.
// Used in §16 (building search). Set to null to disable search entirely.
const ES_URL = 'http://localhost:9200';

// ── Map Defaults ──────────────────────────────────────────────────────────────
const DEFAULT_CENTER  = [46.6167, 24.8258]; // Riyadh [lng, lat]
const DEFAULT_ZOOM    = 12;
const REQUEST_TIMEOUT = 65000;              // ms — TSP/facility searches can be slow

// ── Facility Marker Colors (by type string) ───────────────────────────────────
// SWAP GUIDE: Keys must match _ALLOWED_FACILITY_TYPES in app.py and
//             <option value="…"> in index.html.
const FACILITY_COLORS = {
    'hospital':     '#ef4444',
    'fire station': '#f97316',
    'police':       '#8b5cf6',
    'clinic':       '#10b981',
};
const FACILITY_ICONS = {
    'hospital':     '🏥',
    'fire station': '🚒',
    'police':       '👮',
    'clinic':       '⚕️',
};

// ── Route Line Colors ─────────────────────────────────────────────────────────
// Index 0 = best/primary route; 1 & 2 = alternatives (drawn thinner).
const ROUTE_COLORS = ['#0865fc', '#4f9af7', '#6095d3'];

// ── TSP Waypoint + Leg Colors ─────────────────────────────────────────────────
// Each waypoint marker and the route leg leaving it share the same color.
const TSP_COLORS = [
    '#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6',
    '#ec4899', '#14b8a6', '#f97316', '#6366f1', '#84cc16',
];

// ── Slider Upper Limits ───────────────────────────────────────────────────────
// These are applied to the HTML range inputs in initializeSliders() (§7).
const MAX_FACILITY_COUNT    = 20;
const MAX_SERVICE_MINUTES   = 20;
const MAX_SEARCH_DISTANCE_KM = 30;


// ============================================================================
// §2  APPLICATION STATE
// ============================================================================
// One shared object holds everything that can change at runtime.
// All functions read from / write to this object instead of using globals.
// ─────────────────────────────────────────────────────────────────────────────
const state = {
    // ── Map object & camera ──────────────────────────────────────────────────
    map:           null,                // maplibregl.Map instance (set in §3)
    currentMode:   'route',            // Active tool: 'route' | 'tsp' | 'facility' | 'service'
    currentCenter: DEFAULT_CENTER,
    currentZoom:   DEFAULT_ZOOM,
    currentPitch:  0,
    currentBearing:0,

    // ── Map markers (pins placed by user clicks) ──────────────────────────────
    markers: {
        start:    null,   // Green "S" pin — route start
        end:      null,   // Red   "E" pin — route end
        service:  null,   // Blue truck    — service area origin
        facility: null,   // Red pin       — facility search origin
        tsp:      [],     // Array of { marker, lngLat, color } — TSP waypoints
    },

    // ── Facility result markers (separate so we can clear them independently) ─
    facilityMarkers: [],

    // ── Cached API responses (used to redraw layers after style switches) ──────
    lastRouteData:    null, // Array of GeoJSON FeatureCollections (§9)
    lastTSPRouteData: null, // { segments, waypoint_order }        (§10)
    lastFacilityData: null, // Full /nearest_facility response      (§11)
    lastServiceData:  null, // Full /service_area response          (§12)

    // ── User-controlled settings (driven by sliders / dropdowns) ─────────────
    serviceMinutes:    5,
    facilityCount:     5,
    searchDistanceKm:  10,
    routeOptimization:'fastest', // 'fastest' | 'shortest'
    showAlternatives:  true,

    // ── Info Pointer state ────────────────────────────────────────────────────
    infoPointerActive:      false,
    highlightedFeatureId:   null,
    highlightedSourceLayer: null,

    // ── Draggable Info Panel state ────────────────────────────────────────────
    isDragging:    false,
    dragOffset:    { x: 0, y: 0 },
    panelPosition: null,
};

// Module-level vars (not in state because they are internal implementation details)
let currentDataset    = 'spl_units'; // ES index: 'spl_units' | 'va_samples'
let currentStyleId    = 'basic-style';
let currentHighlightIds = [];        // IDs of 3D search-highlight layers
let currentPopup      = null;        // Currently open MapLibre Popup
let facilitySearchId  = 0;           // Stale-request guard for §11


// ============================================================================
// §3  MAP INITIALIZATION
// ============================================================================
// Entry point called on window load (§17).
// Sets up the MapLibre map, adds controls, wires all event handlers.
// ─────────────────────────────────────────────────────────────────────────────

function initMap() {
    // Create the MapLibre map inside <div id="map">
    state.map = new maplibregl.Map({
        container: 'map',
        style:     STYLES[0].url,
        center:    state.currentCenter,
        zoom:      state.currentZoom,
        pitch:     state.currentPitch,
        bearing:   state.currentBearing,
        maxPitch:  85,
    });

    // Keep camera state in sync so style switches can restore the view
    state.map.on('moveend', () => {
        state.currentCenter  = state.map.getCenter().toArray();
        state.currentZoom    = state.map.getZoom();
        state.currentPitch   = state.map.getPitch();
        state.currentBearing = state.map.getBearing();
    });

    // Navigation control (zoom + compass) — position changes on mobile
    if (window.innerWidth <= 600) {
        state.map.addControl(new maplibregl.NavigationControl(), 'bottom-left');
    } else {
        state.map.addControl(new maplibregl.NavigationControl(), 'top-right');
    }

    // Layer-switcher (Default / Satellite / 3D / BDF) — see §4
    state.map.addControl(createLayerSwitcher(), 'bottom-right');

    // Initialize slider max values from constants (§7)
    initializeSliders();

    // Initialize search panel expand/collapse logic (§7)
    initializeSearchPanel();

    // Inject ℹ️ info-pointer button + ◀ panel-toggle button (§4)
    injectMapButtons();

    // Wire all click/change listeners (§7)
    setupEventHandlers();

    // Resize handler: re-expand search panel on desktop rotation
    window.addEventListener('resize', () => {
        setTimeout(() => {
            const searchPanel = document.querySelector('.search-panel');
            if (searchPanel && window.innerWidth > 600) {
                searchPanel.classList.add('expanded');
            }
        }, 200);
    });

    // Show initial hint once the map finishes loading
    state.map.on('load', () => {
        showInfo('Click anywhere to begin routing of two points');
    });
}


// ============================================================================
// §4  MAP CONTROLS & UI HELPERS
// ============================================================================
// Custom MapLibre controls and panel toggle logic.
// ─────────────────────────────────────────────────────────────────────────────

// ── Layer Switcher ────────────────────────────────────────────────────────────
// Builds the Default / Satellite / 3D / BDF button group (bottom-right).
// Changing a style wipes all custom layers → switchMapStyle() redraws them.
function createLayerSwitcher() {
    class LayerSwitcher {
        onAdd(map) {
            this.map = map;
            this.container = document.createElement('div');
            this.container.className = 'maplibregl-ctrl maplibregl-ctrl-group flex flex-col bg-white';

            STYLES.forEach(style => {
                const btn = document.createElement('button');
                btn.type      = 'button';
                btn.className = 'px-4 py-3 text-sm font-medium hover:bg-blue-50 border-b border-gray-200 transition';
                btn.textContent = style.name === '3D'  ? '3D'
                                : style.name === 'BDF' ? 'BDF'
                                : style.name.charAt(0);
                btn.onclick = () => switchMapStyle(style);
                this.container.appendChild(btn);
            });

            return this.container;
        }
        onRemove() {
            this.container.parentNode?.removeChild(this.container);
        }
    }
    return new LayerSwitcher();
}

// ── Style Switch ──────────────────────────────────────────────────────────────
// Saves camera, loads new style, then redraws all active custom layers.
// Called by the layer-switcher buttons above.
async function switchMapStyle(style) {
    currentStyleId = style.id;

    const center  = state.map.getCenter();
    const zoom    = style.zoom    ?? state.map.getZoom();
    const pitch   = style.pitch   ?? state.map.getPitch();
    const bearing = style.bearing ?? state.map.getBearing();

    state.map.setStyle(style.url);

    state.map.once('idle', () => {
        state.map.jumpTo({ center, zoom, pitch, bearing });

        // Rebuild info-pointer highlight layers if that tool is active
        if (state.infoPointerActive) setupInfoPointerLayers();

        // Redraw layers for the current mode (§14)
        switch (state.currentMode) {
            case 'route':    restoreRouteLayers();   break;
            case 'tsp':      restoreTSPRoute();      break;
            case 'facility': restoreFacilityData();  break;
            case 'service':  restoreServiceArea();   break;
        }
    });
}

// ── Inject Map Buttons ────────────────────────────────────────────────────────
// Adds the ℹ️ Info Pointer button and ◀ Panel Toggle button to the map UI.
// These live outside the sidebar so they're always visible over the map.
function injectMapButtons() {
    const topRight = document.querySelector('.maplibregl-ctrl-top-right');
    if (!topRight) return;

    const ctrlGroup = document.createElement('div');
    ctrlGroup.className = 'maplibregl-ctrl maplibregl-ctrl-group';
    ctrlGroup.id = 'custom-map-controls';

    // ℹ️ button — toggles the info-pointer mode (§5)
    const infoBtn = document.createElement('button');
    infoBtn.className = 'info-pointer-btn';
    infoBtn.type      = 'button';
    infoBtn.innerHTML = 'ℹ️';
    infoBtn.title     = 'Toggle Info Pointer';
    infoBtn.addEventListener('click', () => toggleInfoPointer());

    // ◀ button — collapses / expands the main control panel
    const panelBtn = document.createElement('button');
    panelBtn.className = 'panel-toggle-btn';
    panelBtn.type      = 'button';
    panelBtn.innerHTML = '◀';
    panelBtn.title     = 'Hide Control Panel';
    panelBtn.addEventListener('click', () => toggleControlPanel(panelBtn));

    ctrlGroup.appendChild(infoBtn);
    ctrlGroup.appendChild(panelBtn);

    // On mobile put the group in bottom-left alongside NavigationControl
    if (window.innerWidth <= 600) {
        const bottomLeft = document.querySelector('.maplibregl-ctrl-bottom-left') || createBottomLeftContainer();
        bottomLeft.appendChild(ctrlGroup);
    } else {
        topRight.appendChild(ctrlGroup);
    }

    // Clicking the minimised pill header expands the panel again
    const panel = document.querySelector('.control-panel.header');
    if (panel) {
        panel.addEventListener('click', function expandIfMinimised(e) {
            if (e.target.closest('button, input, select, label, .mode-btn, .route-opt-btn, .range-slider')) return;
            if (!panel.classList.contains('panel-minimised')) return;
            toggleControlPanel(panelBtn);
        });
    }
}

// ── Panel Toggle ──────────────────────────────────────────────────────────────
// Collapses the main control panel to a pill or expands it back.
function toggleControlPanel(btn) {
    const panel = document.querySelector('.control-panel.header');
    if (!panel) return;

    const isMinimised = panel.classList.toggle('panel-minimised');
    btn.classList.toggle('active', isMinimised);
    btn.innerHTML = isMinimised ? '▶' : '◀';
    btn.title     = isMinimised ? 'Show Control Panel' : 'Hide Control Panel';

    if (isMinimised) {
        panel._expandHandler = (e) => {
            if (e.target.closest('button,input,select,label,.mode-btn,.route-opt-btn,.range-slider')) return;
            toggleControlPanel(btn);
        };
        panel.addEventListener('click', panel._expandHandler);
    } else {
        if (panel._expandHandler) {
            panel.removeEventListener('click', panel._expandHandler);
            delete panel._expandHandler;
        }
    }
}

// ── Bottom-left container helper ──────────────────────────────────────────────
// Creates the MapLibre bottom-left control container if it doesn't exist yet.
function createBottomLeftContainer() {
    let el = document.querySelector('.maplibregl-ctrl-bottom-left');
    if (!el) {
        el = document.createElement('div');
        el.className = 'maplibregl-ctrl-bottom-left';
        document.getElementById('map').appendChild(el);
    }
    return el;
}


// ============================================================================
// §5  INFO POINTER FEATURE
// ============================================================================
// When active: clicking any map feature shows its raw GeoJSON properties in
// the floating Feature Info Panel. Useful for inspecting road types, building
// attributes, etc. without switching away from the routing tool.
//
// TO REMOVE: delete this section and the injectMapButtons() ℹ️ button.
// ─────────────────────────────────────────────────────────────────────────────

// Toggle info-pointer ON / OFF
function toggleInfoPointer() {
    state.infoPointerActive = !state.infoPointerActive;

    const btn          = document.querySelector('.info-pointer-btn');
    const mapContainer = document.getElementById('map');
    const featurePanel = document.getElementById('feature-info-panel');
    const panelToggleBtn = document.querySelector('.panel-toggle-btn');

    if (state.infoPointerActive) {
        setupInfoPointerLayers();     // Create highlight source + layers
        btn.classList.add('active');
        mapContainer.classList.add('info-pointer-active');
        featurePanel.classList.remove('hidden');
        resetPanelPosition();         // Move info panel to top-right
        setupDraggablePanel();        // Allow user to drag the info panel
        clearFeatureHighlight();
        updateFeatureInfo({ html: '<p class="info-hint">Click on any feature to see its details</p>' });

        // Auto-minimise the routing panel so it doesn't block the map
        const controlPanel = document.querySelector('.control-panel.header');
        if (controlPanel && !controlPanel.classList.contains('panel-minimised')) {
            toggleControlPanel(panelToggleBtn);
        }

        // Add grab cursor while panning the map in info mode
        state.map.on('mousedown', onMapMouseDown);
        document.addEventListener('mouseup', onMapMouseUp);
    } else {
        btn.classList.remove('active');
        mapContainer.classList.remove('info-pointer-active');
        featurePanel.classList.add('hidden');
        clearFeatureHighlight();
        state.map.off('mousedown', onMapMouseDown);
        document.removeEventListener('mouseup', onMapMouseUp);
    }
}

// Create the GeoJSON source + three highlight layers (fill / line / circle)
// that light up the feature the user clicked.
function setupInfoPointerLayers() {
    if (!state.map.getSource('feature-highlight')) {
        state.map.addSource('feature-highlight', {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: [] },
        });
    }

    const layers = [
        {
            id:     'feature-highlight-fill',
            type:   'fill',
            filter: ['==', ['geometry-type'], 'Polygon'],
            paint:  { 'fill-color': '#3b82f6', 'fill-opacity': 0.3 },
        },
        {
            id:     'feature-highlight-line',
            type:   'line',
            filter: ['any', ['==', ['geometry-type'], 'LineString'], ['==', ['geometry-type'], 'Polygon']],
            paint:  { 'line-color': '#3b82f6', 'line-width': 3, 'line-opacity': 0.8 },
        },
        {
            id:     'feature-highlight-point',
            type:   'circle',
            filter: ['==', ['geometry-type'], 'Point'],
            paint:  { 'circle-radius': 8, 'circle-color': '#3b82f6', 'circle-opacity': 0.6, 'circle-stroke-width': 2, 'circle-stroke-color': '#1e40af' },
        },
    ];

    layers.forEach(layer => {
        if (!state.map.getLayer(layer.id)) {
            state.map.addLayer({ ...layer, source: 'feature-highlight' });
        }
    });
}

// Handle a map click while info pointer is active — find & highlight the feature
function handleInfoPointerClick(e) {
    if (!state.infoPointerActive || state.isDragging) return;

    const features = state.map.queryRenderedFeatures(e.point);
    if (!features || features.length === 0) {
        clearFeatureHighlight();
        updateFeatureInfo({ html: '<p class="info-hint">No features found at this location</p>' });
        return;
    }

    // Filter out our own overlay layers — only inspect base-map features
    const validFeatures = features.filter(f => {
        const id = f.layer.id;
        return !id.startsWith('feature-highlight') &&
               !id.startsWith('route')             &&
               !id.startsWith('tsp-segment')       &&
               !id.startsWith('service-')          &&
               !id.startsWith('facility-');
    });

    if (validFeatures.length === 0) {
        clearFeatureHighlight();
        updateFeatureInfo({ html: '<p class="info-hint">No base layer features at this location</p>' });
        return;
    }

    const feature = validFeatures[0]; // Topmost base-layer feature
    updateFeatureHighlight(feature);
    displayFeatureInfo(feature);
}

// Push the clicked feature's geometry into the highlight source
function updateFeatureHighlight(feature) {
    clearFeatureHighlight();
    state.highlightedFeatureId   = feature.id;
    state.highlightedSourceLayer = feature.sourceLayer;
    const src = state.map.getSource('feature-highlight');
    if (src) src.setData({ type: 'FeatureCollection', features: [feature] });
}

// Render feature properties as an HTML table in the info panel
function displayFeatureInfo(feature) {
    const properties = feature.properties || {};
    const geomType   = feature.geometry.type;

    let html = `
        <div class="feature-layer-info">
            <p><strong>Layer:</strong> ${feature.layer.id}</p>
            <p><strong>Source Layer:</strong> ${feature.sourceLayer || 'N/A'}</p>
            <p><strong>Geometry:</strong> ${geomType}</p>
        </div>`;

    if (Object.keys(properties).length > 0) {
        html += '<div class="feature-properties">';
        Object.keys(properties).sort().forEach(key => {
            const val = properties[key];
            if (val === null || val === undefined) return;
            const formatted = typeof val === 'object'
                ? JSON.stringify(val)
                : typeof val === 'number'
                ? val.toLocaleString()
                : val;
            html += `
                <div class="feature-property">
                    <span class="property-key">${formatPropertyKey(key)}</span>
                    <span class="property-value">${formatted}</span>
                </div>`;
        });
        html += '</div>';
    } else {
        html += '<p class="info-hint">No properties available for this feature</p>';
    }

    updateFeatureInfo({ html });
}

// Convert raw property key (e.g. "building_height" / "UNIT_ID") to a
// human-readable label.
function formatPropertyKey(key) {
    if (key === key.toUpperCase()) return key.replace(/_/g, ' ').trim();
    return key
        .replace(/_/g, ' ')
        .replace(/([A-Z])/g, ' $1')
        .replace(/^./, s => s.toUpperCase())
        .trim();
}

// Write HTML into the feature info panel content area
function updateFeatureInfo({ html }) {
    const content = document.getElementById('feature-info-content');
    if (content) content.innerHTML = html;
}

// Clear the highlight overlay (reset to empty FeatureCollection)
function clearFeatureHighlight() {
    state.highlightedFeatureId   = null;
    state.highlightedSourceLayer = null;
    const src = state.map.getSource('feature-highlight');
    if (src) src.setData({ type: 'FeatureCollection', features: [] });
}

// Cursor helpers — show grab cursor while the user pans the map in info mode
function onMapMouseDown(e) {
    const mapContainer = document.getElementById('map');
    if (!mapContainer) return;
    mapContainer.classList.add('map-info');
    const onMouseMove = () => {
        mapContainer.classList.remove('map-info');
        mapContainer.classList.add('map-panning');
    };
    document.addEventListener('mousemove', onMouseMove, { once: true });
    document.addEventListener('mouseup', () => {
        mapContainer.classList.remove('map-info', 'map-panning');
        document.removeEventListener('mousemove', onMouseMove);
    }, { once: true });
}
function onMapMouseUp() {
    const mapContainer = document.getElementById('map');
    if (mapContainer) mapContainer.classList.remove('map-info', 'map-panning');
}


// ============================================================================
// §6  DRAGGABLE INFO PANEL
// ============================================================================
// Lets the user drag the Feature Info Panel to any screen position.
// Uses mousedown → mousemove → mouseup for desktop; touch events for mobile.
//
// TO REMOVE: delete this section and the setupDraggablePanel() call in §5.
// ─────────────────────────────────────────────────────────────────────────────

function setupDraggablePanel() {
    const panel  = document.getElementById('feature-info-panel');
    const header = document.querySelector('.feature-info-header');
    if (!panel || !header) return;

    // Remove stacked listeners from previous activations
    if (panel._dragStart) {
        header.removeEventListener('mousedown',  panel._dragStart);
        header.removeEventListener('touchstart', panel._dragStart);
        document.removeEventListener('mousemove', panel._drag);
        document.removeEventListener('touchmove', panel._drag);
        document.removeEventListener('mouseup',   panel._dragEnd);
        document.removeEventListener('touchend',  panel._dragEnd);
    }

    let isDragging = false;
    let initialX = 0, initialY = 0;

    const dragStart = (e) => {
        if (e.target.closest('.close-btn')) return;
        const touch = e.touches?.[0] || e;
        const rect  = panel.getBoundingClientRect();
        initialX    = touch.clientX - rect.left;
        initialY    = touch.clientY - rect.top;
        isDragging  = true;
        state.isDragging = true;
        panel.style.right     = 'auto';
        panel.style.left      = rect.left + 'px';
        panel.style.top       = rect.top  + 'px';
        panel.style.transform = 'none';
    };

    const drag = (e) => {
        if (!isDragging) return;
        e.preventDefault();
        const touch = e.touches?.[0] || e;
        const newX  = Math.max(0, Math.min(touch.clientX - initialX, window.innerWidth  - panel.offsetWidth));
        const newY  = Math.max(0, Math.min(touch.clientY - initialY, window.innerHeight - panel.offsetHeight));
        panel.style.left = newX + 'px';
        panel.style.top  = newY + 'px';
    };

    const dragEnd = () => {
        isDragging = false;
        state.isDragging = false;
    };

    header.addEventListener('mousedown',  dragStart);
    document.addEventListener('mousemove', drag);
    document.addEventListener('mouseup',   dragEnd);
    header.addEventListener('touchstart', dragStart, { passive: false });
    document.addEventListener('touchmove', drag,     { passive: false });
    document.addEventListener('touchend',  dragEnd);

    // Store references so they can be cleaned up next time
    panel._dragStart = dragStart;
    panel._drag      = drag;
    panel._dragEnd   = dragEnd;
}

// Reset info panel to its default position (top-right corner)
function resetPanelPosition() {
    const panel = document.getElementById('feature-info-panel');
    if (!panel) return;
    panel.style.left      = 'auto';
    panel.style.right     = '1rem';
    panel.style.top       = '1rem';
    panel.style.bottom    = 'auto';
    panel.style.transform = 'none';
    state.panelPosition   = null;
}


// ============================================================================
// §7  UI INITIALIZATION & EVENT HANDLERS
// ============================================================================
// Runs once at startup to configure sliders, search panel, and all listeners.
// ─────────────────────────────────────────────────────────────────────────────

// Apply constant-driven max values to the three range sliders
function initializeSliders() {
    const fc = document.getElementById('facility-count-input');
    if (fc) fc.max = MAX_FACILITY_COUNT;
    const di = document.getElementById('distance-input');
    if (di) di.max = MAX_SEARCH_DISTANCE_KM;
    const ti = document.getElementById('time-input');
    if (ti) ti.max = MAX_SERVICE_MINUTES;
}

// Search panel — desktop: starts expanded; mobile: starts as a pill
function initializeSearchPanel() {
    const searchPanel = document.querySelector('.search-panel');
    if (!searchPanel) return;

    const isPhone = window.innerWidth <= 600;
    if (isPhone) {
        searchPanel.classList.remove('expanded');
    } else {
        searchPanel.classList.add('expanded');
    }

    if (isPhone) {
        const pill = searchPanel.querySelector('.search-panel-pill');
        if (pill) {
            pill.addEventListener('click', () => {
                searchPanel.classList.add('expanded');
                setTimeout(() => searchPanel.querySelector('#searchInput')?.focus(), 350);
            });
        }
        document.addEventListener('click', (e) => {
            if (searchPanel.classList.contains('expanded') && !searchPanel.contains(e.target)) {
                searchPanel.classList.remove('expanded');
            }
        });
    }
}

// Wire all interactive elements to their handlers
function setupEventHandlers() {
    // All map clicks → single gate
    state.map.on('click', (e) => {
        if (state.infoPointerActive) handleInfoPointerClick(e);
        else                          handleMapClick(e);
    });

    // Feature Info Panel — close button
    document.getElementById('close-feature-info')?.addEventListener('click', () => {
        state.infoPointerActive = true; // flip to true so toggle() turns it off
        toggleInfoPointer();
    });

    // Mode switcher buttons (§8)
    ['route', 'tsp', 'facility', 'service'].forEach(mode => {
        document.getElementById(`mode-${mode}`)?.addEventListener('click', () => switchMode(mode));
    });

    // Route optimization buttons (§9)
    document.getElementById('opt-fastest')?.addEventListener('click', () => {
        state.routeOptimization = 'fastest';
        updateRouteOptButtons();
        if (state.markers.start && state.markers.end)
            calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());
    });
    document.getElementById('opt-shortest')?.addEventListener('click', () => {
        state.routeOptimization = 'shortest';
        updateRouteOptButtons();
        if (state.markers.start && state.markers.end)
            calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());
    });

    // Alternatives checkbox (§9)
    document.getElementById('show-alternatives')?.addEventListener('change', (e) => {
        state.showAlternatives = e.target.checked;
        if (state.markers.start && state.markers.end)
            calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());
    });

    // Service time slider (§12)
    const timeInput = document.getElementById('time-input');
    const timeValue = document.getElementById('time-value');
    if (timeInput && timeValue) {
        timeValue.textContent = timeInput.value;
        timeInput.addEventListener('input', (e) => {
            state.serviceMinutes  = parseInt(e.target.value, 10);
            timeValue.textContent = state.serviceMinutes;
            if (state.markers.service) calculateServiceArea(state.markers.service.getLngLat());
        });
    }

    // Facility count slider (§11)
    const facilityCountInput = document.getElementById('facility-count-input');
    const facilityCountValue = document.getElementById('facility-count-value');
    if (facilityCountInput && facilityCountValue) {
        facilityCountValue.textContent = facilityCountInput.value;
        facilityCountInput.addEventListener('input', (e) => {
            state.facilityCount = parseInt(e.target.value, 10);
            facilityCountValue.textContent = state.facilityCount;
            if (state.markers.facility) calculateNearestFacilities(state.markers.facility.getLngLat());
        });
    }

    // Search radius slider (§11)
    const distanceInput = document.getElementById('distance-input');
    const distanceValue = document.getElementById('distance-value');
    if (distanceInput && distanceValue) {
        distanceValue.textContent = distanceInput.value;
        distanceInput.addEventListener('input', (e) => {
            state.searchDistanceKm = parseInt(e.target.value, 10);
            distanceValue.textContent = state.searchDistanceKm;
            if (state.markers.facility) calculateNearestFacilities(state.markers.facility.getLngLat());
        });
    }

    // Elasticsearch — Enter key triggers search (§16)
    document.getElementById('searchInput')?.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') searchUnits();
    });

    // Dataset radio buttons (§16)
    document.querySelectorAll('input[name="dataset"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            currentDataset = e.target.value;
            document.getElementById('results').innerHTML = '';
            clearHighlight();
        });
    });
}

// Highlight the active route-optimization button (Fastest / Shortest)
function updateRouteOptButtons() {
    document.querySelectorAll('.route-opt-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById(state.routeOptimization === 'fastest' ? 'opt-fastest' : 'opt-shortest')
        ?.classList.add('active');
}

// Dispatch map clicks to the correct mode handler
function handleMapClick(e) {
    const handlers = {
        route:    handleRouteClick,
        tsp:      handleTSPClick,
        facility: handleFacilityClick,
        service:  handleServiceClick,
    };
    const handler = handlers[state.currentMode];
    if (handler) handler(e.lngLat);
    else console.warn(`Unknown mode: ${state.currentMode}`);
}


// ============================================================================
// §8  MODE SWITCHING
// ============================================================================
// Handles transitions between the four tool modes.
// clearAll() wipes all markers and layers before entering a new mode.
// ─────────────────────────────────────────────────────────────────────────────

function switchMode(mode) {
    clearAll();                    // §15 — remove all markers and layers
    state.currentMode = mode;
    updateModeButtons(mode);
    updateModeInstructions(mode);

    // Deactivate info pointer when the user switches mode
    if (state.infoPointerActive) toggleInfoPointer();
}

function updateModeButtons(activeMode) {
    document.querySelectorAll('.mode-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById(`mode-${activeMode}`)?.classList.add('active');
}

// Show / hide mode-specific controls and update the instruction text
function updateModeInstructions(mode) {
    document.getElementById('route-options')?.classList.toggle('hidden',               mode !== 'route');
    document.getElementById('facility-selector')?.classList.toggle('hidden',           mode !== 'facility');
    document.getElementById('facility-sliders-container')?.classList.toggle('hidden',  mode !== 'facility');
    document.getElementById('time-slider')?.classList.toggle('hidden',                 mode !== 'service');

    const instructions = {
        route:    { html: 'Click: <span class="highlight start">Start</span> → <span class="highlight end">End</span>',                  info: '🗺️ A→B Route mode active' },
        tsp:      { html: 'Click to add <span style="color:#8b5cf6;font-weight:bold">waypoints</span> (min 3)',                           info: '🔄 TSP mode: Add at least 3 points' },
        facility: { html: 'Click a <span style="color:#ef4444;font-weight:bold">location</span> to find nearest facilities',              info: '🏥 Nearest Facility mode active' },
        service:  { html: 'Click <span style="color:#ef4444;font-weight:bold">service location</span> + adjust time',                    info: '🚚 Service Area mode active' },
    };

    const cfg = instructions[mode];
    if (cfg) {
        const el = document.getElementById('mode-instruction');
        if (el) el.innerHTML = cfg.html;
        showInfo(cfg.info);
    }
}


// ============================================================================
// §9  ROUTE MODE  (A→B, pgRouting)
// ============================================================================
// Click 1 → green "S" marker.  Click 2 → red "E" marker + route calculated.
// Click 3 → resets.
//
// BACKEND: GET /api/route?start_lon=…&end_lon=…&alternatives=N&optimization=…
// Response: { routes: [ { type:"FeatureCollection", features:[], duration_minutes, total_distance_km } ] }
//
// SWAP TO VALHALLA:
//   1. Call POST /api/route with { locations, costing, costing_options }
//   2. Response: { trip: { legs: [{ shape: "encoded…" }], summary } }
//   3. Decode shape: trip.legs.flatMap(l => decodePolyline6(l.shape))
//   4. Pass coordinates to _addRouteLayer() (already accepts plain LineString coords)
// ─────────────────────────────────────────────────────────────────────────────

function handleRouteClick(lngLat) {
    if (!state.markers.start) {
        state.markers.start = new maplibregl.Marker({ element: createMarker('S', '#10b981') })
            .setLngLat(lngLat).addTo(state.map);
        showInfo('✅ Start set. Click destination');

    } else if (!state.markers.end) {
        state.markers.end = new maplibregl.Marker({ element: createMarker('E', '#ef4444') })
            .setLngLat(lngLat).addTo(state.map);
        calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());

    } else {
        clearAll();
        showInfo('🔄 Cleared. Click new start point');
    }
}

async function calculateRoute(start, end) {
    clearRouteLayers();
    showInfo('⏳ Calculating routes...');

    try {
        const url = `${API_ENDPOINTS.route}?start_lon=${start.lng}&start_lat=${start.lat}&end_lon=${end.lng}&end_lat=${end.lat}&alternatives=${state.showAlternatives ? 3 : 1}&optimization=${state.routeOptimization}`;
        const data = await fetchWithTimeout(url);

        if (data.error) throw new Error(data.error);
        const routes = data.routes;
        if (!routes || routes.length === 0) throw new Error('No routes returned by the server.');

        state.lastRouteData = routes; // Save for §14 restore

        routes.forEach((route, i) => {
            addRouteLayer(route, ROUTE_COLORS[i] || '#5e8bbe', i === 0 ? 0.9 : 0.6, i === 0 ? 7 : 5, `route-${i}`);
        });

        routes.length > 1 ? fitToMultipleRoutes(routes) : fitToFeatures(routes[0]);

        const best = routes[0];
        let summary = `✅ <strong>Best ${state.routeOptimization} route:</strong> ${best.duration_minutes} min • ${best.total_distance_km} km`;
        if (routes.length > 1) {
            summary += `<br><small>Showing ${routes.length} of ${data.requested} requested routes</small>`;
            routes.slice(1).forEach((r, i) => {
                const color = ROUTE_COLORS[i + 1] || '#5e8bbe';
                summary += `<br><small style="color:${color};">● Route ${i + 2}: ${r.duration_minutes} min • ${r.total_distance_km} km</small>`;
            });
        }
        showInfo(summary);

    } catch (error) {
        handleError('Route calculation', error);
    }
}

function fitToMultipleRoutes(routes) {
    const bounds = new maplibregl.LngLatBounds();
    routes.forEach(route => {
        route.features.forEach(f => {
            if (f.geometry?.coordinates) f.geometry.coordinates.forEach(c => bounds.extend(c));
        });
    });
    state.map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 1500 });
}


// ============================================================================
// §10  TSP MODE  (multi-point, pgRouting)
// ============================================================================
// Click to add numbered waypoints. After the 3rd point a 2-second timer
// fires and the optimised route is calculated automatically.
//
// BACKEND: POST /api/route/tsp  { points: [[lon,lat],…] }
// Response: { segments: [FeatureCollection,…], waypoint_order: [0,2,1,…], … }
//
// SWAP TO VROOM:
//   1. Call POST /api/optimize_route with { vehicles, jobs }
//   2. Response: { vroom: { routes: [{ steps, duration, distance }] }, geojson }
//   3. Re-fetch individual legs via POST /api/route for segment-coloring
//   See App 2 _runVRP() for the complete pattern.
// ─────────────────────────────────────────────────────────────────────────────

function handleTSPClick(lngLat) {
    // Guard against duplicate / too-close clicks
    const MIN_DISTANCE_METERS = 100;
    const isTooClose = state.markers.tsp.some(item => {
        const dx = item.lngLat.lng - lngLat.lng;
        const dy = item.lngLat.lat - lngLat.lat;
        return Math.sqrt(dx * dx + dy * dy) * 111320 < MIN_DISTANCE_METERS;
    });
    if (isTooClose) {
        showInfo('⚠️ This point is too close to an existing waypoint.<br>Please click somewhere else.');
        return;
    }

    const num   = state.markers.tsp.length + 1;
    const color = TSP_COLORS[(num - 1) % TSP_COLORS.length];

    const marker = new maplibregl.Marker({ element: createMarker(num.toString(), color) })
        .setLngLat(lngLat).addTo(state.map);

    state.markers.tsp.push({ marker, lngLat, color });

    if (state.markers.tsp.length < 3) {
        showInfo(`✅ Point ${num} added. Need ${3 - state.markers.tsp.length} more (min 3)`);
    } else {
        showInfo(`✅ Point ${num} added. Auto-calculating in 2 seconds...`);
        setTimeout(() => {
            if (state.markers.tsp.length >= 3) calculateTSP();
        }, 2000);
    }
}

async function calculateTSP() {
    if (state.markers.tsp.length < 3) { showInfo('❌ Need at least 3 points for TSP'); return; }
    showInfo(`⏳ Solving TSP for ${state.markers.tsp.length} points...`);

    try {
        const points = state.markers.tsp.map(m => [m.lngLat.lng, m.lngLat.lat]);
        const data   = await fetchWithTimeout(API_ENDPOINTS.tsp, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ points }),
        });

        if (data.error) throw new Error(data.error);

        clearTSPLayers();

        state.lastTSPRouteData = {        // Save for §14 restore
            segments:       data.segments,
            waypoint_order: data.waypoint_order,
        };

        drawTSPSegments(data.segments, data.waypoint_order);

        if (data.segments?.length > 0) fitToFeatures(data.segments[0]);

        const orderStr = data.waypoint_order
            .map(idx => `<span style="color:${TSP_COLORS[idx % TSP_COLORS.length]};font-weight:bold;">${idx + 1}</span>`)
            .join(' <span style="color:#6b7280;">→</span> ');

        showInfo(`
            ✅ <strong>TSP Optimized!</strong><br>
            Order: ${orderStr}<br>
            ${data.duration_minutes} min • ${data.total_distance_km} km • ${data.segment_count} legs
        `);

    } catch (error) {
        handleError('TSP calculation', error);
    }
}

// Each leg i uses the color of waypoint_order[i] (the starting point of that leg)
function drawTSPSegments(segments, waypoint_order) {
    if (!segments || segments.length === 0) return;
    segments.forEach((segmentData, i) => {
        const startWaypointIdx = waypoint_order ? waypoint_order[i] : i;
        const color            = TSP_COLORS[startWaypointIdx % TSP_COLORS.length];
        addRouteLayer(segmentData, color, 0.95, 6.5, `tsp-segment-${i}`);
    });
}


// ============================================================================
// §11  NEAREST FACILITY MODE  (pgRouting)
// ============================================================================
// Click → place search pin → API finds facilities within radius and routes
// to each one. Results shown as colored markers + route lines.
//
// BACKEND: GET /api/nearest_facility?lon=…&lat=…&type=…&limit=…&max_distance_km=…&routes=true
// Response: { facilities: [{ name, type, address, facility_lon, facility_lat,
//                             travel_minutes, crow_distance_km,
//                             route: { type:"FeatureCollection", features:[] } }] }
//
// SWAP TO VALHALLA (App 2 pattern):
//   1. Call GET /api/nearest_facility (FastAPI wraps PostGIS + Valhalla matrix)
//   2. The response shape is identical — no JS changes needed.
//   3. Only change: update API_ENDPOINTS.nearestFacility if the path differs.
// ─────────────────────────────────────────────────────────────────────────────

function handleFacilityClick(lngLat) {
    clearFacilityData(); // Remove previous pin, markers, and route layers

    state.markers.facility = new maplibregl.Marker({ element: createMarker('📍', '#dc2626') })
        .setLngLat(lngLat).addTo(state.map);

    showInfo('⏳ Searching nearest facilities...');
    calculateNearestFacilities(lngLat);
}

async function calculateNearestFacilities(lngLat) {
    const mySearchId   = ++facilitySearchId; // Stale-request guard
    const facilityType = document.getElementById('facility-type-select')?.value || 'hospital';

    showInfo(`⏳ Searching for nearest ${facilityType}s within ${state.searchDistanceKm}km...<br><small>This may take a few seconds</small>`);

    try {
        const url  = `${API_ENDPOINTS.nearestFacility}?lon=${lngLat.lng}&lat=${lngLat.lat}&type=${encodeURIComponent(facilityType)}&limit=${state.facilityCount}&max_distance_km=${state.searchDistanceKm}&routes=true`;
        const data = await fetchWithTimeout(url);

        if (mySearchId !== facilitySearchId) return; // Superseded — discard

        if (data.error) throw new Error(data.error);
        if (!data.facilities || data.facilities.length === 0) {
            showInfo(`ℹ️ No ${facilityType} found within ${state.searchDistanceKm}km radius`);
            return;
        }

        displayFacilityResults(lngLat, data, facilityType);

    } catch (error) {
        if (mySearchId === facilitySearchId) handleError('Facility search', error);
    }
}

// Draw route lines and facility markers; save data for §14 restore
function displayFacilityResults(lngLat, data, facilityType) {
    removeFacilityLayers();
    removeFacilityMarkers();

    // Flatten all per-facility route features into one FeatureCollection
    const allRouteFeatures = [];
    data.facilities.forEach((facility, index) => {
        if (facility.route?.features) {
            facility.route.features.forEach(f => {
                allRouteFeatures.push({
                    ...f,
                    properties: {
                        ...f.properties,
                        facility_name: facility.name,
                        facility_rank: index + 1,
                        travel_minutes: facility.travel_minutes,
                    },
                });
            });
        }
    });

    // Add route lines
    if (allRouteFeatures.length > 0) {
        const facilityColor = FACILITY_COLORS[facilityType] || '#6366f1';
        state.map.addSource('facility-routes', {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: allRouteFeatures },
        });
        state.map.addLayer({
            id: 'facility-routes', type: 'line', source: 'facility-routes',
            paint: {
                'line-color': facilityColor,
                'line-width': ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 6, 5, 3],
                'line-opacity': 0.8,
            },
        });
    }

    // Create facility markers with hover popups
    state.facilityMarkers = data.facilities.map((facility, index) => {
        const icon  = FACILITY_ICONS[facility.type]  || '📍';
        const color = FACILITY_COLORS[facility.type] || '#6366f1';

        const el = document.createElement('div');
        el.className = 'facility-marker';
        el.style.background = color;
        el.innerHTML = icon;

        const badge = document.createElement('div');
        badge.className = 'rank-badge';
        badge.style.borderColor = color;
        badge.style.color = color;
        badge.textContent = index + 1;
        el.appendChild(badge);

        const popup = new maplibregl.Popup({ offset: 25, closeButton: true, closeOnClick: true, maxWidth: '300px' })
            .setHTML(`
                <div style="font-family:Inter,sans-serif;min-width:220px;">
                    <div style="font-size:24px;margin-bottom:8px;">${icon}</div>
                    <strong style="font-size:14px;color:#1f2937;">${facility.name}</strong>
                    ${facility.address ? `<p style="margin:4px 0;font-size:12px;color:#6b7280;">${facility.address}</p>` : ''}
                    <p style="margin:8px 0 0;font-size:13px;color:#059669;">
                        <strong>⏱️ ${facility.travel_minutes} minutes</strong> drive
                    </p>
                    <p style="margin:4px 0 0;font-size:11px;color:#9ca3af;">
                        Rank: #${index + 1}
                        ${facility.crow_distance_km ? ` • ${parseFloat(facility.crow_distance_km).toFixed(1)} km straight-line` : ''}
                    </p>
                </div>`);

        const marker = new maplibregl.Marker({ element: el, anchor: 'center', offset: [0, -6] })
            .setLngLat([parseFloat(facility.facility_lon), parseFloat(facility.facility_lat)])
            .setPopup(popup).addTo(state.map);

        marker._popup = popup;

        let closeTimeout;
        el.addEventListener('mouseenter', () => {
            clearTimeout(closeTimeout);
            if (!marker.getPopup().isOpen()) marker.togglePopup();
        });
        el.addEventListener('mouseleave', () => {
            closeTimeout = setTimeout(() => { if (marker.getPopup().isOpen()) marker.togglePopup(); }, 200);
        });

        return marker;
    });

    state.lastFacilityData = data; // Save for §14 restore

    // Build info box summary
    const closest = data.facilities[0];
    const icon    = FACILITY_ICONS[facilityType] || '📍';
    const list    = data.facilities.map((f, i) =>
        `${i + 1}. ${FACILITY_ICONS[f.type] || '📍'} ${f.name} (${f.travel_minutes} min)`
    ).join('<br>');

    showInfo(`
        ✅ Found ${data.count} ${facilityType}${data.count > 1 ? 's' : ''} within ${state.searchDistanceKm} km
        <br><strong>Closest:</strong> ${icon} ${closest.name}
        <br><strong>Travel time:</strong> ${closest.travel_minutes} minutes
        ${closest.crow_distance_km ? `<br><small>Straight-line: ${closest.crow_distance_km.toFixed(1)} km</small>` : ''}
        <br><br><small style="font-size:0.85rem;">${list}</small>
    `);

    const bounds = new maplibregl.LngLatBounds();
    bounds.extend([lngLat.lng, lngLat.lat]);
    data.facilities.forEach(f => bounds.extend([parseFloat(f.facility_lon), parseFloat(f.facility_lat)]));
    state.map.fitBounds(bounds, { padding: { top: 80, bottom: 80, left: 80, right: 80 }, maxZoom: 14, duration: 1000 });
}

// ── Facility helper: remove markers only ─────────────────────────────────────
function removeFacilityMarkers() {
    state.facilityMarkers.forEach(m => {
        if (m._popup?.isOpen()) m._popup.remove();
        m.remove();
    });
    state.facilityMarkers = [];
}

// ── Facility helper: remove route layers only ─────────────────────────────────
function removeFacilityLayers() {
    ['facility-routes'].forEach(id => {
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    });
}

// Rebuild route lines after a style switch (markers survive style changes)
function rebuildFacilityRouteLayers(data, facilityType) {
    const allRouteFeatures = [];
    data.facilities.forEach((facility, index) => {
        if (facility.route?.features) {
            facility.route.features.forEach(f => {
                allRouteFeatures.push({ ...f, properties: { ...f.properties, facility_rank: index + 1 } });
            });
        }
    });
    if (allRouteFeatures.length === 0) return;

    const facilityColor = FACILITY_COLORS[facilityType] || '#6366f1';
    state.map.addSource('facility-routes', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: allRouteFeatures },
    });
    state.map.addLayer({
        id: 'facility-routes', type: 'line', source: 'facility-routes',
        paint: {
            'line-color':   facilityColor,
            'line-width':   ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 6, 5, 3],
            'line-opacity': 0.8,
        },
    });
}


// ============================================================================
// §12  SERVICE AREA MODE  (pgRouting)
// ============================================================================
// Click → truck pin → API returns reachable polygon + road network.
//
// BACKEND: GET /api/service_area?lon=…&lat=…&minutes=N
// Response: { service_area: GeoJSON Polygon, reachable_network: GeoJSON MultiLineString }
//
// SWAP TO VALHALLA ISOCHRONE:
//   1. POST /api/isochrone { locations:[{lat,lon}], costing:"auto", contours:[{time:N}], polygons:true }
//   2. Response: GeoJSON FeatureCollection of polygons (one per contour)
//   3. Render: fill + line layers on each feature — same pattern as below.
// ─────────────────────────────────────────────────────────────────────────────

function handleServiceClick(lngLat) {
    clearServiceArea();
    if (state.markers.service) { state.markers.service.remove(); state.markers.service = null; }

    state.markers.service = new maplibregl.Marker({ element: createMarker('🚚', '#1c2ae1') })
        .setLngLat(lngLat).addTo(state.map);

    calculateServiceArea(lngLat);
}

async function calculateServiceArea(lngLat) {
    showInfo(`⏳ Calculating ${state.serviceMinutes}-min service area...`);

    try {
        const data = await fetchWithTimeout(
            `${API_ENDPOINTS.serviceArea}?lon=${lngLat.lng}&lat=${lngLat.lat}&minutes=${state.serviceMinutes}`
        );

        if (data.error) throw new Error(data.error);

        state.lastServiceData = data; // Save for §14 restore
        drawServiceArea(data);

        if (data.service_area.coordinates?.[0]) {
            const bounds = new maplibregl.LngLatBounds();
            data.service_area.coordinates[0].forEach(c => bounds.extend(c));
            state.map.fitBounds(bounds, { padding: 100, maxZoom: 14, duration: 1500 });
        }

        showInfo(`🚚 ${state.serviceMinutes}-min service area calculated<br><small>Red zone = reachable area from service point</small>`);

    } catch (error) {
        handleError('Service area calculation', error);
    }
}

// Separated so it can be called both initially and from §14 restore
function drawServiceArea(data) {
    clearServiceArea();

    // Reachable road network (amber lines)
    if (data.reachable_network) {
        state.map.addSource('service-network', { type: 'geojson', data: data.reachable_network });
        state.map.addLayer({
            id: 'service-network', type: 'line', source: 'service-network',
            paint: { 'line-color': '#f59e0b', 'line-width': 3, 'line-opacity': 0.6 },
        });
    }

    // Catchment polygon (red fill + dashed border)
    if (data.service_area) {
        state.map.addSource('service-hull', { type: 'geojson', data: data.service_area });
        state.map.addLayer({
            id: 'service-hull', type: 'fill', source: 'service-hull',
            paint: { 'fill-color': '#dc2626', 'fill-opacity': 0.15 },
        });
        state.map.addLayer({
            id: 'service-border', type: 'line', source: 'service-hull',
            paint: { 'line-color': '#dc2626', 'line-width': 4, 'line-dasharray': [3, 2], 'line-opacity': 0.8 },
        });
    }
}


// ============================================================================
// §13  UTILITY FUNCTIONS
// ============================================================================
// Shared helpers used across multiple sections.
// ─────────────────────────────────────────────────────────────────────────────

// ── Fetch with timeout ────────────────────────────────────────────────────────
// All API calls go through this wrapper so they are automatically cancelled
// after REQUEST_TIMEOUT ms. FastAPI error details are surfaced in the message.
async function fetchWithTimeout(url, options = {}) {
    const controller = new AbortController();
    const timeoutId  = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);

    try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        clearTimeout(timeoutId);

        if (!response.ok) {
            let detail = `${response.status} ${response.statusText}`;
            try { const body = await response.json(); if (body.detail) detail = body.detail; } catch {}
            throw new Error(detail);
        }

        return await response.json();

    } catch (error) {
        clearTimeout(timeoutId);
        if (error.name === 'AbortError') throw new Error('Request timed out. Please try again.');
        throw error;
    }
}

// ── Error handler ─────────────────────────────────────────────────────────────
function handleError(context, error) {
    console.error(`${context} error:`, error);
    const message = error.message.includes('Failed to fetch')
        ? 'Network error. Check your connection and try again.'
        : error.message;
    showInfo(`❌ ${message}`);
}

// ── Circular marker factory ───────────────────────────────────────────────────
// Returns an HTMLElement for use with new maplibregl.Marker({ element }).
// text = label inside the circle (letter, number, or emoji)
// bgColor = CSS color string
function createMarker(text, bgColor = null) {
    const el = document.createElement('div');
    el.className = 'marker';
    if (text === '🚚') el.classList.add('marker-service');
    if (text === 'E')  el.classList.add('marker-end');
    if (bgColor) el.style.backgroundColor = bgColor;
    el.textContent = text;
    return el;
}

// ── Info box ──────────────────────────────────────────────────────────────────
// Writes HTML into the #info-box inside the search panel.
// Used by all mode-specific handlers to report status / results.
function showInfo(text) {
    const infoBox   = document.getElementById('info-box');
    const routeInfo = document.getElementById('route-info');
    if (infoBox && routeInfo) {
        infoBox.classList.remove('hidden');
        routeInfo.innerHTML = text;
    }
}

// ── Add GeoJSON route layer ───────────────────────────────────────────────────
// Removes any existing layer/source with the same ID first (prevents conflicts).
// data    = GeoJSON FeatureCollection
// layerId = unique string (e.g. 'route-0', 'tsp-segment-3')
function addRouteLayer(data, color, opacity = 0.9, width = 7, layerId = 'route') {
    if (state.map.getLayer(layerId))  state.map.removeLayer(layerId);
    if (state.map.getSource(layerId)) state.map.removeSource(layerId);
    state.map.addSource(layerId, { type: 'geojson', data });
    state.map.addLayer({
        id: layerId, type: 'line', source: layerId,
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint:  { 'line-color': color, 'line-width': width, 'line-opacity': opacity },
    });
}

// ── Fit bounds to a GeoJSON FeatureCollection ─────────────────────────────────
function fitToFeatures(data) {
    const bounds = new maplibregl.LngLatBounds();
    data.features.forEach(f => {
        if (f.geometry?.coordinates) f.geometry.coordinates.forEach(c => bounds.extend(c));
    });
    state.map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 1500 });
}


// ============================================================================
// §14  RESTORE FUNCTIONS  (after map style switch)
// ============================================================================
// MapLibre wipes all custom sources/layers when setStyle() is called.
// These functions redraw each mode's data from the cached API responses
// (state.lastRouteData, etc.) without making new network requests.
// ─────────────────────────────────────────────────────────────────────────────

function restoreRouteLayers() {
    if (!Array.isArray(state.lastRouteData)) return;
    state.lastRouteData.forEach((route, i) => {
        addRouteLayer(route, ROUTE_COLORS[i] || '#5e8bbe', i === 0 ? 0.9 : 0.6, i === 0 ? 7 : 5, `route-${i}`);
    });
}

function restoreTSPRoute() {
    if (!state.lastTSPRouteData) return;
    const { segments, waypoint_order } = state.lastTSPRouteData;

    // Clean up any stale layers first
    for (let i = 0; i < 50; i++) {
        const id = `tsp-segment-${i}`;
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }

    // Guard: MapLibre may fire 'idle' before isStyleLoaded() is true
    if (!state.map.isStyleLoaded()) {
        state.map.once('styledata', () => restoreTSPRoute());
        return;
    }

    drawTSPSegments(segments, waypoint_order);
    // TSP markers are DOM elements and survive style switches automatically
}

function restoreFacilityData() {
    if (!state.lastFacilityData || !state.markers.facility) return;
    const facilityType = document.getElementById('facility-type-select')?.value || 'hospital';
    removeFacilityLayers();
    rebuildFacilityRouteLayers(state.lastFacilityData, facilityType);
    // Facility markers survive style switches automatically
}

function restoreServiceArea() {
    if (!state.lastServiceData || !state.markers.service) return;
    drawServiceArea(state.lastServiceData);
}


// ============================================================================
// §15  CLEANUP FUNCTIONS
// ============================================================================
// Remove specific sets of layers/markers. clearAll() resets everything.
// ─────────────────────────────────────────────────────────────────────────────

function clearRouteLayers() {
    for (let i = 0; i < 20; i++) {         // Support up to 20 alternatives
        const id = `route-${i}`;
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }
    if (state.map.getLayer('route'))  state.map.removeLayer('route');
    if (state.map.getSource('route')) state.map.removeSource('route');
    state.lastRouteData = null;
}

function clearTSPLayers() {
    for (let i = 0; i < 50; i++) {         // Support up to 50 segments
        const id = `tsp-segment-${i}`;
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }
    state.lastTSPRouteData = null;
}

function clearFacilityData() {
    if (state.markers.facility) { state.markers.facility.remove(); state.markers.facility = null; }
    state.facilityMarkers.forEach(m => m.remove());
    state.facilityMarkers = [];
    removeFacilityLayers();
    state.lastFacilityData = null;
}

function clearServiceArea() {
    if (!state.map) return;
    // MapLibre requires layers to be removed BEFORE their sources
    ['service-border', 'service-hull', 'service-network'].forEach(id => {
        if (state.map.getLayer(id)) state.map.removeLayer(id);
    });
    ['service-hull', 'service-network'].forEach(id => {
        if (state.map.getSource(id)) state.map.removeSource(id);
    });
}

// 3D search highlight cleanup (§16)
function clearHighlight() {
    currentHighlightIds.forEach(id => {
        if (state.map?.getLayer(id))  state.map.removeLayer(id);
        if (state.map?.getSource(id)) state.map.removeSource(id);
    });
    currentHighlightIds = [];
    if (currentPopup) { currentPopup.remove(); currentPopup = null; }
    document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
}

// Master clear — removes everything and resets state
function clearAll() {
    clearRouteLayers();
    clearTSPLayers();
    clearFacilityData();
    clearServiceArea();
    clearHighlight();
    clearFeatureHighlight();

    Object.keys(state.markers).forEach(key => {
        if (key === 'tsp') {
            state.markers.tsp.forEach(item => item.marker?.remove());
            state.markers.tsp = [];
        } else if (state.markers[key]) {
            state.markers[key].remove();
            state.markers[key] = null;
        }
    });

    state.lastRouteData    = null;
    state.lastTSPRouteData = null;
    state.lastFacilityData = null;
    state.lastServiceData  = null;

    showInfo('Click to start');
}


// ============================================================================
// §16  ELASTICSEARCH SEARCH  (building / floor lookup)
// ============================================================================
// Searches Elasticsearch for matching SPL unit IDs, names, or addresses.
// Clicking a result switches to BDF style and zooms with a 3D extrusion highlight.
//
// REMOVE: delete this section, §16-related HTML in index.html (the search panel),
//         and ES_URL in §1. No other section depends on this.
//
// SWAP INDEX: Change the index name inside searchUnits() and the field list.
// SWAP URL:   Change ES_URL in §1 (e.g. '/es' for an Nginx proxy).
// ─────────────────────────────────────────────────────────────────────────────

async function searchUnits() {
    const queryText  = document.getElementById('searchInput').value.trim();
    const resultsDiv = document.getElementById('results');

    if (!queryText) {
        resultsDiv.innerHTML = '<div class="no-results">Please enter a search term</div>';
        return;
    }

    resultsDiv.innerHTML = '<div class="loading">Searching...</div>';
    clearHighlight();

    // Index and field selection based on dataset radio
    const index  = currentDataset === 'spl_units' ? 'building_units' : 'buildings_vertical';
    const fields = currentDataset === 'spl_units'
        ? ['properties.UNIT_ID', 'properties.NAME^2', 'properties.NAME_LONG', 'properties.UnitAddres', 'properties.LabelNames']
        : ['properties.UnitVerticalAddress^3', 'properties.fkShortAddress^1.8'];

    const esQuery = {
        query: { multi_match: { query: queryText, fields, type: 'best_fields', fuzziness: 'AUTO' } },
        size: 20,
    };

    try {
        const response = await fetch(`${ES_URL}/${index}/_search`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(esQuery),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        resultsDiv.innerHTML = '';

        if (data.hits.hits.length === 0) {
            resultsDiv.innerHTML = '<div class="no-results">No results found.</div>';
            return;
        }

        data.hits.hits.forEach(hit => {
            const feature = hit._source;
            const props   = feature.properties || {};
            const item    = document.createElement('div');
            item.className = 'result-item';

            if (currentDataset === 'spl_units') {
                item.innerHTML = `
                    <strong>${props.UNIT_ID || 'N/A'}</strong><br>
                    ${props.LabelNames || 'Unnamed'} (${props.UnitAddres || 'No address'})<br>
                    <small>Floor Height: ${props.Base !== undefined ? props.Base.toFixed(2) + 'm' : 'N/A'} | Type: ${props.USE_TYPE || 'N/A'}</small>`;
            } else {
                item.innerHTML = `
                    <strong>${props.UnitVerticalAddress || props.fkFloorGUID || '—'}</strong><br>
                    Floor ${props.FloorID ?? '—'} – ${props.UseType || '—'}<br>
                    <small>Address: ${props.UnitVerticalAddress || props.fkShortAddress || 'No address'} | Building: ${props.BuildingHeight ? props.BuildingHeight.toFixed(1) + 'm' : '—'}</small>`;
            }

            item.onclick = () => zoomToFeature(feature, item);
            resultsDiv.appendChild(item);
        });

    } catch (err) {
        console.error('Search error:', err);
        resultsDiv.innerHTML = `<div class="error">Error: ${err.message}<br><small>Check console for details</small></div>`;
    }
}

// Position popup slightly to the right of and above the feature's center
function getPopupAnchorPosition(bounds) {
    const ne = bounds.getNorthEast(), sw = bounds.getSouthWest();
    return [
        ne.lng + (ne.lng - sw.lng) * 0.3,
        sw.lat + (ne.lat - sw.lat) * 0.65,
    ];
}

// Zoom to a search result: switch to BDF, add orange 3D extrusion, show popup
async function zoomToFeature(feature, clickedElement) {
    if (!state.map) { console.warn('Map not ready'); return; }

    clearHighlight();
    clickedElement.classList.add('active');

    if (!feature.geometry) { alert('No geometry available for this feature.'); return; }

    const bounds = new maplibregl.LngLatBounds();
    const props  = feature.properties || {};
    const flattenCoords = (arr) => {
        if (typeof arr[0] === 'number') bounds.extend([arr[0], arr[1]]);
        else arr.forEach(flattenCoords);
    };
    flattenCoords(feature.geometry.coordinates);
    if (bounds.isEmpty()) { alert('No valid geometry found.'); return; }

    const popupPosition = getPopupAnchorPosition(bounds);

    let popupHTML = `
        <div style="max-width:280px;font-size:14px;line-height:1.6;">
            <strong style="font-size:16px;color:#1f2937;">
                ${props.NAME || props.FloorUsage || props.UnitAddress || 'Feature'}
            </strong><br>`;

    if (currentDataset === 'spl_units') {
        popupHTML += `
            <strong>Unit ID:</strong> ${props.UNIT_ID || '—'}<br>
            <strong>Address:</strong> ${props.UnitVerticalAddress || 'N/A'}<br>
            <strong>Floor:</strong> ${props.Base !== undefined ? props.Base.toFixed(2) + 'm' : 'N/A'}<br>
            <strong>Height:</strong> ${props.HEIGHT !== undefined ? props.HEIGHT.toFixed(2) + 'm' : 'N/A'}<br>
            <strong>Type:</strong> ${props.USE_TYPE || 'N/A'}`;
    } else {
        popupHTML += `
            <strong>ID:</strong> ${props.fkFloorGUID || props.UnitVerticalAddress || '—'}<br>
            <strong>Address:</strong> ${props.UnitVerticalAddress || props.fkShortAddress || 'N/A'}<br>
            <strong>Floor:</strong> ${props.FloorID ?? '—'}<br>
            <strong>Usage:</strong> ${props.UseType || '—'}<br>
            <strong>Type:</strong> ${props.Occupant || '—'}<br>
            <strong>Total Floors:</strong> ${props.NoofFloors || '—'}<br>
            <strong>Building Height:</strong> ${props.BuildingHeight ? props.BuildingHeight.toFixed(1) + 'm' : '—'}`;
    }
    popupHTML += '</div>';

    const afterStyleLoad = () => addHighlightAndAnimate(feature, bounds, popupPosition, popupHTML);

    const bdfStyle = STYLES.find(s => s.id === 'bdf-style');
    if (currentStyleId !== 'bdf-style') {
        state.map.setStyle(bdfStyle.url);
        currentStyleId = 'bdf-style';
        state.map.once('idle', afterStyleLoad);
    } else {
        afterStyleLoad();
    }
}

// Add 3D extrusion highlight and animate camera to the feature
function addHighlightAndAnimate(feature, bounds, popupPosition, popupHTML) {
    const layers        = state.map.getStyle().layers || [];
    const firstSymbolLayer = layers.find(l => l.type === 'symbol')?.id;
    const beforeId      = firstSymbolLayer ?? undefined;
    const props         = feature.properties || {};

    const safeId = (props.UNIT_ID || props.fkFloorGUID || props.UnitAddress || 'feat')
        .replace(/[^a-z0-9]/gi, '-');
    const id = `highlight-${safeId}`;

    currentHighlightIds.push(id);

    if (state.map.getSource(id)) state.map.removeSource(id);
    state.map.addSource(id, {
        type: 'geojson',
        data: { type: 'Feature', geometry: feature.geometry, properties: { ...feature } },
    });

    // Extrusion heights vary by dataset
    let extrusionBase, extrusionHeight;
    if (currentDataset === 'spl_units') {
        extrusionBase   = props.Base || 0;
        extrusionHeight = extrusionBase + (props.HEIGHT || 4.25);
    } else {
        const floorH    = (props.BuildingHeight || 0) / (props.FloorsAboveGround || 1);
        extrusionBase   = floorH * (props.FloorID || 0);
        extrusionHeight = extrusionBase + floorH;
    }

    state.map.addLayer({
        id, type: 'fill-extrusion', source: id,
        paint: {
            'fill-extrusion-color':   '#ff5c00',
            'fill-extrusion-opacity': 0.95,
            'fill-extrusion-height':  extrusionHeight,
            'fill-extrusion-base':    extrusionBase,
        },
    }, beforeId);

    state.map.fitBounds(bounds, {
        padding:   { top: 100, bottom: 100, left: 420, right: 100 },
        pitch:     60,
        bearing:   -18,
        minZoom:   16,
        maxZoom:   19.5,
        duration:  1600,
        essential: true,
    });

    setTimeout(() => {
        currentPopup = new maplibregl.Popup({
            offset: [15, 0], closeButton: true, className: 'unit-popup', maxWidth: '300px', anchor: 'left',
        })
            .setLngLat(popupPosition)
            .setHTML(popupHTML)
            .addTo(state.map);
        currentPopup.on('close', () => { currentPopup = null; });
    }, 800);
}


// ============================================================================
// §17  APPLICATION BOOT
// ============================================================================
// Single entry point — everything starts here when the page loads.
// ─────────────────────────────────────────────────────────────────────────────
window.addEventListener('load', initMap);