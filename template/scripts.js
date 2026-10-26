// ============================================================================
// KSA ROUTE & SERVICE ANALYSIS — scripts.js
// ============================================================================
//
// FILE STRUCTURE  (Ctrl+F on the SECTION header to jump there)
// ─────────────────────────────────────────────────────────────
//  SECTION 1  — Configuration & Constants
//  SECTION 2  — Application State
//  SECTION 3  — Map Initialization
//  SECTION 4  — Map Controls & UI Helpers  (layer switcher, panel toggle)
//  SECTION 5  — Info Pointer Feature       (click-to-inspect)
//  SECTION 6  — Draggable Panel            (info panel drag logic)
//  SECTION 7  — UI Initialization          (sliders, search panel, events)
//  SECTION 8  — Mode Switching             (route / tsp / facility / service)
//  SECTION 9  — Route Mode (A→B)           ← SWAP routing engine here
//  SECTION 10 — TSP Mode                   ← SWAP TSP solver here
//  SECTION 11 — Nearest Facility Mode      ← SWAP facility API here
//  SECTION 12 — Service Area Mode          ← SWAP isochrone API here
//  SECTION 13 — Utility Functions          (fetch, markers, showInfo)
//  SECTION 14 — Restore Functions          (rebuild layers after style switch)
//  SECTION 15 — Cleanup Functions          (clear markers and layers)
//  SECTION 16 — Search (Elasticsearch)     ← SWAP search engine here
//  SECTION 17 — App Entry Point
//
// HOW TO SWAP ROUTING ENGINE (e.g. pgRouting → Valhalla)
// ────────────────────────────────────────────────────────
//  1. Update API_ENDPOINTS in SECTION 1.
//  2. Replace calculateRoute()   in SECTION 9.
//  3. Replace calculateTSP()     in SECTION 10.
//  4. The rest of the app (markers, layers, UI) is untouched.
//
// HOW TO CHANGE LAYOUT (floating panels → dark sidebar)
// ──────────────────────────────────────────────────────
//  JS changes: NONE — all layout is CSS-only.
//  CSS changes: follow the "SIDEBAR SWAP" comments in styles.css.
//  HTML changes: move panel <div>s outside #map in index.html.
// ============================================================================


// ============================================================================
// SECTION 1 — CONFIGURATION & CONSTANTS
// ============================================================================
// All URLs, limits, and display values live here.
// Change these without touching any other section.

// ── Map styles ────────────────────────────────────────────────────────────────
// Each entry is one layer-switcher button.
// pitch   = tilt angle (0 = flat, 60 = 3D view)
// bearing = compass rotation in degrees
// SWAP: add or remove entries to change available basemap styles.
const STYLES = [
    { id: 'basic-style', name: 'Default',   url: 'styles/martin/style.json',     pitch: 0,  bearing: 0   },
    { id: 'sat-style',   name: 'Satellite', url: 'styles/martin/style_sat.json', pitch: 0,  bearing: 0   },
    { id: '3d-style',    name: '3D',        url: 'styles/martin/style_3d.json',  pitch: 45, bearing: 0   },
    { id: 'bdf-style',   name: 'BDF',       url: 'styles/martin/style_bdf.json', pitch: 60, bearing: -20 },
];

// ── API endpoints ─────────────────────────────────────────────────────────────
// BACKEND_URL is proxied by Nginx: /api/ → routing-api:5000
// SWAP routing engine: change these URLs and update SECTION 9-12 accordingly.
const BACKEND_URL = '/api';
const API_ENDPOINTS = {
    route:           `${BACKEND_URL}/route`,            // A→B route (pgRouting /route)
    tsp:             `${BACKEND_URL}/route/tsp`,         // TSP solver (pgRouting /route/tsp)
    nearestFacility: `${BACKEND_URL}/nearest_facility`,  // Facility search + routing
    serviceArea:     `${BACKEND_URL}/service_area`,      // Isochrone / service area
};

// ── Elasticsearch ─────────────────────────────────────────────────────────────
// SWAP: change to your own search backend URL.
// If using the Nginx /es/ proxy, set: const ES_URL = '/es';
const ES_URL = 'http://localhost:9200';

// ── Map defaults ──────────────────────────────────────────────────────────────
const DEFAULT_CENTER  = [46.6167, 24.8258];  // Riyadh [lng, lat]
const DEFAULT_ZOOM    = 12;
const REQUEST_TIMEOUT = 65000;               // ms — TSP and facility can be slow

// ── Facility display ──────────────────────────────────────────────────────────
// Keys must match option values in #facility-type-select (index.html)
// and _ALLOWED_FACILITY_TYPES in app.py.
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

// ── Route colors ──────────────────────────────────────────────────────────────
// Index 0 = best/primary route.  1 and 2 = alternatives.
const ROUTE_COLORS = ['#0865fc', '#4f9af7', '#6095d3'];

// ── TSP colors ────────────────────────────────────────────────────────────────
// Waypoint N and its outgoing leg share TSP_COLORS[N-1].
const TSP_COLORS = [
    '#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6',
    '#ec4899', '#14b8a6', '#f97316', '#6366f1', '#84cc16',
];

// ── Slider limits ─────────────────────────────────────────────────────────────
// Changing these here automatically updates the HTML slider max values
// (applied by initializeSliders() in SECTION 7).
const MAX_FACILITY_COUNT    = 20;
const MAX_SERVICE_MINUTES   = 20;
const MAX_SEARCH_DISTANCE_KM = 30;


// ============================================================================
// SECTION 2 — APPLICATION STATE
// ============================================================================
// Single source of truth for everything that can change at runtime.
// Do NOT read from the DOM for decisions — read from state instead.

const state = {
    // ── Map ────────────────────────────────────────────────────────────────
    map:           null,           // maplibregl.Map instance
    currentMode:   'route',        // 'route' | 'tsp' | 'facility' | 'service'
    currentCenter: DEFAULT_CENTER,
    currentZoom:   DEFAULT_ZOOM,
    currentPitch:  0,
    currentBearing: 0,

    // ── Markers ────────────────────────────────────────────────────────────
    markers: {
        start:    null,   // Green S pin (route mode)
        end:      null,   // Red E pin (route mode)
        service:  null,   // Blue truck (service area mode)
        facility: null,   // Red pin (facility mode search origin)
        tsp:      [],     // [{ marker, lngLat, color }, ...]
    },

    // ── Facility result markers ────────────────────────────────────────────
    // Stored separately from state.markers so they can be cleared independently.
    facilityMarkers: [],

    // ── Saved API responses ────────────────────────────────────────────────
    // Kept so layers can be redrawn after a map style switch without a new
    // network call.  Set to null when the data is no longer valid.
    lastRouteData:    null,   // Array of GeoJSON FeatureCollections
    lastTSPRouteData: null,   // { segments, waypoint_order }
    lastFacilityData: null,   // Full /nearest_facility response
    lastServiceData:  null,   // Full /service_area response

    // ── User settings ──────────────────────────────────────────────────────
    serviceMinutes:    5,
    facilityCount:     5,
    searchDistanceKm:  10,
    routeOptimization: 'fastest',  // 'fastest' | 'shortest'
    showAlternatives:  true,

    // ── Info pointer ───────────────────────────────────────────────────────
    infoPointerActive:      false,
    highlightedFeatureId:   null,
    highlightedSourceLayer: null,

    // ── Draggable panel ────────────────────────────────────────────────────
    isDragging:    false,
    dragOffset:    { x: 0, y: 0 },
    panelPosition: null,
};

// Module-level variables (not part of state because they don't drive rendering)
let currentDataset      = 'spl_units';   // ES index in use
let currentStyleId      = 'basic-style'; // Active basemap style ID
let currentHighlightIds = [];            // Layer IDs for 3D search highlights
let currentPopup        = null;          // Open MapLibre popup reference
let facilitySearchId    = 0;             // Incremented per search (stale-request guard)


// ============================================================================
// SECTION 3 — MAP INITIALIZATION
// ============================================================================
// Called once on page load.  Sets up the MapLibre map and wires all controls.

/**
 * Initialize MapLibre map and all UI subsystems.
 * SWAP map library: replace new maplibregl.Map({...}) with your library's
 * equivalent, then update all state.map.* calls throughout the file.
 */
function initMap() {
    state.map = new maplibregl.Map({
        container:  'map',
        style:      STYLES[0].url,
        center:     state.currentCenter,
        zoom:       state.currentZoom,
        pitch:      state.currentPitch,
        bearing:    state.currentBearing,
        maxPitch:   85,
    });

    // Keep camera state in sync so style switches can restore the view
    state.map.on('moveend', () => {
        state.currentCenter  = state.map.getCenter().toArray();
        state.currentZoom    = state.map.getZoom();
        state.currentPitch   = state.map.getPitch();
        state.currentBearing = state.map.getBearing();
    });

    // Navigation controls (zoom + compass)
    // SWAP position: 'top-right' | 'bottom-left' | 'top-left' | 'bottom-right'
    if (window.innerWidth <= 600) {
        state.map.addControl(new maplibregl.NavigationControl(), 'bottom-left');
    } else {
        state.map.addControl(new maplibregl.NavigationControl(), 'top-right');
    }

    // Custom layer-switcher buttons (Default / Satellite / 3D / BDF)
    state.map.addControl(createLayerSwitcher(), 'bottom-right');

    // Initialize slider max values
    initializeSliders();

    // Initialize the search panel (mobile collapse behavior)
    initializeSearchPanel();

    // Inject ℹ️ and ◀ buttons into the map control area
    injectMapButtons();

    // Attach all event listeners (map clicks, slider changes, button clicks)
    setupEventHandlers();

    // Responsive resize handler
    window.addEventListener('resize', () => {
        setTimeout(() => {
            const searchPanel = document.querySelector('.search-panel');
            if (searchPanel && window.innerWidth > 600) {
                searchPanel.classList.add('expanded');
            }
        }, 200);
    });

    // Show the initial instruction once the map is ready
    state.map.on('load', () => {
        showInfo('Click anywhere to begin routing of two points');
    });
}


// ============================================================================
// SECTION 4 — MAP CONTROLS & UI HELPERS
// ============================================================================
// Custom controls that sit on the map canvas (layer switcher, button injection).

/**
 * Build the layer-switcher control (Default / Satellite / 3D / BDF).
 * SWAP: remove STYLES entries in SECTION 1 to reduce buttons.
 */
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
                btn.textContent = style.name === '3D' ? '3D'
                                : style.name === 'BDF' ? 'BDF'
                                : style.name.charAt(0);
                btn.onclick = () => switchMapStyle(style);
                this.container.appendChild(btn);
            });
            return this.container;
        }
        onRemove() { this.container.parentNode?.removeChild(this.container); }
    }
    return new LayerSwitcher();
}

/**
 * Switch basemap style while preserving all custom layers and markers.
 * After the new style loads, _restoreLayersForCurrentMode() redraws everything.
 * @param {Object} style - Entry from the STYLES array
 */
async function switchMapStyle(style) {
    currentStyleId = style.id;

    const center  = state.map.getCenter();
    const zoom    = style.zoom    ?? state.map.getZoom();
    const pitch   = style.pitch   ?? state.map.getPitch();
    const bearing = style.bearing ?? state.map.getBearing();

    state.map.setStyle(style.url);

    state.map.once('idle', () => {
        state.map.jumpTo({ center, zoom, pitch, bearing });

        if (state.infoPointerActive) setupInfoPointerLayers();

        _restoreLayersForCurrentMode();
    });
}

/**
 * Redraw whichever mode's layers were active before the style switch.
 * Called from switchMapStyle() and also after search-zoom style changes.
 */
function _restoreLayersForCurrentMode() {
    switch (state.currentMode) {
        case 'route':    restoreRouteLayers();  break;
        case 'tsp':      restoreTSPRoute();     break;
        case 'facility': restoreFacilityData(); break;
        case 'service':  restoreServiceArea();  break;
    }
}

/**
 * Inject ℹ️ (info pointer) and ◀ (panel toggle) buttons into the map UI.
 * On mobile they go to the bottom-left; on desktop to the top-right.
 */
function injectMapButtons() {
    const topRight = document.querySelector('.maplibregl-ctrl-top-right');
    if (!topRight) return;

    const ctrlGroup = document.createElement('div');
    ctrlGroup.className = 'maplibregl-ctrl maplibregl-ctrl-group';
    ctrlGroup.id = 'custom-map-controls';

    // ── Info Pointer Button ───────────────────────────────────────────────
    const infoBtn = document.createElement('button');
    infoBtn.className = 'info-pointer-btn';
    infoBtn.type      = 'button';
    infoBtn.innerHTML = 'ℹ️';
    infoBtn.title     = 'Toggle Info Pointer';
    infoBtn.addEventListener('click', () => toggleInfoPointer());

    // ── Panel Toggle Button ───────────────────────────────────────────────
    const panelBtn = document.createElement('button');
    panelBtn.className = 'panel-toggle-btn';
    panelBtn.type      = 'button';
    panelBtn.innerHTML = '◀';
    panelBtn.title     = 'Hide Control Panel';
    panelBtn.addEventListener('click', () => toggleControlPanel(panelBtn));

    ctrlGroup.appendChild(infoBtn);
    ctrlGroup.appendChild(panelBtn);

    if (window.innerWidth <= 600) {
        const bottomLeft = document.querySelector('.maplibregl-ctrl-bottom-left') || _createBottomLeftContainer();
        bottomLeft.appendChild(ctrlGroup);
    } else {
        topRight.appendChild(ctrlGroup);
    }

    // Clicking the minimised panel itself re-expands it
    const panel = document.querySelector('.control-panel.header');
    if (panel) {
        panel.addEventListener('click', function expandIfMinimised(e) {
            if (e.target.closest('button,input,select,label,.mode-btn,.route-opt-btn,.range-slider')) return;
            if (!panel.classList.contains('panel-minimised')) return;
            toggleControlPanel(panelBtn);
        });
    }
}

/**
 * Show or hide the floating control panel.
 * @param {HTMLElement} btn - The ◀ / ▶ toggle button element
 */
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

/** Helper: create the bottom-left map control container if it doesn't exist. */
function _createBottomLeftContainer() {
    let el = document.querySelector('.maplibregl-ctrl-bottom-left');
    if (!el) {
        el = document.createElement('div');
        el.className = 'maplibregl-ctrl-bottom-left';
        document.getElementById('map').appendChild(el);
    }
    return el;
}


// ============================================================================
// SECTION 5 — INFO POINTER FEATURE
// ============================================================================
// When active, any click on the map shows the raw GeoJSON properties of
// whatever feature is under the cursor in the floating info panel.
//
// TO REMOVE THIS FEATURE:
//   1. Delete SECTION 5 and SECTION 6 entirely.
//   2. Remove the infoBtn injection from injectMapButtons() above.
//   3. Remove the #feature-info-panel block from index.html.

/**
 * Toggle info pointer mode on/off.
 * ON  → cursor becomes a crosshair; map clicks show feature properties.
 * OFF → cursor and map click behavior return to normal.
 */
function toggleInfoPointer() {
    state.infoPointerActive = !state.infoPointerActive;

    const btn            = document.querySelector('.info-pointer-btn');
    const mapContainer   = document.getElementById('map');
    const featurePanel   = document.getElementById('feature-info-panel');
    const panelToggleBtn = document.querySelector('.panel-toggle-btn');

    if (state.infoPointerActive) {
        setupInfoPointerLayers();
        btn.classList.add('active');
        mapContainer.classList.add('info-pointer-active');
        featurePanel.classList.remove('hidden');
        resetPanelPosition();
        setupDraggablePanel();
        clearFeatureHighlight();
        updateFeatureInfo({ html: '<p class="info-hint">Click on any feature to see its details</p>' });

        // Auto-minimise the control panel to give more screen space
        const controlPanel = document.querySelector('.control-panel.header');
        if (controlPanel && !controlPanel.classList.contains('panel-minimised')) {
            toggleControlPanel(panelToggleBtn);
        }

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

/**
 * Create the three invisible highlight layers used to mark the clicked feature
 * (one each for polygon fill, line, and point geometries).
 */
function setupInfoPointerLayers() {
    if (!state.map.getSource('feature-highlight')) {
        state.map.addSource('feature-highlight', {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: [] },
        });
    }

    const layers = [
        {
            id: 'feature-highlight-fill',
            type: 'fill',
            filter: ['==', ['geometry-type'], 'Polygon'],
            paint: { 'fill-color': '#3b82f6', 'fill-opacity': 0.3 },
        },
        {
            id: 'feature-highlight-line',
            type: 'line',
            filter: ['any', ['==', ['geometry-type'], 'LineString'], ['==', ['geometry-type'], 'Polygon']],
            paint: { 'line-color': '#3b82f6', 'line-width': 3, 'line-opacity': 0.8 },
        },
        {
            id: 'feature-highlight-point',
            type: 'circle',
            filter: ['==', ['geometry-type'], 'Point'],
            paint: { 'circle-radius': 8, 'circle-color': '#3b82f6', 'circle-opacity': 0.6, 'circle-stroke-width': 2, 'circle-stroke-color': '#1e40af' },
        },
    ];

    layers.forEach(layer => {
        if (!state.map.getLayer(layer.id)) {
            state.map.addLayer({ ...layer, source: 'feature-highlight' });
        }
    });
}

/**
 * Handle a map click while info pointer is active.
 * Queries rendered features at the click point, filters out our own overlay
 * layers, and displays the topmost base-map feature's properties.
 * @param {Object} e - MapLibre click event
 */
function handleInfoPointerClick(e) {
    if (!state.infoPointerActive || state.isDragging) return;

    const features = state.map.queryRenderedFeatures(e.point);
    if (!features || features.length === 0) {
        clearFeatureHighlight();
        updateFeatureInfo({ html: '<p class="info-hint">No features found at this location</p>' });
        return;
    }

    // Filter out our own route/facility/highlight layers
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

    const feature = validFeatures[0];
    updateFeatureHighlight(feature);
    displayFeatureInfo(feature);
}

/**
 * Push a single feature into the highlight source so MapLibre draws it.
 * @param {Object} feature - GeoJSON feature from queryRenderedFeatures()
 */
function updateFeatureHighlight(feature) {
    clearFeatureHighlight();
    state.highlightedFeatureId   = feature.id;
    state.highlightedSourceLayer = feature.sourceLayer;
    const src = state.map.getSource('feature-highlight');
    if (src) src.setData({ type: 'FeatureCollection', features: [feature] });
}

/**
 * Build and inject the feature-properties HTML into the info panel.
 * @param {Object} feature - GeoJSON feature
 */
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
            const formatted = typeof val === 'object' ? JSON.stringify(val)
                            : typeof val === 'number'  ? val.toLocaleString()
                            : val;
            html += `<div class="feature-property">
                        <span class="property-key">${_formatPropertyKey(key)}</span>
                        <span class="property-value">${formatted}</span>
                     </div>`;
        });
        html += '</div>';
    } else {
        html += '<p class="info-hint">No properties available for this feature</p>';
    }

    updateFeatureInfo({ html });
}

/**
 * Convert a raw GeoJSON key ("building_height", "UNIT_ID") to a readable label.
 * ALL_CAPS keys are left mostly intact; camelCase and snake_case are split.
 * @param {string} key
 * @returns {string}
 */
function _formatPropertyKey(key) {
    if (key === key.toUpperCase()) return key.replace(/_/g, ' ').trim();
    return key
        .replace(/_/g, ' ')
        .replace(/([A-Z])/g, ' $1')
        .replace(/^./, s => s.toUpperCase())
        .trim();
}

/** Write arbitrary HTML into the feature info panel content area. */
function updateFeatureInfo({ html }) {
    const content = document.getElementById('feature-info-content');
    if (content) content.innerHTML = html;
}

/** Clear the highlight source (removes the blue overlay from the map). */
function clearFeatureHighlight() {
    state.highlightedFeatureId   = null;
    state.highlightedSourceLayer = null;
    const src = state.map.getSource('feature-highlight');
    if (src) src.setData({ type: 'FeatureCollection', features: [] });
}

/** Add CSS classes for cursor changes when the user pans while in info-pointer mode. */
function onMapMouseDown() {
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
// SECTION 6 — DRAGGABLE PANEL
// ============================================================================
// Lets the user move the feature info panel anywhere on screen.
// Stores listener references on the panel DOM node to prevent stacking.

/**
 * Attach drag handlers to the info panel's header bar.
 * Supports both mouse and touch events.
 */
function setupDraggablePanel() {
    const panel  = document.getElementById('feature-info-panel');
    const header = document.querySelector('.feature-info-header');
    if (!panel || !header) return;

    // Clean up stacked listeners from previous activations
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
        initialX = touch.clientX - rect.left;
        initialY = touch.clientY - rect.top;
        isDragging = true;
        state.isDragging = true;
        panel.style.right = 'auto';
        panel.style.left  = rect.left + 'px';
        panel.style.top   = rect.top + 'px';
        panel.style.transform = 'none';
    };

    const drag = (e) => {
        if (!isDragging) return;
        e.preventDefault();
        const touch = e.touches?.[0] || e;
        const newX = Math.max(0, Math.min(touch.clientX - initialX, window.innerWidth  - panel.offsetWidth));
        const newY = Math.max(0, Math.min(touch.clientY - initialY, window.innerHeight - panel.offsetHeight));
        panel.style.left = newX + 'px';
        panel.style.top  = newY + 'px';
    };

    const dragEnd = () => { isDragging = false; state.isDragging = false; };

    header.addEventListener('mousedown',  dragStart);
    document.addEventListener('mousemove', drag);
    document.addEventListener('mouseup',   dragEnd);
    header.addEventListener('touchstart', dragStart, { passive: false });
    document.addEventListener('touchmove', drag,     { passive: false });
    document.addEventListener('touchend',  dragEnd);

    // Store for cleanup
    panel._dragStart = dragStart;
    panel._drag      = drag;
    panel._dragEnd   = dragEnd;
}

/** Reset the info panel to its default top-right position. */
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
// SECTION 7 — UI INITIALIZATION & EVENT HANDLERS
// ============================================================================
// Sets slider maxes, wires up the search panel for mobile, and attaches all
// button/input listeners.

/**
 * Apply SECTION 1 max constants to the HTML slider elements.
 * Doing this in JS (rather than hardcoding in HTML) means changing a constant
 * is the only step needed to update the UI limit.
 */
function initializeSliders() {
    const fc = document.getElementById('facility-count-input');
    if (fc) fc.max = MAX_FACILITY_COUNT;
    const di = document.getElementById('distance-input');
    if (di) di.max = MAX_SEARCH_DISTANCE_KM;
    const ti = document.getElementById('time-input');
    if (ti) ti.max = MAX_SERVICE_MINUTES;
}

/**
 * Configure the search panel collapse/expand behavior.
 * Desktop: always expanded.
 * Mobile: starts collapsed; tapping the pill expands it.
 */
function initializeSearchPanel() {
    const searchPanel = document.querySelector('.search-panel');
    if (!searchPanel) return;

    const isPhone = window.innerWidth <= 600;
    searchPanel.classList.toggle('expanded', !isPhone);

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

/**
 * Attach all interactive event listeners.
 * Called once by initMap().
 */
function setupEventHandlers() {
    // ── Map click gate ────────────────────────────────────────────────────
    // All map clicks funnel through here.  Info pointer takes priority.
    state.map.on('click', (e) => {
        if (state.infoPointerActive) handleInfoPointerClick(e);
        else                          handleMapClick(e);
    });

    // ── Feature info panel close button ───────────────────────────────────
    document.getElementById('close-feature-info')?.addEventListener('click', () => {
        state.infoPointerActive = true;  // ensure toggleInfoPointer() turns it OFF
        toggleInfoPointer();
    });

    // ── Mode buttons ──────────────────────────────────────────────────────
    ['route', 'tsp', 'facility', 'service'].forEach(mode => {
        document.getElementById(`mode-${mode}`)?.addEventListener('click', () => switchMode(mode));
    });

    // ── Route optimization buttons ─────────────────────────────────────────
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

    // ── Show alternatives checkbox ─────────────────────────────────────────
    document.getElementById('show-alternatives')?.addEventListener('change', (e) => {
        state.showAlternatives = e.target.checked;
        if (state.markers.start && state.markers.end)
            calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());
    });

    // ── Service time slider ────────────────────────────────────────────────
    const timeInput = document.getElementById('time-input');
    const timeValue = document.getElementById('time-value');
    if (timeInput && timeValue) {
        timeValue.textContent = timeInput.value;
        timeInput.addEventListener('input', (e) => {
            state.serviceMinutes  = parseInt(e.target.value, 10);
            timeValue.textContent = state.serviceMinutes;
            if (state.markers.service)
                calculateServiceArea(state.markers.service.getLngLat());
        });
    }

    // ── Facility count slider ──────────────────────────────────────────────
    const facilityCountInput = document.getElementById('facility-count-input');
    const facilityCountValue = document.getElementById('facility-count-value');
    if (facilityCountInput && facilityCountValue) {
        facilityCountValue.textContent = facilityCountInput.value;
        facilityCountInput.addEventListener('input', (e) => {
            state.facilityCount            = parseInt(e.target.value, 10);
            facilityCountValue.textContent = state.facilityCount;
            if (state.markers.facility)
                calculateNearestFacilities(state.markers.facility.getLngLat());
        });
    }

    // ── Search radius slider ───────────────────────────────────────────────
    const distanceInput = document.getElementById('distance-input');
    const distanceValue = document.getElementById('distance-value');
    if (distanceInput && distanceValue) {
        distanceValue.textContent = distanceInput.value;
        distanceInput.addEventListener('input', (e) => {
            state.searchDistanceKm    = parseInt(e.target.value, 10);
            distanceValue.textContent = state.searchDistanceKm;
            if (state.markers.facility)
                calculateNearestFacilities(state.markers.facility.getLngLat());
        });
    }

    // ── Search panel Enter key ─────────────────────────────────────────────
    document.getElementById('searchInput')?.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') searchUnits();
    });

    // ── Dataset radio buttons ──────────────────────────────────────────────
    document.querySelectorAll('input[name="dataset"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            currentDataset = e.target.value;
            document.getElementById('results').innerHTML = '';
            clearHighlight();
        });
    });
}

/** Highlight the active route optimization button (Fastest or Shortest). */
function updateRouteOptButtons() {
    document.querySelectorAll('.route-opt-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById(state.routeOptimization === 'fastest' ? 'opt-fastest' : 'opt-shortest')
            ?.classList.add('active');
}

/**
 * Dispatch a map click to the correct mode handler.
 * @param {Object} e - MapLibre click event
 */
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
// SECTION 8 — MODE SWITCHING
// ============================================================================

/**
 * Switch active tool mode.
 * Clears all layers/markers, updates the UI, disables info pointer if active.
 * @param {string} mode - 'route' | 'tsp' | 'facility' | 'service'
 */
function switchMode(mode) {
    clearAll();
    state.currentMode = mode;
    _updateModeButtons(mode);
    _updateModeInstructions(mode);
    if (state.infoPointerActive) toggleInfoPointer();
}

function _updateModeButtons(activeMode) {
    document.querySelectorAll('.mode-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById(`mode-${activeMode}`)?.classList.add('active');
}

/**
 * Show/hide the mode-specific controls and update the instruction text.
 * @param {string} mode
 */
function _updateModeInstructions(mode) {
    document.getElementById('route-options')?.classList.toggle('hidden',              mode !== 'route');
    document.getElementById('facility-selector')?.classList.toggle('hidden',          mode !== 'facility');
    document.getElementById('facility-sliders-container')?.classList.toggle('hidden', mode !== 'facility');
    document.getElementById('time-slider')?.classList.toggle('hidden',               mode !== 'service');

    const instructions = {
        route:    { html: 'Click: <span class="highlight start">Start</span> → <span class="highlight end">End</span>', info: '🗺️ A→B Route mode active' },
        tsp:      { html: 'Click to add <span style="color:#8b5cf6;font-weight:bold">waypoints</span> (min 3)',        info: '🔄 TSP mode: Add at least 3 points' },
        facility: { html: 'Click a <span style="color:#ef4444;font-weight:bold">location</span> to find nearest facilities', info: '🏥 Nearest Facility mode active' },
        service:  { html: 'Click <span style="color:#ef4444;font-weight:bold">service location</span> + adjust time', info: '🚚 Service Area mode active' },
    };

    const cfg = instructions[mode];
    if (cfg) {
        const instruction = document.getElementById('mode-instruction');
        if (instruction) instruction.innerHTML = cfg.html;
        showInfo(cfg.info);
    }
}


// ============================================================================
// SECTION 9 — ROUTE MODE  (A→B)
// ============================================================================
// Click 1 = green Start pin.  Click 2 = red End pin + route calculation.
// Click 3 = resets and starts over.
//
// ┌─ SWAP ROUTING ENGINE ────────────────────────────────────────────────────┐
// │  Currently uses pgRouting via /api/route (GET with query params).        │
// │  To switch to Valhalla:                                                  │
// │    1. Update API_ENDPOINTS.route in SECTION 1.                           │
// │    2. Replace the fetch URL in calculateRoute() below.                   │
// │    3. Change response parsing: pgRouting returns { routes:[...] }        │
// │       while Valhalla returns { trip: { legs:[...] } }.                   │
// │    4. Routes from Valhalla are polyline-encoded; use decodePolyline6().  │
// └──────────────────────────────────────────────────────────────────────────┘

/**
 * Handle map clicks in route mode.
 * @param {Object} lngLat - { lng, lat }
 */
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

/**
 * Fetch and draw one or more routes between start and end.
 * SWAP: replace the URL and response parsing to change routing engine.
 * @param {Object} start - { lng, lat }
 * @param {Object} end   - { lng, lat }
 */
async function calculateRoute(start, end) {
    clearRouteLayers();
    showInfo('⏳ Calculating routes...');

    try {
        // ── pgRouting request (GET) ───────────────────────────────────────
        // SWAP: replace this URL with your engine's endpoint.
        const url = `${API_ENDPOINTS.route}?start_lon=${start.lng}&start_lat=${start.lat}` +
                    `&end_lon=${end.lng}&end_lat=${end.lat}` +
                    `&alternatives=${state.showAlternatives ? 3 : 1}` +
                    `&optimization=${state.routeOptimization}`;

        const data = await fetchWithTimeout(url);
        if (data.error) throw new Error(data.error);

        // ── pgRouting response shape ──────────────────────────────────────
        // { routes: [ { type:"FeatureCollection", features:[], total_distance_km, duration_minutes }, ... ] }
        // SWAP: parse your engine's response here.
        const routes = data.routes;
        if (!routes || routes.length === 0) throw new Error('No routes returned by the server.');

        state.lastRouteData = routes;

        routes.forEach((route, i) => {
            const color   = ROUTE_COLORS[i] || '#5e8bbe';
            const opacity = i === 0 ? 0.9 : 0.6;
            const width   = i === 0 ? 7   : 5;
            addRouteLayer(route, color, opacity, width, `route-${i}`);
        });

        routes.length > 1 ? _fitToMultipleRoutes(routes) : fitToFeatures(routes[0]);

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

function _fitToMultipleRoutes(routes) {
    const bounds = new maplibregl.LngLatBounds();
    routes.forEach(route => {
        route.features.forEach(f => {
            if (f.geometry?.coordinates) f.geometry.coordinates.forEach(c => bounds.extend(c));
        });
    });
    state.map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 1500 });
}


// ============================================================================
// SECTION 10 — TSP MODE  (Travelling Salesman Problem)
// ============================================================================
// Click to add numbered waypoints (min 3).  After the 3rd point a 2-second
// debounce fires and the optimal visit order is calculated.
// Each waypoint and its outgoing segment share the same color.
//
// ┌─ SWAP TSP SOLVER ─────────────────────────────────────────────────────────┐
// │  Currently uses pgRouting /api/route/tsp (POST { points: [[lng,lat]...]}. │
// │  To switch to VROOM: change API_ENDPOINTS.tsp, change the POST body       │
// │  format to VROOM's { vehicles, jobs } shape, and update response parsing. │
// └──────────────────────────────────────────────────────────────────────────┘

/**
 * Handle map clicks in TSP mode.
 * @param {Object} lngLat - { lng, lat }
 */
function handleTSPClick(lngLat) {
    // Reject clicks that are too close to an existing waypoint
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
        showInfo(`✅ Point ${num} added. Auto-calculating in 2 s…`);
        setTimeout(() => { if (state.markers.tsp.length >= 3) calculateTSP(); }, 2000);
    }
}

/**
 * Send waypoints to the TSP API and draw the optimised route.
 * SWAP: replace the fetch call and response parsing to change solver.
 */
async function calculateTSP() {
    if (state.markers.tsp.length < 3) { showInfo('❌ Need at least 3 points for TSP'); return; }

    showInfo(`⏳ Solving TSP for ${state.markers.tsp.length} points...`);

    try {
        const points = state.markers.tsp.map(m => [m.lngLat.lng, m.lngLat.lat]);

        // ── pgRouting TSP request (POST) ──────────────────────────────────
        // SWAP: change endpoint and body format for your solver.
        const data = await fetchWithTimeout(API_ENDPOINTS.tsp, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ points }),
        });
        if (data.error) throw new Error(data.error);

        clearTSPLayers();

        // ── pgRouting TSP response shape ──────────────────────────────────
        // { segments: [FeatureCollection,...], waypoint_order: [0,2,1,...], duration_minutes, total_distance_km }
        // SWAP: parse your solver's response here.
        state.lastTSPRouteData = { segments: data.segments, waypoint_order: data.waypoint_order };
        drawTSPSegments(data.segments, data.waypoint_order);

        if (data.segments?.length > 0) fitToFeatures(data.segments[0]);

        const orderStr = data.waypoint_order.map(idx => {
            const color = TSP_COLORS[idx % TSP_COLORS.length];
            return `<span style="color:${color};font-weight:bold;">${idx + 1}</span>`;
        }).join(' <span style="color:#6b7280;">→</span> ');

        showInfo(`✅ <strong>TSP Optimized!</strong><br>
            Order: ${orderStr}<br>
            ${data.duration_minutes} min • ${data.total_distance_km} km • ${data.segment_count} legs`);

    } catch (error) {
        handleError('TSP calculation', error);
    }
}

/**
 * Draw each TSP leg with the color of its starting waypoint.
 * @param {Array} segments       - Array of GeoJSON FeatureCollections
 * @param {Array} waypoint_order - 0-based visit order indices
 */
function drawTSPSegments(segments, waypoint_order) {
    if (!segments || segments.length === 0) return;
    segments.forEach((segmentData, i) => {
        const startWaypointIdx = waypoint_order ? waypoint_order[i] : i;
        const color            = TSP_COLORS[startWaypointIdx % TSP_COLORS.length];
        addRouteLayer(segmentData, color, 0.95, 6.5, `tsp-segment-${i}`);
    });
}


// ============================================================================
// SECTION 11 — NEAREST FACILITY MODE
// ============================================================================
// User clicks to set a search origin.  The API returns the N nearest
// facilities of the chosen type, each with a driving route.
//
// ┌─ SWAP FACILITY API ────────────────────────────────────────────────────────┐
// │  Currently uses PostGIS /api/nearest_facility (GET with query params).    │
// │  To switch to Overpass API: replace _runFacilitySearchFromAPI() with      │
// │  an Overpass fetch, then route individually via Valhalla /route.          │
// └──────────────────────────────────────────────────────────────────────────┘

/**
 * Handle a map click in facility mode: clear old results, place origin pin,
 * then trigger the search.
 * @param {Object} lngLat - { lng, lat }
 */
function handleFacilityClick(lngLat) {
    clearFacilityData();
    state.markers.facility = new maplibregl.Marker({ element: createMarker('📍', '#dc2626') })
        .setLngLat(lngLat).addTo(state.map);
    showInfo('⏳ Searching nearest facilities...');
    calculateNearestFacilities(lngLat);
}

/**
 * Query the facility API and draw results.
 * Uses facilitySearchId to discard stale responses if the user clicks again
 * before the previous request completes.
 * @param {Object} lngLat - { lng, lat }
 */
async function calculateNearestFacilities(lngLat) {
    const mySearchId   = ++facilitySearchId;
    const facilityType = document.getElementById('facility-type-select')?.value || 'hospital';

    showInfo(`⏳ Searching for nearest ${facilityType}s within ${state.searchDistanceKm} km...<br><small>This may take a few seconds</small>`);

    try {
        // ── Facility API request (GET) ────────────────────────────────────
        // SWAP: replace this URL for a different backend.
        const url = `${API_ENDPOINTS.nearestFacility}?lon=${lngLat.lng}&lat=${lngLat.lat}` +
                    `&type=${encodeURIComponent(facilityType)}` +
                    `&limit=${state.facilityCount}` +
                    `&max_distance_km=${state.searchDistanceKm}` +
                    `&routes=true`;

        const data = await fetchWithTimeout(url);
        if (mySearchId !== facilitySearchId) return;  // stale — discard
        if (data.error) throw new Error(data.error);

        if (!data.facilities || data.facilities.length === 0) {
            showInfo(`ℹ️ No ${facilityType} found within ${state.searchDistanceKm} km radius`);
            return;
        }

        displayFacilityResults(lngLat, data, facilityType);

    } catch (error) {
        if (mySearchId === facilitySearchId) handleError('Facility search', error);
    }
}

/**
 * Draw facility markers and route lines from the API response.
 * Also called by restoreFacilityData() after a style switch.
 * @param {Object} lngLat       - Search origin
 * @param {Object} data         - API response { facilities:[...], count }
 * @param {string} facilityType - e.g. 'hospital'
 */
function displayFacilityResults(lngLat, data, facilityType) {
    removeFacilityLayers();
    removeFacilityMarkers();

    // ── Collect all route features into one GeoJSON source ────────────────
    // (single source is more efficient than one source per facility)
    const allRouteFeatures = [];
    data.facilities.forEach((facility, index) => {
        if (facility.route?.features) {
            facility.route.features.forEach(f => {
                allRouteFeatures.push({
                    ...f,
                    properties: { ...f.properties, facility_name: facility.name, facility_rank: index + 1, travel_minutes: facility.travel_minutes },
                });
            });
        }
    });

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

    // ── Facility markers with hover popups ────────────────────────────────
    state.facilityMarkers = data.facilities.map((facility, index) => {
        const icon  = FACILITY_ICONS[facility.type] || '📍';
        const color = FACILITY_COLORS[facility.type] || '#6366f1';

        const el = document.createElement('div');
        el.className       = 'facility-marker';
        el.style.background = color;
        el.innerHTML       = icon;

        const badge = document.createElement('div');
        badge.className      = 'rank-badge';
        badge.style.borderColor = color;
        badge.style.color    = color;
        badge.textContent    = index + 1;
        el.appendChild(badge);

        const popup = new maplibregl.Popup({ offset: 25, closeButton: true, closeOnClick: true, maxWidth: '300px' })
            .setHTML(`<div style="font-family:Inter,sans-serif;min-width:220px;">
                <div style="font-size:24px;margin-bottom:8px;">${icon}</div>
                <strong style="font-size:14px;color:#1f2937;">${facility.name}</strong>
                ${facility.address ? `<p style="margin:4px 0;font-size:12px;color:#6b7280;">${facility.address}</p>` : ''}
                <p style="margin:8px 0 0;font-size:13px;color:#059669;"><strong>⏱️ ${facility.travel_minutes} minutes</strong> drive</p>
                <p style="margin:4px 0 0;font-size:11px;color:#9ca3af;">
                    Rank: #${index + 1}
                    ${facility.crow_distance_km ? ` • ${parseFloat(facility.crow_distance_km).toFixed(1)} km straight-line` : ''}
                </p>
            </div>`);

        const marker = new maplibregl.Marker({ element: el, anchor: 'center', offset: [0, -6] })
            .setLngLat([parseFloat(facility.facility_lon), parseFloat(facility.facility_lat)])
            .setPopup(popup).addTo(state.map);
        marker._popup = popup;

        // Hover to open / close popup
        let closeTimeout;
        el.addEventListener('mouseenter', () => { clearTimeout(closeTimeout); if (!marker.getPopup().isOpen()) marker.togglePopup(); });
        el.addEventListener('mouseleave', () => { closeTimeout = setTimeout(() => { if (marker.getPopup().isOpen()) marker.togglePopup(); }, 200); });

        return marker;
    });

    state.lastFacilityData = data;

    // ── Info box summary ──────────────────────────────────────────────────
    const closest = data.facilities[0];
    const icon    = FACILITY_ICONS[facilityType] || '📍';
    const list    = data.facilities.map((f, i) => `${i + 1}. ${FACILITY_ICONS[f.type] || '📍'} ${f.name} (${f.travel_minutes} min)`).join('<br>');
    showInfo(`✅ Found ${data.count} ${facilityType}${data.count > 1 ? 's' : ''} within ${state.searchDistanceKm} km
        <br><strong>Closest:</strong> ${icon} ${closest.name}
        <br><strong>Travel time:</strong> ${closest.travel_minutes} minutes
        ${closest.crow_distance_km ? `<br><small>Straight-line: ${closest.crow_distance_km.toFixed(1)} km</small>` : ''}
        <br><br><small style="font-size:0.85rem;">${list}</small>`);

    // ── Fit map to all results ─────────────────────────────────────────────
    const bounds = new maplibregl.LngLatBounds();
    bounds.extend([lngLat.lng, lngLat.lat]);
    data.facilities.forEach(f => bounds.extend([parseFloat(f.facility_lon), parseFloat(f.facility_lat)]));
    state.map.fitBounds(bounds, { padding: { top: 80, bottom: 80, left: 80, right: 80 }, maxZoom: 14, duration: 1000 });
}

// ── Helper: remove facility result markers ────────────────────────────────────
function removeFacilityMarkers() {
    state.facilityMarkers.forEach(m => { if (m._popup?.isOpen()) m._popup.remove(); m.remove(); });
    state.facilityMarkers = [];
}

// ── Helper: remove facility route layers ─────────────────────────────────────
function removeFacilityLayers() {
    ['facility-routes'].forEach(id => {
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    });
}

// ── Helper: rebuild route layers only (markers survive style switches) ────────
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
    state.map.addSource('facility-routes', { type: 'geojson', data: { type: 'FeatureCollection', features: allRouteFeatures } });
    state.map.addLayer({
        id: 'facility-routes', type: 'line', source: 'facility-routes',
        paint: { 'line-color': facilityColor, 'line-width': ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 6, 5, 3], 'line-opacity': 0.8 },
    });
}


// ============================================================================
// SECTION 12 — SERVICE AREA MODE
// ============================================================================
// User clicks a service center.  The API returns a polygon representing the
// area reachable within the configured number of minutes.
//
// ┌─ SWAP ISOCHRONE API ───────────────────────────────────────────────────────┐
// │  Currently uses pgRouting /api/service_area (GET).                        │
// │  To switch to Valhalla: change API_ENDPOINTS.serviceArea and update       │
// │  calculateServiceArea() to POST the Valhalla isochrone payload.           │
// │  Response shape: Valhalla returns GeoJSON directly; pgRouting returns     │
// │  { service_area: Geometry, reachable_network: FeatureCollection }.        │
// └──────────────────────────────────────────────────────────────────────────┘

/**
 * Handle a map click in service area mode.
 * @param {Object} lngLat - { lng, lat }
 */
function handleServiceClick(lngLat) {
    clearServiceArea();
    if (state.markers.service) { state.markers.service.remove(); state.markers.service = null; }
    state.markers.service = new maplibregl.Marker({ element: createMarker('🚚', '#1c2ae1') })
        .setLngLat(lngLat).addTo(state.map);
    calculateServiceArea(lngLat);
}

/**
 * Fetch and render the service area polygon.
 * SWAP: replace the URL and response parsing to change isochrone provider.
 * @param {Object} lngLat - { lng, lat }
 */
async function calculateServiceArea(lngLat) {
    showInfo(`⏳ Calculating ${state.serviceMinutes}-min service area...`);

    try {
        // ── pgRouting service area request (GET) ─────────────────────────
        // SWAP: change to your isochrone endpoint.
        const data = await fetchWithTimeout(
            `${API_ENDPOINTS.serviceArea}?lon=${lngLat.lng}&lat=${lngLat.lat}&minutes=${state.serviceMinutes}`
        );
        if (data.error) throw new Error(data.error);

        state.lastServiceData = data;
        drawServiceArea(data);

        // Fit map to polygon extent
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

/**
 * Draw (or redraw) the service area layers.
 * Separated from calculateServiceArea() so restoreServiceArea() can reuse it.
 * @param {Object} data - Saved API response
 */
function drawServiceArea(data) {
    clearServiceArea();

    // Road network (amber lines)
    if (data.reachable_network) {
        state.map.addSource('service-network', { type: 'geojson', data: data.reachable_network });
        state.map.addLayer({ id: 'service-network', type: 'line', source: 'service-network',
            paint: { 'line-color': '#f59e0b', 'line-width': 3, 'line-opacity': 0.6 } });
    }

    // Catchment polygon (red fill + dashed border)
    if (data.service_area) {
        state.map.addSource('service-hull', { type: 'geojson', data: data.service_area });
        state.map.addLayer({ id: 'service-hull',   type: 'fill', source: 'service-hull',
            paint: { 'fill-color': '#dc2626', 'fill-opacity': 0.15 } });
        state.map.addLayer({ id: 'service-border', type: 'line', source: 'service-hull',
            paint: { 'line-color': '#dc2626', 'line-width': 4, 'line-dasharray': [3, 2], 'line-opacity': 0.8 } });
    }
}


// ============================================================================
// SECTION 13 — UTILITY FUNCTIONS
// ============================================================================

/**
 * Fetch a URL with an automatic timeout.
 * Surfaces HTTP error detail from FastAPI's JSON body when available.
 * @param {string} url     - URL to fetch
 * @param {Object} options - Standard fetch() options
 * @returns {Promise<Object>} Parsed JSON response
 */
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

/**
 * Log an error and display a user-friendly message in the info box.
 * @param {string} context - Human label for the operation that failed
 * @param {Error}  error
 */
function handleError(context, error) {
    console.error(`${context} error:`, error);
    const message = error.message.includes('Failed to fetch')
        ? 'Network error. Check your connection and try again.'
        : error.message;
    showInfo(`❌ ${message}`);
}

/**
 * Create a circular DOM marker for MapLibre.
 * SWAP: replace with maplibregl.Marker({ color }) for native markers.
 * @param {string} text    - Label or emoji inside the circle
 * @param {string} bgColor - CSS color (hex)
 * @returns {HTMLElement}
 */
function createMarker(text, bgColor = null) {
    const el = document.createElement('div');
    el.className = 'marker';
    if (text === '🚚') el.classList.add('marker-service');
    if (text === 'E')  el.classList.add('marker-end');
    if (bgColor) el.style.backgroundColor = bgColor;
    el.textContent = text;
    return el;
}

/**
 * Write HTML into the shared info / status box.
 * SWAP: to use a toast notification instead, show a temporary overlay here.
 * @param {string} text - HTML string
 */
function showInfo(text) {
    const infoBox   = document.getElementById('info-box');
    const routeInfo = document.getElementById('route-info');
    if (infoBox && routeInfo) {
        infoBox.classList.remove('hidden');
        routeInfo.innerHTML = text;
    }
}

/**
 * Add or update a GeoJSON line layer on the map.
 * Removes an existing layer/source with the same ID before adding (avoids
 * "source already exists" errors when recalculating in the same session).
 * @param {Object} data    - GeoJSON FeatureCollection
 * @param {string} color   - Hex color
 * @param {number} opacity - 0–1
 * @param {number} width   - Pixels
 * @param {string} layerId - Unique layer ID
 */
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

/**
 * Zoom the map to fit a GeoJSON FeatureCollection.
 * @param {Object} data - GeoJSON FeatureCollection
 */
function fitToFeatures(data) {
    const bounds = new maplibregl.LngLatBounds();
    data.features.forEach(f => {
        if (f.geometry?.coordinates) f.geometry.coordinates.forEach(c => bounds.extend(c));
    });
    state.map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 1500 });
}


// ============================================================================
// SECTION 14 — RESTORE FUNCTIONS  (after map style switch)
// ============================================================================
// Each function redraws one mode's data from saved state without a new API call.

/** Redraw A→B routes after a style switch. */
function restoreRouteLayers() {
    if (!Array.isArray(state.lastRouteData)) return;
    state.lastRouteData.forEach((route, i) => {
        addRouteLayer(route, ROUTE_COLORS[i] || '#5e8bbe', i === 0 ? 0.9 : 0.6, i === 0 ? 7 : 5, `route-${i}`);
    });
}

/** Redraw TSP segments after a style switch. */
function restoreTSPRoute() {
    if (!state.lastTSPRouteData) return;
    const { segments, waypoint_order } = state.lastTSPRouteData;

    for (let i = 0; i < 50; i++) {
        const id = `tsp-segment-${i}`;
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }

    if (!state.map.isStyleLoaded()) { state.map.once('styledata', () => restoreTSPRoute()); return; }
    drawTSPSegments(segments, waypoint_order);
}

/**
 * Rebuild facility route layers after a style switch.
 * Markers survive style switches on their own (they're DOM nodes, not style layers).
 */
function restoreFacilityData() {
    if (!state.lastFacilityData || !state.markers.facility) return;
    const facilityType = document.getElementById('facility-type-select')?.value || 'hospital';
    removeFacilityLayers();
    rebuildFacilityRouteLayers(state.lastFacilityData, facilityType);
}

/** Redraw service area polygon after a style switch. */
function restoreServiceArea() {
    if (!state.lastServiceData || !state.markers.service) return;
    drawServiceArea(state.lastServiceData);
}


// ============================================================================
// SECTION 15 — CLEANUP FUNCTIONS
// ============================================================================

/** Remove all A→B route layers (up to 20 alternatives). */
function clearRouteLayers() {
    for (let i = 0; i < 20; i++) {
        const id = `route-${i}`;
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }
    if (state.map.getLayer('route'))  state.map.removeLayer('route');
    if (state.map.getSource('route')) state.map.removeSource('route');
    state.lastRouteData = null;
}

/** Remove all TSP segment layers (up to 50). */
function clearTSPLayers() {
    for (let i = 0; i < 50; i++) {
        const id = `tsp-segment-${i}`;
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }
    state.lastTSPRouteData = null;
}

/** Remove facility origin pin, result markers, and route lines. */
function clearFacilityData() {
    if (state.markers.facility) { state.markers.facility.remove(); state.markers.facility = null; }
    state.facilityMarkers.forEach(m => m.remove());
    state.facilityMarkers = [];
    removeFacilityLayers();
    state.lastFacilityData = null;
}

/** Remove service area layers (layers must be removed before their sources). */
function clearServiceArea() {
    if (!state.map) return;
    ['service-border', 'service-hull', 'service-network'].forEach(id => {
        if (state.map.getLayer(id)) state.map.removeLayer(id);
    });
    ['service-hull', 'service-network'].forEach(id => {
        if (state.map.getSource(id)) state.map.removeSource(id);
    });
}

/** Remove 3D search highlights and close the current popup. */
function clearHighlight() {
    currentHighlightIds.forEach(id => {
        if (state.map?.getLayer(id))  state.map.removeLayer(id);
        if (state.map?.getSource(id)) state.map.removeSource(id);
    });
    currentHighlightIds = [];
    if (currentPopup) { currentPopup.remove(); currentPopup = null; }
    document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
}

/** Master clear — removes everything and resets state. Called on mode switch. */
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
// SECTION 16 — SEARCH (Elasticsearch)
// ============================================================================
// Searches building_units or buildings_vertical indices.
// Results zoom to the feature in a 3D BDF view with an orange extrusion highlight.
//
// ┌─ SWAP SEARCH BACKEND ─────────────────────────────────────────────────────┐
// │  Currently uses Elasticsearch multi_match with fuzzy matching.            │
// │  To switch to a different backend:                                        │
// │    1. Update ES_URL in SECTION 1.                                         │
// │    2. Replace the fetch call in searchUnits().                            │
// │    3. Keep zoomToFeature() unchanged — it just needs a GeoJSON feature.  │
// └──────────────────────────────────────────────────────────────────────────┘

/**
 * Execute a fuzzy multi-field search against the active Elasticsearch index.
 * Renders matching features as clickable cards in the search panel.
 */
async function searchUnits() {
    const queryText  = document.getElementById('searchInput').value.trim();
    const resultsDiv = document.getElementById('results');

    if (!queryText) { resultsDiv.innerHTML = '<div class="no-results">Please enter a search term</div>'; return; }

    resultsDiv.innerHTML = '<div class="loading">Searching...</div>';
    clearHighlight();

    // ── Elasticsearch query ───────────────────────────────────────────────
    // SWAP: replace this block with your search backend's request format.
    const index  = currentDataset === 'spl_units' ? 'building_units' : 'buildings_vertical';
    const fields = currentDataset === 'spl_units'
        ? ['properties.UNIT_ID', 'properties.NAME^2', 'properties.NAME_LONG', 'properties.UnitAddres', 'properties.LabelNames']
        : ['properties.UnitVerticalAddress^3', 'properties.fkShortAddress^1.8'];

    const esQuery = { query: { multi_match: { query: queryText, fields, type: 'best_fields', fuzziness: 'AUTO' } }, size: 20 };

    try {
        const response = await fetch(`${ES_URL}/${index}/_search`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(esQuery),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        resultsDiv.innerHTML = '';

        if (data.hits.hits.length === 0) { resultsDiv.innerHTML = '<div class="no-results">No results found.</div>'; return; }

        data.hits.hits.forEach(hit => {
            const feature = hit._source;
            const props   = feature.properties || {};
            const item    = document.createElement('div');
            item.className = 'result-item';

            if (currentDataset === 'spl_units') {
                item.innerHTML = `<strong>${props.UNIT_ID || 'N/A'}</strong><br>
                    ${props.LabelNames || 'Unnamed'} (${props.UnitAddres || 'No address'})<br>
                    <small>Floor Height: ${props.Base !== undefined ? props.Base.toFixed(2) + 'm' : 'N/A'} | Type: ${props.USE_TYPE || 'N/A'}</small>`;
            } else {
                item.innerHTML = `<strong>${props.UnitVerticalAddress || props.fkFloorGUID || '—'}</strong><br>
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

/**
 * Compute popup anchor position relative to a feature's bounding box.
 * @param {Object} bounds - maplibregl.LngLatBounds
 * @returns {Array} [lng, lat]
 */
function _getPopupAnchorPosition(bounds) {
    const ne = bounds.getNorthEast(), sw = bounds.getSouthWest();
    return [ ne.lng + (ne.lng - sw.lng) * 0.3, sw.lat + (ne.lat - sw.lat) * 0.65 ];
}

/**
 * Zoom to a search result, switch to the BDF 3D style, and highlight the
 * feature with an orange fill-extrusion.
 * @param {Object}      feature        - Elasticsearch _source document
 * @param {HTMLElement} clickedElement - The result card that was clicked
 */
async function zoomToFeature(feature, clickedElement) {
    if (!state.map) { console.warn('Map not ready'); return; }

    clearHighlight();
    clickedElement.classList.add('active');

    if (!feature.geometry) { alert('No geometry available for this feature.'); return; }

    const bounds = new maplibregl.LngLatBounds();
    const flattenCoords = (arr) => {
        if (typeof arr[0] === 'number') bounds.extend([arr[0], arr[1]]);
        else arr.forEach(flattenCoords);
    };
    flattenCoords(feature.geometry.coordinates);
    if (bounds.isEmpty()) { alert('No valid geometry found.'); return; }

    const popupPosition = _getPopupAnchorPosition(bounds);
    const props         = feature.properties || {};

    // ── Build popup HTML ──────────────────────────────────────────────────
    let popupHTML = `<div style="max-width:280px;font-size:14px;line-height:1.6;">
        <strong style="font-size:16px;color:#1f2937;">${props.NAME || props.FloorUsage || props.UnitAddress || 'Feature'}</strong><br>`;
    if (currentDataset === 'spl_units') {
        popupHTML += `<strong>Unit ID:</strong> ${props.UNIT_ID || '—'}<br>
            <strong>Address:</strong> ${props.UnitVerticalAddress || 'N/A'}<br>
            <strong>Floor:</strong> ${props.Base !== undefined ? props.Base.toFixed(2) + 'm' : 'N/A'}<br>
            <strong>Height:</strong> ${props.HEIGHT !== undefined ? props.HEIGHT.toFixed(2) + 'm' : 'N/A'}<br>
            <strong>Type:</strong> ${props.USE_TYPE || 'N/A'}`;
    } else {
        popupHTML += `<strong>ID:</strong> ${props.fkFloorGUID || props.UnitVerticalAddress || '—'}<br>
            <strong>Address:</strong> ${props.UnitVerticalAddress || props.fkShortAddress || 'N/A'}<br>
            <strong>Floor:</strong> ${props.FloorID ?? '—'}<br>
            <strong>Usage:</strong> ${props.UseType || '—'}<br>
            <strong>Building Height:</strong> ${props.BuildingHeight ? props.BuildingHeight.toFixed(1) + 'm' : '—'}`;
    }
    popupHTML += '</div>';

    const afterStyleLoad = () => _addHighlightAndAnimate(feature, bounds, popupPosition, popupHTML);

    // ── Switch to BDF 3D style for the highlight ──────────────────────────
    const bdfStyle = STYLES.find(s => s.id === 'bdf-style');
    if (currentStyleId !== 'bdf-style') {
        state.map.setStyle(bdfStyle.url);
        currentStyleId = 'bdf-style';
        state.map.once('idle', afterStyleLoad);
    } else {
        afterStyleLoad();
    }
}

/**
 * Add an orange 3D extrusion and animate the camera to the selected feature.
 * @param {Object} feature       - Elasticsearch document
 * @param {Object} bounds        - LngLatBounds
 * @param {Array}  popupPosition - [lng, lat]
 * @param {string} popupHTML     - Popup HTML string
 */
function _addHighlightAndAnimate(feature, bounds, popupPosition, popupHTML) {
    const layers    = state.map.getStyle().layers || [];
    const beforeId  = layers.find(l => l.type === 'symbol')?.id;
    const props     = feature.properties || {};
    const safeId    = (props.UNIT_ID || props.fkFloorGUID || props.UnitAddress || 'feat').replace(/[^a-z0-9]/gi, '-');
    const id        = `highlight-${safeId}`;

    currentHighlightIds.push(id);
    if (state.map.getSource(id)) state.map.removeSource(id);

    state.map.addSource(id, {
        type: 'geojson',
        data: { type: 'Feature', geometry: feature.geometry, properties: { ...feature } },
    });

    // ── Calculate extrusion heights ────────────────────────────────────────
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

    // Animate camera
    state.map.fitBounds(bounds, {
        padding: { top: 100, bottom: 100, left: 420, right: 100 },
        pitch: 60, bearing: -18, minZoom: 16, maxZoom: 19.5, duration: 1600, essential: true,
    });

    // Show popup after animation starts
    setTimeout(() => {
        currentPopup = new maplibregl.Popup({ offset: [15, 0], closeButton: true, className: 'unit-popup', maxWidth: '300px', anchor: 'left' })
            .setLngLat(popupPosition).setHTML(popupHTML).addTo(state.map);
        currentPopup.on('close', () => { currentPopup = null; });
    }, 800);
}


// ============================================================================
// SECTION 17 — APP ENTRY POINT
// ============================================================================

window.addEventListener('load', initMap);