// ============================================================================
// ROUTING & SERVICES ANALYSIS APPLICATION
// ============================================================================
// This application provides:
//   1. Route Planning     — A→B routing with fastest or shortest options
//   2. TSP Optimization   — Find the optimal path through multiple waypoints
//   3. Nearest Facility   — Locate nearby hospitals, fire stations, police, etc.
//   4. Service Area       — Calculate the reachable area within X minutes
//   5. Building Search    — Search indoor units and vertical addresses via Elasticsearch
//   6. Info Pointer       — Click any map feature to inspect its raw data properties


// ============================================================================
// SECTION 1: CONFIGURATION & CONSTANTS
// All static settings live here. Change these to customize the application.
// ============================================================================

// --- Map Style Definitions ---
// Each style is a different visual theme for the basemap.
// pitch   = how much the camera tilts (0 = flat top-down, 60 = steep 3D angle)
// bearing = compass rotation in degrees (0 = north up, -20 = slightly rotated)
const STYLES = [
    { id: 'basic-style', name: 'Default',   url: 'styles/martin/style.json',     pitch: 0,  bearing: 0   },
    { id: 'sat-style',   name: 'Satellite', url: 'styles/martin/style_sat.json', pitch: 0,  bearing: 0   },
    { id: '3d-style',    name: '3D',        url: 'styles/martin/style_3d.json',  pitch: 45, bearing: 0   },
    { id: 'bdf-style',   name: 'BDF',       url: 'styles/martin/style_bdf.json', pitch: 60, bearing: -20 }
];

// --- Backend API Endpoints ---
// All requests go through the Nginx /api/ proxy, which forwards them
// to the routing-api backend service (avoids CORS issues with direct calls).
const BACKEND_URL = '/api';
const API_ENDPOINTS = {
    route:           `${BACKEND_URL}/route`,             // A→B turn-by-turn routing
    tsp:             `${BACKEND_URL}/route/tsp`,          // Multi-point optimal route (TSP)
    nearestFacility: `${BACKEND_URL}/nearest_facility`,   // Find nearby hospitals, police, etc.
    serviceArea:     `${BACKEND_URL}/service_area`        // Reachable area polygon
};

// --- Elasticsearch Configuration ---
// Elasticsearch is the search engine that powers the building/unit search feature.
const ES_URL = 'http://localhost:9200';

// --- Map Default Settings ---
const DEFAULT_CENTER    = [46.6167, 24.8258]; // Riyadh, Saudi Arabia [longitude, latitude]
const DEFAULT_ZOOM      = 12;
const REQUEST_TIMEOUT   = 65000; // ms to wait before aborting an API call (TSP and facility queries can be slow)

// --- UI Control Limits ---
// These values drive the slider maximums. Change here to update the UI automatically.
const MAX_FACILITY_COUNT    = 20;  // Maximum number of facilities to retrieve
const MAX_SERVICE_MINUTES   = 20;  // Maximum service-area time in minutes
const MAX_SEARCH_DISTANCE_KM = 30; // Maximum facility search radius in km

// --- Facility Display Configuration ---
// Maps each facility type to its accent color and emoji icon.
// Keys must match the values in the facility-type dropdown in index.html.
const FACILITY_COLORS = {
    'hospital':     '#ef4444',
    'fire station': '#f97316',
    'police':       '#8b5cf6',
    'clinic':       '#10b981'
};
const FACILITY_ICONS = {
    'hospital':     '🏥',
    'fire station': '🚒',
    'police':       '👮',
    'clinic':       '⚕️'
};

// --- Route Color Palette ---
// Index 0 = primary (best) route, indices 1+ = alternative routes.
const ROUTE_COLORS = ['#0865fc', '#4f9af7', '#6095d3'];

// --- TSP Waypoint & Segment Colors ---
// Each waypoint marker and the route leg departing from it share the same color.
// Index 0 = waypoint 1 (and the leg from waypoint 1 to 2), and so on.
const TSP_COLORS = [
    '#3b82f6', // Blue
    '#ef4444', // Red
    '#10b981', // Green
    '#f59e0b', // Amber
    '#8b5cf6', // Purple
    '#ec4899', // Pink
    '#14b8a6', // Teal
    '#f97316', // Orange
    '#6366f1', // Indigo
    '#84cc16', // Lime
];


// ============================================================================
// SECTION 2: APPLICATION STATE
// ============================================================================
// "State" = the live memory of everything happening in the app right now.
// All mutable data lives here so any function can read or update it
// without relying on scattered global variables.

const state = {
    // --- Core Map Properties ---
    map:            null,             // The MapLibre GL map instance
    currentMode:    'route',          // Active tool: 'route' | 'tsp' | 'facility' | 'service'
    currentCenter:  DEFAULT_CENTER,   // Current map center [lng, lat]
    currentZoom:    DEFAULT_ZOOM,     // Current zoom level
    currentPitch:   0,                // Camera tilt angle (0–85 degrees)
    currentBearing: 0,                // Compass direction in degrees

    // --- Map Markers ---
    // Markers are the clickable pins the user places on the map.
    markers: {
        start:    null,  // Green "S" pin — start point for A→B routing
        end:      null,  // Red "E" pin — destination for A→B routing
        service:  null,  // Blue truck icon — center of the service area
        facility: null,  // Red pin — origin point for a facility search
        tsp:      []     // Array of { marker, lngLat, color } for TSP waypoints
    },

    // Result markers (hospital/police icons etc.) are kept separately so
    // they can be cleared without removing the search origin pin.
    facilityMarkers: [],

    // --- Saved API Responses ---
    // These are stored so we can redraw layers after a map style switch
    // without making new API calls.
    lastRouteData:    null, // Array of route FeatureCollections
    lastTSPRouteData: null, // { segments, waypoint_order }
    lastFacilityData: null, // Full /nearest_facility response
    lastServiceData:  null, // Full /service_area response

    // --- User Settings ---
    serviceMinutes:    5,         // Time limit for service area calculation
    facilityCount:     5,         // How many facilities to retrieve
    searchDistanceKm:  10,        // Search radius in kilometres
    routeOptimization: 'fastest', // 'fastest' or 'shortest'
    showAlternatives:  true,      // Whether to show alternative route lines

    // --- Info Pointer Tool ---
    infoPointerActive:      false, // Is the feature-inspect mode currently on?
    highlightedFeatureId:   null,  // ID of the feature currently highlighted
    highlightedSourceLayer: null,  // The map layer the highlighted feature belongs to

    // --- Draggable Info Panel ---
    isDragging:   false,          // True while the user is dragging the info panel
    dragOffset:   { x: 0, y: 0 },// Cursor offset from panel top-left at drag start
    panelPosition: null           // Last saved panel position
};

// Module-level variables that don't need to live inside the state object.
let currentDataset     = 'spl_units';   // Elasticsearch index: 'spl_units' or vertical addresses
let currentStyleId     = 'basic-style'; // ID of the currently active map style
let currentHighlightIds = [];           // Layer IDs for 3D search highlights (for cleanup)
let currentPopup        = null;         // The currently open maplibregl.Popup instance
let facilitySearchId    = 0;            // Incremented each search to discard stale responses


// ============================================================================
// SECTION 3: MAP INITIALIZATION
// ============================================================================

/**
 * Create the MapLibre map and wire up all controls and event handlers.
 * This is the application entry point — called once when the page loads.
 */
function initMap() {
    // Mount the map inside the <div id="map"> element.
    state.map = new maplibregl.Map({
        container: 'map',
        style:     STYLES[0].url,
        center:    state.currentCenter,
        zoom:      state.currentZoom,
        pitch:     state.currentPitch,
        bearing:   state.currentBearing,
        maxPitch:  85
    });

    // Keep camera state in sync so style switches can restore the exact view.
    state.map.on('moveend', () => {
        state.currentCenter  = state.map.getCenter().toArray();
        state.currentZoom    = state.map.getZoom();
        state.currentPitch   = state.map.getPitch();
        state.currentBearing = state.map.getBearing();
    });

    // Place navigation controls (zoom +/−, compass) in the appropriate corner
    // based on screen size: top-right on desktop, bottom-left on mobile.
    if (window.innerWidth <= 600) {
        state.map.addControl(new maplibregl.NavigationControl(), 'bottom-left');
    } else {
        state.map.addControl(new maplibregl.NavigationControl(), 'top-right');
    }

    // Add the custom style-switcher buttons (Default / Satellite / 3D / BDF).
    state.map.addControl(createLayerSwitcher(), 'bottom-right');

    // Set slider max values from our constants.
    initializeSliders();

    // Configure search panel behavior for phone vs desktop.
    initializeSearchPanel();

    // Inject the Info Pointer (ℹ️) and Panel Toggle (◀) buttons into the map UI.
    injectMapButtons();

    // Wire up all sidebar buttons, sliders, and the map click handler.
    setupEventHandlers();

    // On resize/orientation change, make sure the search panel is visible on desktop.
    window.addEventListener('resize', () => {
        setTimeout(() => {
            const searchPanel = document.querySelector('.search-panel');
            if (searchPanel && window.innerWidth > 600) {
                searchPanel.classList.add('expanded');
            }
        }, 200);
    });

    // Show the initial instruction once the map has finished loading.
    state.map.on('load', () => {
        showInfo('Click anywhere to begin routing of two points');
    });
}


// ============================================================================
// SECTION 4: MAP CONTROLS & UI HELPERS
// ============================================================================

/**
 * Build the layer-switcher control (Default / Satellite / 3D / BDF buttons).
 * MapLibre requires controls to implement onAdd() and onRemove() methods.
 *
 * @returns {Object} A MapLibre-compatible control object
 */
function createLayerSwitcher() {
    class LayerSwitcher {
        onAdd(map) {
            this.map = map;

            // Create the button group container in the bottom-right corner.
            this.container = document.createElement('div');
            this.container.className = 'maplibregl-ctrl maplibregl-ctrl-group flex flex-col bg-white';

            // One button per style.
            STYLES.forEach(style => {
                const btn = document.createElement('button');
                btn.type      = 'button';
                btn.className = 'px-4 py-3 text-sm font-medium hover:bg-blue-50 border-b border-gray-200 transition';
                // Use short labels so the buttons stay compact.
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

/**
 * Switch to a different basemap style and redraw all active data layers.
 * When map.setStyle() is called, MapLibre wipes all custom sources and layers,
 * so we must wait for the new style to finish loading and then rebuild them.
 *
 * @param {Object} style - One entry from the STYLES array
 */
async function switchMapStyle(style) {
    currentStyleId = style.id;

    // Snapshot the current camera so we can restore it after the style loads.
    const center  = state.map.getCenter();
    const zoom    = style.zoom    ?? state.map.getZoom();
    const pitch   = style.pitch   ?? state.map.getPitch();
    const bearing = style.bearing ?? state.map.getBearing();

    // Load the new style (this wipes all custom sources and layers).
    state.map.setStyle(style.url);

    state.map.once('idle', () => {
        // Restore the camera position.
        state.map.jumpTo({ center, zoom, pitch, bearing });

        // Rebuild Info Pointer highlight layers if that tool was active.
        if (state.infoPointerActive) {
            setupInfoPointerLayers();
        }

        // Redraw the data layers for whichever mode is currently active.
        switch (state.currentMode) {
            case 'route':    restoreRouteLayers();  break;
            case 'tsp':      restoreTSPRoute();     break;
            case 'facility': restoreFacilityData(); break;
            case 'service':  restoreServiceArea();  break;
        }
    });
}

/**
 * Inject the Info Pointer (ℹ️) button and the Panel Toggle (◀) button
 * into the map's control area. These can't go through addControl() because
 * they need to share a group with the existing navigation buttons.
 */
function injectMapButtons() {
    const topRight = document.querySelector('.maplibregl-ctrl-top-right'); // MapLibre's built-in control container
    if (!topRight) return; // If the map controls haven't loaded yet, wait a bit and try again

    // Create a control group to hold both buttons
    const ctrlGroup = document.createElement('div');
    ctrlGroup.className = 'maplibregl-ctrl maplibregl-ctrl-group';
    ctrlGroup.id        = 'custom-map-controls';

    // Info Pointer button — toggles feature-inspect mode.
    const infoBtn     = document.createElement('button');
    infoBtn.className = 'info-pointer-btn';
    infoBtn.type      = 'button';
    infoBtn.innerHTML = 'ℹ️';
    infoBtn.title     = 'Toggle Info Pointer';
    infoBtn.addEventListener('click', () => toggleInfoPointer());

    // Panel Toggle button — minimizes or restores the sidebar panel.
    const panelBtn     = document.createElement('button');
    panelBtn.className = 'panel-toggle-btn';
    panelBtn.type      = 'button';
    panelBtn.innerHTML = '◀';
    panelBtn.title     = 'Hide Control Panel';
    panelBtn.addEventListener('click', () => toggleControlPanel(panelBtn));

    ctrlGroup.appendChild(infoBtn);
    ctrlGroup.appendChild(panelBtn);

    // On mobile, move the buttons to the bottom-left instead of top-right.
    if (window.innerWidth <= 600) {
        const bottomLeft = document.querySelector('.maplibregl-ctrl-bottom-left')
                         || createBottomLeftContainer();
        bottomLeft.appendChild(ctrlGroup);
    } else {
        topRight.appendChild(ctrlGroup);
    }

    // When the panel is minimized, clicking its header bar should expand it again.
    const panel = document.querySelector('.control-panel.header');
    if (panel) {
        panel.addEventListener('click', function expandIfMinimised(e) {
            // Don't intercept clicks on interactive children.
            if (e.target.closest('button, input, select, label, .mode-btn, .route-opt-btn, .range-slider')) return;
            if (!panel.classList.contains('panel-minimised')) return;
            toggleControlPanel(panelBtn);
        });
    }
}

/**
 * Minimize or restore the left sidebar panel.
 *
 * @param {HTMLElement} btn - The toggle button element (◀ / ▶)
 */
function toggleControlPanel(btn) {
    const panel = document.querySelector('.control-panel.header');
    if (!panel) return;
 
    // Toggle minimized state
    const isMinimised = panel.classList.toggle('panel-minimised');

    // Update the button arrow and tooltip to reflect the new state.
    btn.classList.toggle('active', isMinimised);
    btn.innerHTML = isMinimised ? '▶' : '◀';
    btn.title     = isMinimised ? 'Show Control Panel' : 'Hide Control Panel';

    if (isMinimised) {
        // When minimized, a click anywhere on the panel header expands it.
        panel._expandHandler = (e) => {
            if (e.target.closest('button,input,select,label,.mode-btn,.route-opt-btn,.range-slider')) return;
            toggleControlPanel(btn);
        };
        panel.addEventListener('click', panel._expandHandler);
    } else {
        // When expanded, remove the expand-on-click handler.
        if (panel._expandHandler) {
            panel.removeEventListener('click', panel._expandHandler);
            delete panel._expandHandler;
        }
    }
}

/** Ensure a bottom-left MapLibre control container exists, creating one if needed. */
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
// SECTION 5: UI INITIALIZATION & EVENT HANDLERS
// ============================================================================

/**
 * Set slider max-value attributes from the constants defined at the top.
 * This way, changing MAX_FACILITY_COUNT (for example) automatically
 * updates the slider range without touching the HTML.
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
 * Configure the search panel for the current device size.
 * On desktop (> 600px) it starts expanded; on mobile it starts collapsed
 * and can be toggled by tapping the pill bar.
 */
function initializeSearchPanel() {
    const searchPanel = document.querySelector('.search-panel');
    if (!searchPanel) return;

    const isPhone = window.innerWidth <= 600;

    if (isPhone) {
        searchPanel.classList.remove('expanded');   // Start minimized on phone
    } else {
        searchPanel.classList.add('expanded');      // Start expanded on desktop
    }

    if (isPhone) {
        // Tapping the collapsed pill expands the search panel.
        const pill = searchPanel.querySelector('.search-panel-pill');
        if (pill) {
            pill.addEventListener('click', () => {
                searchPanel.classList.add('expanded');
                setTimeout(() => {
                    searchPanel.querySelector('#searchInput')?.focus();
                }, 350);
            });
        }

        // Tapping outside the expanded panel collapses it.
        document.addEventListener('click', (e) => {
            if (searchPanel.classList.contains('expanded') && !searchPanel.contains(e.target)) {
                searchPanel.classList.remove('expanded');
            }
        });
    }
}

/**
 * Attach event listeners to all sidebar controls.
 * Called once during map initialization.
 */
function setupEventHandlers() {
    // Route all map clicks through a single handler that branches by mode.
    state.map.on('click', (e) => {
        if (state.infoPointerActive) handleInfoPointerClick(e); // Inspect map feature
        else                          handleMapClick(e);         // Place markers / run tools
    });

    // Close button on the feature info panel.
    document.getElementById('close-feature-info')?.addEventListener('click', () => {
        state.infoPointerActive = true; // Force state to "on" so toggle turns it off
        toggleInfoPointer();
    });

    // Mode switcher buttons (Route / TSP / Facility / Service).
    ['route', 'tsp', 'facility', 'service'].forEach(mode => {
        document.getElementById(`mode-${mode}`)?.addEventListener('click', () => switchMode(mode));
    });

    // Route optimization toggle (Fastest / Shortest).
    document.getElementById('opt-fastest')?.addEventListener('click', () => {
        state.routeOptimization = 'fastest';
        updateRouteOptButtons();
        if (state.markers.start && state.markers.end) {
            calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());
        }
    });
    document.getElementById('opt-shortest')?.addEventListener('click', () => {
        state.routeOptimization = 'shortest';
        updateRouteOptButtons();
        if (state.markers.start && state.markers.end) {
            calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());
        }
    });

    // Show alternatives checkbox — recalculates the route immediately if both pins exist.
    document.getElementById('show-alternatives')?.addEventListener('change', (e) => {
        state.showAlternatives = e.target.checked;
        if (state.markers.start && state.markers.end) {
            calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());
        }
    });

    // Service area time slider.
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

    // Facility count slider.
    const facilityCountInput = document.getElementById('facility-count-input');
    const facilityCountValue = document.getElementById('facility-count-value');
    if (facilityCountInput && facilityCountValue) {
        facilityCountValue.textContent = facilityCountInput.value;
        facilityCountInput.addEventListener('input', (e) => {
            state.facilityCount            = parseInt(e.target.value, 10);
            facilityCountValue.textContent = state.facilityCount;
            if (state.markers.facility) calculateNearestFacilities(state.markers.facility.getLngLat());
        });
    }

    // Facility search-radius slider.
    const distanceInput = document.getElementById('distance-input');
    const distanceValue = document.getElementById('distance-value');
    if (distanceInput && distanceValue) {
        distanceValue.textContent = distanceInput.value;
        distanceInput.addEventListener('input', (e) => {
            state.searchDistanceKm    = parseInt(e.target.value, 10);
            distanceValue.textContent = state.searchDistanceKm;
            // Recalculate facilities if search is active
            if (state.markers.facility) calculateNearestFacilities(state.markers.facility.getLngLat());
        });
    }

    // Building search — press Enter to run.
    document.getElementById('searchInput')?.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') searchUnits();
    });

    // Dataset radio buttons (SPL Units vs. Vertical Addresses).
    document.querySelectorAll('input[name="dataset"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            currentDataset = e.target.value;
            document.getElementById('results').innerHTML = '';
            clearHighlight();
        });
    });
}

/**
 * Route a map click to the correct mode handler.
 * @param {Object} e - MapLibre click event
 */
function handleMapClick(e) {
    const handlers = {
        route:    handleRouteClick,
        tsp:      handleTSPClick,
        facility: handleFacilityClick,
        service:  handleServiceClick
    };
    const handler = handlers[state.currentMode];
    if (handler) {
        handler(e.lngLat);
    } else {
        console.warn(`Unknown mode: ${state.currentMode}`);
    }
}

/**
 * Highlight the active route-optimization button (Fastest or Shortest).
 */
function updateRouteOptButtons() {
    // Remove active class from all buttons
    document.querySelectorAll('.route-opt-btn').forEach(btn => btn.classList.remove('active'));
    // Add active class to selected button
    document.getElementById(state.routeOptimization === 'fastest' ? 'opt-fastest' : 'opt-shortest')
        ?.classList.add('active');
}


// ============================================================================
// SECTION 6: MODE SWITCHING
// ============================================================================

/**
 * Switch to a different tool mode (Route / TSP / Facility / Service).
 * Clears all existing markers and layers, then updates the UI.
 *
 * @param {string} mode - 'route' | 'tsp' | 'facility' | 'service'
 */
function switchMode(mode) {
    clearAll();                      // Remove all existing markers and layers
    state.currentMode = mode;        // Update current mode
    updateModeButtons(mode);         // Update button highlights
    updateModeInstructions(mode);    // Update instruction text

    // Always deactivate the Info Pointer when switching modes.
    if (state.infoPointerActive) toggleInfoPointer();
}

/**
 * Mark the active mode button as highlighted in the sidebar.
 * @param {string} activeMode - The currently active mode string
 */
function updateModeButtons(activeMode) {
    document.querySelectorAll('.mode-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById(`mode-${activeMode}`)?.classList.add('active');
}

/**
 * Show the relevant controls for the active mode and update the instruction text.
 * @param {string} mode - The currently active mode string
 */
function updateModeInstructions(mode) {
    // Toggle visibility of mode-specific control sections.
    document.getElementById('route-options')?.classList.toggle('hidden', mode !== 'route');
    document.getElementById('facility-selector')?.classList.toggle('hidden', mode !== 'facility');
    document.getElementById('facility-sliders-container')?.classList.toggle('hidden', mode !== 'facility');
    document.getElementById('time-slider')?.classList.toggle('hidden', mode !== 'service');

    // Each mode has its own instructional HTML and status message.
    const instructions = {
        route:    { html: 'Click: <span class="highlight start">Start</span> → <span class="highlight end">End</span>',
                    info: '🗺️ A→B Route mode active' },
        tsp:      { html: 'Click to add <span style="color:#8b5cf6;font-weight:bold">waypoints</span> (min 3)',
                    info: '🔄 TSP mode: Add at least 3 points' },
        facility: { html: 'Click a <span style="color:#ef4444;font-weight:bold">location</span> to find nearest facilities',
                    info: '🏥 Nearest Facility mode active' },
        service:  { html: 'Click <span style="color:#ef4444;font-weight:bold">service location</span> + adjust time',
                    info: '🚚 Service Area mode active' }
    };

    const cfg = instructions[mode];
    if (cfg) {
        const el = document.getElementById('mode-instruction');
        if (el) el.innerHTML = cfg.html;
        showInfo(cfg.info);
    }
}


// ============================================================================
// SECTION 7: ROUTE MODE (A→B)
// ============================================================================
// First click places a green Start pin; second click places a red End pin and
// immediately calculates the route. A third click resets and starts over.

/**
 * Handle a map click while in Route mode.
 * @param {Object} lngLat - Clicked coordinates { lng, lat }
 */
function handleRouteClick(lngLat) {
    if (!state.markers.start) {
        // First click — place the Start pin.
        state.markers.start = new maplibregl.Marker({ element: createMarker('S', '#10b981') })
            .setLngLat(lngLat)
            .addTo(state.map);
        showInfo('✅ Start set. Click destination');

    } else if (!state.markers.end) {
        // Second click — place the End pin and calculate the route.
        state.markers.end = new maplibregl.Marker({ element: createMarker('E', '#ef4444') })
            .setLngLat(lngLat)
            .addTo(state.map);
        calculateRoute(state.markers.start.getLngLat(), state.markers.end.getLngLat());

    } else {
        // Third click — reset everything and start over.
        clearAll();
        showInfo('🔄 Cleared. Click new start point');
    }
}

/**
 * Request a route from the API and draw it on the map.
 *
 * @param {Object} start - Start coordinates { lng, lat }
 * @param {Object} end   - End coordinates { lng, lat }
 */
async function calculateRoute(start, end) {
    clearRouteLayers();
    showInfo('⏳ Calculating routes...');

    try {
        const url  = `${API_ENDPOINTS.route}?start_lon=${start.lng}&start_lat=${start.lat}&end_lon=${end.lng}&end_lat=${end.lat}&alternatives=${state.showAlternatives ? 3 : 1}&optimization=${state.routeOptimization}`;
        const data = await fetchWithTimeout(url);

        if (data.error) throw new Error(data.error);

        const routes = data.routes;
        if (!routes || routes.length === 0) throw new Error('No routes returned by the server.');

        // Save so we can redraw after a style switch without a new API call.
        state.lastRouteData = routes;

        // Draw each alternative; the primary route is thicker and more opaque.
        routes.forEach((route, i) => {
            const color = ROUTE_COLORS[i] || '#5e8bbe';
            const opacity = i === 0 ? 0.9 : 0.6;   // Primary route more opaque
            const width = i === 0 ? 7 : 5;          // Primary route thicker
            addRouteLayer(route, color, opacity, width, `route-${i}`);
        });

        // Fit the viewport to all drawn routes.
        routes.length > 1 ? fitToMultipleRoutes(routes) : fitToFeatures(routes[0]);

        // Build a summary with color swatches so the user can match text to lines.
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

/**
 * Fit the map to show multiple routes at once.
 * @param {Array} routes - Array of GeoJSON FeatureCollections
 */
function fitToMultipleRoutes(routes) {
    const bounds = new maplibregl.LngLatBounds();
    routes.forEach(route => {
        route.features.forEach(f => {
            if (f.geometry?.coordinates) {
                f.geometry.coordinates.forEach(c => bounds.extend(c));
            }
        });
    });
    state.map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 1500 });
}


// ============================================================================
// SECTION 8: TSP MODE (Travelling Salesman Problem)
// ============================================================================
// The user clicks to add numbered waypoints. Once at least 3 exist,
// the app waits 2 seconds and then calculates the optimal visit order.
// Each waypoint marker and its outgoing route leg share the same color.

/**
 * Handle a map click in TSP mode — add a new waypoint.
 * Rejects clicks that are within 100 m of an existing waypoint to avoid
 * degenerate routes.
 *
 * @param {Object} lngLat - Clicked coordinates { lng, lat }
 */
function handleTSPClick(lngLat) {
    const MIN_DISTANCE_METERS = 100;

    // Rough distance check using Pythagorean theorem on degree differences.
    // (1 degree ≈ 111 320 m at the equator — close enough for a proximity guard.)
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
        .setLngLat(lngLat)
        .addTo(state.map);

    // Store the marker alongside its color so we can color route legs to match.
    state.markers.tsp.push({ marker, lngLat, color });

    if (state.markers.tsp.length < 3) {
        showInfo(`✅ Point ${num} added. Need ${3 - state.markers.tsp.length} more (min 3)`);
    } else {
        showInfo(`✅ Point ${num} added. Auto-calculating optimized route in 2 seconds...`);
        // Give the user a moment to keep adding points before we solve.
        setTimeout(() => {
            if (state.markers.tsp.length >= 3) calculateTSP();
        }, 2000);
    }
}

/**
 * Request an optimized multi-point route from the backend and draw it.
 */
async function calculateTSP() {
    if (state.markers.tsp.length < 3) {
        showInfo('❌ Need at least 3 points for TSP');
        return;
    }

    showInfo(`⏳ Solving TSP for ${state.markers.tsp.length} points...`);

    try {
        // Extract coordinates from markers (in the order they were added)
        const points = state.markers.tsp.map(m => [m.lngLat.lng, m.lngLat.lat]);
        
        // Send request to TSP API
        const data   = await fetchWithTimeout(API_ENDPOINTS.tsp, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ points })
        });

        if (data.error) throw new Error(data.error);

        // Clear any previous TSP layers before drawing new ones
        clearTSPLayers();

        // Save both segments and visit order for style-switch restoration.
        state.lastTSPRouteData = {
            segments:       data.segments,
            waypoint_order: data.waypoint_order
        };
        // Draw each leg segment with the color of its STARTING waypoint.
        drawTSPSegments(data.segments, data.waypoint_order);

        // Fit map to the first segment's bounds (rough but fast)
        if (data.segments?.length > 0) fitToFeatures(data.segments[0]);

        // Build a color-coded visit-order string for the info box.
        const orderStr = data.waypoint_order.map(idx => {
            const color = TSP_COLORS[idx % TSP_COLORS.length];
            return `<span style="color:${color};font-weight:bold;">${idx + 1}</span>`;
        }).join(' <span style="color:#6b7280;">→</span> ');

        showInfo(`
            ✅ <strong>TSP Optimized!</strong><br>
            Order: ${orderStr}<br>
            ${data.duration_minutes} min • ${data.total_distance_km} km • ${data.segment_count} legs
        `);

    } catch (error) {
        handleError('TSP calculation', error);
    }
}

/**
 * Draw each TSP route leg as a colored line.
 *
 * Color rule: leg i departs from waypoint_order[i].
 * The leg color = TSP_COLORS[ waypoint_order[i] % length ],
 * which matches the departure marker's color exactly.
 *
 * @param {Array} segments       - GeoJSON FeatureCollections, one per leg
 * @param {Array} waypoint_order - 0-based indices of waypoints in visit order
 *                                 (length = segments + 1; last entry closes the loop)
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
// SECTION 9: NEAREST FACILITY MODE
// ============================================================================
// The user clicks a location; the backend searches for nearby facilities
// (hospitals, fire stations, police, clinics) and returns route lines to each.
//
// Stale-request guard:
//   facilitySearchId increments on every new search. Each async call captures
//   its own copy. If the user clicks again before the first response arrives,
//   the old IDs won't match and that stale response is silently discarded.

/**
 * Handle a map click in Facility mode.
 * @param {Object} lngLat - Clicked coordinates { lng, lat }
 */
function handleFacilityClick(lngLat) {
    clearFacilityData();

    // Place the search-origin pin.
    state.markers.facility = new maplibregl.Marker({ element: createMarker('📍', '#dc2626') })
        .setLngLat(lngLat)
        .addTo(state.map);

    showInfo('⏳ Searching nearest facilities...');
    calculateNearestFacilities(lngLat);
}

/**
 * Request nearby facilities from the API and draw markers + route lines.
 *
 * @param {Object} lngLat - Search center coordinates { lng, lat }
 */
async function calculateNearestFacilities(lngLat) {
    const mySearchId   = ++facilitySearchId; // Capture this search's unique ID.
    const facilityType = document.getElementById('facility-type-select')?.value || 'hospital';

    showInfo(`⏳ Searching for nearest ${facilityType}s within ${state.searchDistanceKm}km...<br><small>This may take a few seconds</small>`);

    try {
        const url  = `${API_ENDPOINTS.nearestFacility}?lon=${lngLat.lng}&lat=${lngLat.lat}&type=${encodeURIComponent(facilityType)}&limit=${state.facilityCount}&max_distance_km=${state.searchDistanceKm}&routes=true`;
        const data = await fetchWithTimeout(url);

        // If the user clicked a new location while this request was running, discard it.
        if (mySearchId !== facilitySearchId) return;

        if (data.error) throw new Error(data.error);

        if (!data.facilities || data.facilities.length === 0) {
            showInfo(`ℹ️ No ${facilityType} found within ${state.searchDistanceKm}km radius`);
            return;
        }

        displayFacilityResults(lngLat, data, facilityType);

    } catch (error) {
        // Only show the error if this is still the active (non-superseded) search.
        if (mySearchId === facilitySearchId) handleError('Facility search', error);
    }
}

/**
 * Render facility markers and route lines for a completed search.
 * Also called by restoreFacilityData() after a style switch.
 *
 * @param {Object} lngLat       - The search origin { lng, lat }
 * @param {Object} data         - API response { facilities: [...], count: N }
 * @param {string} facilityType - e.g. 'hospital'
 */
function displayFacilityResults(lngLat, data, facilityType) {
    // Clear any leftovers from a previous search or slider-triggered update.
    removeFacilityLayers();
    removeFacilityMarkers();

    // Flatten all per-facility route features into one FeatureCollection.
    // This lets us draw all routes as a single efficient map layer.
    const allRouteFeatures = [];
    data.facilities.forEach((facility, index) => {
        if (facility.route?.features) {
            facility.route.features.forEach(f => {
                allRouteFeatures.push({
                    ...f,
                    properties: {
                        ...f.properties,
                        facility_name:  facility.name,
                        facility_rank:  index + 1,  // 1 = closest, 2 = second-closest, etc.
                        travel_minutes: facility.travel_minutes
                    }
                });
            });
        }
    });

    if (allRouteFeatures.length > 0) {
        const facilityColor = FACILITY_COLORS[facilityType] || '#6366f1';

        state.map.addSource('facility-routes', {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: allRouteFeatures }
        });

        state.map.addLayer({
            id:     'facility-routes',
            type:   'line',
            source: 'facility-routes',
            paint: {
                'line-color':   facilityColor,
                // Closest facility (rank 1) gets a wider line than farther ones.
                'line-width':   ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 6, 5, 3],
                'line-opacity': 0.8
            }
        });
    }

    // Create a marker for each facility result.
    state.facilityMarkers = data.facilities.map((facility, index) => {
        const icon  = FACILITY_ICONS[facility.type] || '📍';
        const color = FACILITY_COLORS[facility.type] || '#6366f1';

        const el        = document.createElement('div');
        el.className    = 'facility-marker';
        el.style.background = color;
        el.innerHTML    = icon;

        // Small rank badge in the corner of the marker.
        const badge           = document.createElement('div');
        badge.className       = 'rank-badge';
        badge.style.borderColor = color;
        badge.style.color     = color;
        badge.textContent     = index + 1;
        el.appendChild(badge);

        const popup = new maplibregl.Popup({
            offset: 25, closeButton: true, closeOnClick: true,
            closeOnMove: false, maxWidth: '300px'
        }).setHTML(`
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
            </div>
        `);

        const marker = new maplibregl.Marker({ element: el, anchor: 'center', offset: [0, -6] })
            .setLngLat([parseFloat(facility.facility_lon), parseFloat(facility.facility_lat)])
            .setPopup(popup)
            .addTo(state.map);

        // Store popup on marker for cleanup
        marker._popup = popup;

        // Show popup on hover; a short delay prevents it from disappearing when
        // the cursor moves from the marker into the popup itself.
        let closeTimeout;
        el.addEventListener('mouseenter', () => {
            clearTimeout(closeTimeout);
            if (!marker.getPopup().isOpen()) marker.togglePopup();
        });

        //Hide popup on mouse leave smoothly with a small delay to allow moving the mouse into the popup without it disappearing immediately
        el.addEventListener('mouseleave', () => {
            closeTimeout = setTimeout(() => {
                if (marker.getPopup().isOpen()) marker.togglePopup();
            }, 200);
        });

        return marker;
    });

    state.lastFacilityData = data;

    // Build the info-box summary list.
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

    // Zoom to fit all results and the search pin.
    const bounds = new maplibregl.LngLatBounds();
    bounds.extend([lngLat.lng, lngLat.lat]);
    data.facilities.forEach(f => bounds.extend([parseFloat(f.facility_lon), parseFloat(f.facility_lat)]));
    state.map.fitBounds(bounds, { padding: { top: 80, bottom: 80, left: 80, right: 80 }, maxZoom: 14, duration: 1000 });
}

/** Remove all facility result markers from the map and clear the array. */
function removeFacilityMarkers() {
    state.facilityMarkers.forEach(m => {
        if (m._popup?.isOpen()) m._popup.remove();
        m.remove();
    });
    state.facilityMarkers = [];
}

/** Remove the facility route source and layer from the map. */
function removeFacilityLayers() {
    ['facility-routes'].forEach(id => {
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    });
}

/**
 * Rebuild ONLY the route lines after a style switch.
 * Markers survive style switches automatically (they are DOM elements, not map layers),
 * so we only need to recreate the sources and layers.
 *
 * @param {Object} data         - Saved API response (state.lastFacilityData)
 * @param {string} facilityType - e.g. 'hospital'
 */
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
        data: { type: 'FeatureCollection', features: allRouteFeatures }
    });
    state.map.addLayer({
        id: 'facility-routes', type: 'line', source: 'facility-routes',
        paint: {
            'line-color':   facilityColor,
            'line-width':   ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 6, 5, 3],
            'line-opacity': 0.8
        }
    });
}


// ============================================================================
// SECTION 10: SERVICE AREA MODE
// ============================================================================
// The user clicks to set a service center; the map shades the area reachable
// within the chosen number of minutes by road.

/**
 * Handle a map click in Service Area mode.
 * @param {Object} lngLat - Clicked coordinates { lng, lat }
 */
function handleServiceClick(lngLat) {
    clearServiceArea();

    if (state.markers.service) {
        state.markers.service.remove();
        state.markers.service = null;
    }

    // Place the service-center truck marker.
    state.markers.service = new maplibregl.Marker({ element: createMarker('🚚', '#1c2ae1') })
        .setLngLat(lngLat)
        .addTo(state.map);

    calculateServiceArea(lngLat);
}

/**
 * Request a service area polygon from the API and draw it.
 * @param {Object} lngLat - Service center coordinates { lng, lat }
 */
async function calculateServiceArea(lngLat) {
    showInfo(`⏳ Calculating ${state.serviceMinutes}-min service area...`);

    try {
        // Fetch service area data
        const data = await fetchWithTimeout(
            `${API_ENDPOINTS.serviceArea}?lon=${lngLat.lng}&lat=${lngLat.lat}&minutes=${state.serviceMinutes}`
        );

        if (data.error) throw new Error(data.error);

        state.lastServiceData = data; // Save for style-switch restoration.
        
        // Build all layers
        drawServiceArea(data);

        // Zoom to fit the service area polygon.
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
 * Draw the service area layers on the map.
 * Extracted into its own function so it can be called both on first draw
 * and when restoring layers after a map style switch.
 *
 * @param {Object} data - Saved API response (state.lastServiceData)
 */
function drawServiceArea(data) {
    clearServiceArea(); // Remove old layers first to avoid "source already exists" errors.

    // Yellow lines showing the reachable road network.
    if (data.reachable_network) {
        state.map.addSource('service-network', { type: 'geojson', data: data.reachable_network });
        state.map.addLayer({
            id: 'service-network', type: 'line', source: 'service-network',
            paint: { 'line-color': '#f59e0b', 'line-width': 3, 'line-opacity': 0.6 }
        });
    }

    // Red semi-transparent fill polygon (the catchment area).
    if (data.service_area) {
        state.map.addSource('service-hull', { type: 'geojson', data: data.service_area });
        state.map.addLayer({
            id: 'service-hull', type: 'fill', source: 'service-hull',
            paint: { 'fill-color': '#dc2626', 'fill-opacity': 0.15 }
        });

        // Red dashed border around the catchment polygon.
        state.map.addLayer({
            id: 'service-border', type: 'line', source: 'service-hull',
            paint: { 'line-color': '#dc2626', 'line-width': 4, 'line-dasharray': [3, 2], 'line-opacity': 0.8 }
        });
    }
}


// ============================================================================
// SECTION 11: INFO POINTER FEATURE
// ============================================================================
// When active, clicking any visible map feature opens a floating panel that
// shows its raw GeoJSON properties. Useful for inspecting road types,
// building attributes, or any other layer data.

/**
 * Toggle the Info Pointer on or off.
 * ON  → cursor changes to crosshair; clicking a feature shows its data.
 * OFF → normal marker-placement behavior is restored.
 */
function toggleInfoPointer() {
    state.infoPointerActive = !state.infoPointerActive;

    const btn            = document.querySelector('.info-pointer-btn');
    const mapContainer   = document.getElementById('map');
    const featurePanel   = document.getElementById('feature-info-panel');
    const panelToggleBtn = document.querySelector('.panel-toggle-btn');

    if (state.infoPointerActive) {
        setupInfoPointerLayers(); // Create the hidden highlight layers.
        btn.classList.add('active');
        mapContainer.classList.add('info-pointer-active'); // CSS sets crosshair cursor.
        featurePanel.classList.remove('hidden');
        resetPanelPosition();    // Move the panel back to its default top-right position.
        setupDraggablePanel();   // Allow the user to drag the panel anywhere.
        clearFeatureHighlight(); // Remove any old highlight from the map
        updateFeatureInfo({ html: '<p class="info-hint">Click on any feature to see its details</p>' });

        // Auto-minimize the control panel to give more screen space for inspection.
        const controlPanel = document.querySelector('.control-panel.header');
        if (controlPanel && !controlPanel.classList.contains('panel-minimised')) {
            toggleControlPanel(panelToggleBtn);
        }

        // Attach cursor-change listeners for map panning.
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
 * Create the three hidden highlight layers (fill, line, circle) that
 * visually mark whichever feature the user clicks.
 * Called once when the tool activates and again after every style switch.
 */
function setupInfoPointerLayers() {
    // Create data source for highlighted features
    if (!state.map.getSource('feature-highlight')) {
        state.map.addSource('feature-highlight', {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: [] } // Starts empty.
        });
    }

    // One layer per geometry type so polygons, lines, and points all highlight correctly.
    const highlightLayers = [
        {
            id:     'feature-highlight-fill',
            type:   'fill',
            filter: ['==', ['geometry-type'], 'Polygon'],
            paint:  { 'fill-color': '#3b82f6', 'fill-opacity': 0.3 }
        },
        {
            id:     'feature-highlight-line',
            type:   'line',
            filter: ['any', ['==', ['geometry-type'], 'LineString'], ['==', ['geometry-type'], 'Polygon']],
            paint:  { 'line-color': '#3b82f6', 'line-width': 3, 'line-opacity': 0.8 }
        },
        {
            id:     'feature-highlight-point',
            type:   'circle',
            filter: ['==', ['geometry-type'], 'Point'],
            paint:  { 'circle-radius': 8, 'circle-color': '#3b82f6', 'circle-opacity': 0.6,
                      'circle-stroke-width': 2, 'circle-stroke-color': '#1e40af' }
        }
    ];

    highlightLayers.forEach(layer => {
        if (!state.map.getLayer(layer.id)) {
            state.map.addLayer({ ...layer, source: 'feature-highlight' });
        }
    });
}

/**
 * Handle a map click while the Info Pointer is active.
 * Queries rendered features at the clicked pixel, filters out our own overlay
 * layers, and displays the top basemap feature's properties.
 *
 * @param {Object} e - MapLibre click event (e.point = pixel coordinates)
 */
function handleInfoPointerClick(e) {
    // Do nothing if the user is dragging the panel (not actually clicking the map)
    if (!state.infoPointerActive || state.isDragging) return;

    // Ask MapLibre for all features rendered at this pixel
    const features = state.map.queryRenderedFeatures(e.point);

    if (!features || features.length === 0) {
        clearFeatureHighlight();
        updateFeatureInfo({ html: '<p class="info-hint">No features found at this location</p>' });
        return;
    }

    // Filter out our own overlay layers; we only want to inspect basemap features.
    const validFeatures = features.filter(f => {
        const id = f.layer.id;
        return !id.startsWith('feature-highlight') &&
               !id.startsWith('route')             &&
               !id.startsWith('tsp-segment')       &&
               !id.startsWith('service-')          &&
               !id.startsWith('facility-');
    });

    // If there are no valid features after filtering, show a message instead of an empty panel
    if (validFeatures.length === 0) {
        clearFeatureHighlight();
        updateFeatureInfo({ html: '<p class="info-hint">No base layer features at this location</p>' });
        return;
    }

    // The first item is topmost — the one the user most likely intended to click.
    const feature = validFeatures[0];
    // Highlight and display information
    updateFeatureHighlight(feature);
    displayFeatureInfo(feature);
}

/**
 * Put the clicked feature into the 'feature-highlight' source so the
 * blue overlay appears on the map.
 *
 * @param {Object} feature - A GeoJSON feature from queryRenderedFeatures()
 */
function updateFeatureHighlight(feature) {
    clearFeatureHighlight();

    state.highlightedFeatureId   = feature.id;
    state.highlightedSourceLayer = feature.sourceLayer;

    // Update the highlight layer with this feature
    const src = state.map.getSource('feature-highlight');
    if (src) src.setData({ type: 'FeatureCollection', features: [feature] });
}

/** Remove the blue highlight overlay from the map. */
function clearFeatureHighlight() {
    state.highlightedFeatureId   = null;
    state.highlightedSourceLayer = null;

    const src = state.map.getSource('feature-highlight');
    if (src) src.setData({ type: 'FeatureCollection', features: [] });
}

/**
 * Build and display the feature properties in the info panel.
 * @param {Object} feature - GeoJSON feature from queryRenderedFeatures()
 */
function displayFeatureInfo(feature) {
    const properties = feature.properties || {};
    const geomType   = feature.geometry.type;

    // Header: layer ID, source layer, and geometry type.
    let html = `
        <div class="feature-layer-info">
            <p><strong>Layer:</strong> ${feature.layer.id}</p>
            <p><strong>Source Layer:</strong> ${feature.sourceLayer || 'N/A'}</p>
            <p><strong>Geometry:</strong> ${geomType}</p>
        </div>
    `;

    // Add property information if available
    if (Object.keys(properties).length > 0) {
        html += '<div class="feature-properties">';

        // Sort keys alphabetically so the list is easy to read
        Object.keys(properties).sort().forEach(key => {
            const val = properties[key];
            if (val === null || val === undefined) return;// Skip empty values

            // Format numbers with commas, stringify nested objects, leave strings as-is.
            const formatted = typeof val === 'object' ? JSON.stringify(val)
                            : typeof val === 'number'  ? val.toLocaleString()
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

/**
 * Convert a raw GeoJSON property key into a human-readable label.
 * Examples: "building_height" → "Building height"
 *           "UNIT_ID"         → "UNIT ID"  (ALL_CAPS keys are kept mostly intact)
 *           "buildingType"    → "Building Type"
 *
 * @param {string} key - Raw property key
 * @returns {string}   Human-friendly label
 */
function formatPropertyKey(key) {
    if (key === key.toUpperCase()) {
        // ALL_CAPS keys (common in GIS data: UNIT_ID, USE_TYPE) — just replace underscores.
        return key.replace(/_/g, ' ').trim();
    }
    // Handle snake_case and camelCase.
    return key
        .replace(/_/g, ' ')
        .replace(/([A-Z])/g, ' $1')
        .replace(/^./, s => s.toUpperCase())
        .trim();
}

/**
 * Set the HTML content of the info panel body.
 * @param {Object} options
 * @param {string} options.html - HTML string to insert
 */
function updateFeatureInfo({ html }) {
    const content = document.getElementById('feature-info-content');
    if (content) content.innerHTML = html;
}


// ============================================================================
// SECTION 12: DRAGGABLE PANEL
// ============================================================================
// Allows the user to drag the feature-info panel to any position on screen.

/**
 * Attach mouse and touch drag handlers to the info panel's header bar.
 * Stored handler references allow removal on the next call, preventing
 * stacked (duplicate) listeners.
 */
function setupDraggablePanel() {
    const panel  = document.getElementById('feature-info-panel');
    const header = document.querySelector('.feature-info-header');
    if (!panel || !header) return;

    // Remove listeners from a previous activation.
    if (panel._dragStart) {
        header.removeEventListener('mousedown',   panel._dragStart);
        header.removeEventListener('touchstart',  panel._dragStart);
        document.removeEventListener('mousemove', panel._drag);
        document.removeEventListener('touchmove', panel._drag);
        document.removeEventListener('mouseup',   panel._dragEnd);
        document.removeEventListener('touchend',  panel._dragEnd);
    }

    let isDragging = false;
    let initialX = 0, initialY = 0;

    const dragStart = (e) => {
        if (e.target.closest('.close-btn')) return; // Don't drag when closing.

        // Works for both mouse and touch events.
        const touch = e.touches?.[0] || e;
        const rect  = panel.getBoundingClientRect();

        // Offset keeps the panel from jumping to the cursor on drag start.
        initialX    = touch.clientX - rect.left;
        initialY    = touch.clientY - rect.top;
        isDragging  = true;
        state.isDragging = true;

        // Switch from right/top anchor to explicit left/top for free positioning.
        panel.style.right     = 'auto';
        panel.style.left      = rect.left + 'px';
        panel.style.top       = rect.top  + 'px';
        panel.style.transform = 'none';
    };

    const drag = (e) => {
        if (!isDragging) return;
        e.preventDefault();
        const touch = e.touches?.[0] || e;

        // Clamp within the viewport so the panel can't be dragged off-screen.
        const newX = Math.max(0, Math.min(touch.clientX - initialX, window.innerWidth  - panel.offsetWidth));
        const newY = Math.max(0, Math.min(touch.clientY - initialY, window.innerHeight - panel.offsetHeight));
        panel.style.left = newX + 'px';
        panel.style.top  = newY + 'px';
    };

    const dragEnd = () => {
        isDragging       = false;
        state.isDragging = false;
    };

    header.addEventListener('mousedown',   dragStart);
    document.addEventListener('mousemove', drag);
    document.addEventListener('mouseup',   dragEnd);

    header.addEventListener('touchstart', dragStart, { passive: false });
    document.addEventListener('touchmove', drag,     { passive: false });
    document.addEventListener('touchend',  dragEnd);

    // Save references so they can be removed on the next call.
    panel._dragStart = dragStart;
    panel._drag      = drag;
    panel._dragEnd   = dragEnd;
}

/** Reset the info panel to its default top-right corner position. */
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
// SECTION 13: BUILDING SEARCH (Elasticsearch)
// ============================================================================
// Searches two Elasticsearch indices:
//   • building_units      — SPL unit polygons (spl_units dataset)
//   • buildings_vertical  — Vertical address / floor-level units
//
// Clicking a result switches to the BDF style, zooms to the feature,
// highlights it as an orange 3D extrusion, and shows a popup.

/**
 * Run a search against Elasticsearch.
 * Uses multi_match so multiple fields are queried at once,
 * and fuzziness:AUTO to tolerate minor typos.
 */
async function searchUnits() {
    const queryText  = document.getElementById('searchInput').value.trim();
    const resultsDiv = document.getElementById('results');

    if (!queryText) {
        resultsDiv.innerHTML = '<div class="no-results">Please enter a search term</div>';
        return;
    }

    resultsDiv.innerHTML = '<div class="loading">Searching...</div>';
    clearHighlight();

    // Select the correct index and fields for the active dataset.
    const index  = currentDataset === 'spl_units' ? 'building_units' : 'buildings_vertical';
    const fields = currentDataset === 'spl_units'
        ? ['properties.UNIT_ID', 'properties.NAME^2', 'properties.NAME_LONG',
           'properties.UnitAddres', 'properties.LabelNames']
        : ['properties.UnitVerticalAddress^3', 'properties.fkShortAddress^1.8'];

    const esQuery = {
        query: {
            multi_match: {
                query:     queryText,
                fields,
                type:      'best_fields',
                fuzziness: 'AUTO' // Tolerates up to 2 character differences.
            }
        },
        size: 20 // Return at most 20 results
    };

    try {
        const response = await fetch(`${ES_URL}/${index}/_search`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(esQuery)
        });

        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        resultsDiv.innerHTML = '';

        if (data.hits.hits.length === 0) {
            resultsDiv.innerHTML = '<div class="no-results">No results found.</div>';
            return;
        }

        // Render each result as a clickable list item.
        data.hits.hits.forEach(hit => {
            const feature = hit._source;
            const props   = feature.properties || {};
            const item    = document.createElement('div');
            item.className = 'result-item';

            if (currentDataset === 'spl_units') {
                item.innerHTML = `
                    <strong>${props.UNIT_ID || 'N/A'}</strong><br>
                    ${props.LabelNames || 'Unnamed'} (${props.UnitAddres || 'No address'})<br>
                    <small>Floor Height: ${props.Base !== undefined ? props.Base.toFixed(2) + 'm' : 'N/A'} | Type: ${props.USE_TYPE || 'N/A'}</small>
                `;
            } else {
                item.innerHTML = `
                    <strong>${props.UnitVerticalAddress || props.fkFloorGUID || '—'}</strong><br>
                    Floor ${props.FloorID ?? '—'} – ${props.UseType || '—'}<br>
                    <small>Address: ${props.UnitVerticalAddress || props.fkShortAddress || 'No address'} | Building: ${props.BuildingHeight ? props.BuildingHeight.toFixed(1) + 'm' : '—'}</small>
                `;
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
 * Zoom to a search result, switch to the 3D BDF style, and highlight the feature.
 * The BDF style is always required here because it contains the indoor data layers.
 *
 * @param {Object}      feature        - Elasticsearch document (_source)
 * @param {HTMLElement} clickedElement - The result list item that was clicked
 */
async function zoomToFeature(feature, clickedElement) {
    if (!state.map) { console.warn('Map not ready'); return; }

    clearHighlight();
    clickedElement.classList.add('active');

    if (!feature.geometry) {
        alert('No geometry available for this feature.');
        return;
    }

    // Calculate the bounding box of the feature's geometry.
    const bounds = new maplibregl.LngLatBounds();
    const props  = feature.properties || {};
    const flattenCoords = (arr) => {
        if (typeof arr[0] === 'number') bounds.extend([arr[0], arr[1]]);
        else arr.forEach(flattenCoords);
    };
    flattenCoords(feature.geometry.coordinates);

    if (bounds.isEmpty()) { alert('No valid geometry found.'); return; }

    const popupPosition = getPopupAnchorPosition(bounds);

    // Build the popup HTML based on which dataset the result came from.
    let popupHTML = `
        <div style="max-width:280px;font-size:14px;line-height:1.6;">
            <strong style="font-size:16px;color:#1f2937;">
                ${props.NAME || props.FloorUsage || props.UnitAddress || 'Feature'}
            </strong><br>
    `;
    if (currentDataset === 'spl_units') {
        popupHTML += `
            <strong>Unit ID:</strong> ${props.UNIT_ID || '—'}<br>
            <strong>Address:</strong> ${props.UnitVerticalAddress || 'N/A'}<br>
            <strong>Floor:</strong> ${props.Base !== undefined ? props.Base.toFixed(2) + 'm' : 'N/A'}<br>
            <strong>Height:</strong> ${props.HEIGHT !== undefined ? props.HEIGHT.toFixed(2) + 'm' : 'N/A'}<br>
            <strong>Type:</strong> ${props.USE_TYPE || 'N/A'}
        `;
    } else {
        popupHTML += `
            <strong>ID:</strong> ${props.fkFloorGUID || props.UnitVerticalAddress || '—'}<br>
            <strong>Address:</strong> ${props.UnitVerticalAddress || props.fkShortAddress || 'N/A'}<br>
            <strong>Floor:</strong> ${props.FloorID ?? '—'}<br>
            <strong>Usage:</strong> ${props.UseType || '—'}<br>
            <strong>Type:</strong> ${props.Occupant || '—'}<br>
            <strong>Total Floors:</strong> ${props.NoofFloors || '—'}<br>
            <strong>Building Height:</strong> ${props.BuildingHeight ? props.BuildingHeight.toFixed(1) + 'm' : '—'}
        `;
    }
    popupHTML += '</div>';

    const afterStyleLoad = () => addHighlightAndAnimate(feature, bounds, popupPosition, popupHTML);

    // Always switch to BDF style before highlighting indoor features.
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
 * Add an orange 3D fill-extrusion at the correct floor height and animate
 * the camera to show it. Called once the BDF style is confirmed to be loaded.
 *
 * @param {Object} feature       - Elasticsearch document
 * @param {Object} bounds        - LngLatBounds of the feature
 * @param {Array}  popupPosition - [lng, lat] anchor for the popup
 * @param {string} popupHTML     - HTML content for the popup
 */
function addHighlightAndAnimate(feature, bounds, popupPosition, popupHTML) {
    const layers         = state.map.getStyle().layers || [];
    // Insert the highlight below the first symbol layer so labels still render on top.
    const firstSymbolLayer = layers.find(l => l.type === 'symbol')?.id;
    const beforeId         = firstSymbolLayer ?? undefined;

    const props  = feature.properties || {};
    // Build a URL-safe ID from the feature's identifier.
    const safeId = (props.UNIT_ID || props.fkFloorGUID || props.UnitAddress || 'feat')
        .replace(/[^a-z0-9]/gi, '-');
    const id = `highlight-${safeId}`;

    // Track for cleanup
    currentHighlightIds.push(id);

    // Remove any existing layer/source with this ID to avoid "already exists" errors.
    if (state.map.getSource(id)) state.map.removeSource(id);

    state.map.addSource(id, {
        type: 'geojson',
        data: { type: 'Feature', geometry: feature.geometry, properties: { ...feature } }
    });

    // Calculate the 3D extrusion heights so the highlight sits at the correct floor.
    let extrusionBase, extrusionHeight;
    if (currentDataset === 'spl_units') {
        // Units: use Base and HEIGHT properties
        extrusionBase   = props.Base || 0;
        extrusionHeight = extrusionBase + (props.HEIGHT || 4.25);
    } else {
        // Floors: calculate from building height and floor number
        const floorH    = (props.BuildingHeight || 0) / (props.FloorsAboveGround || 1);
        extrusionBase   = floorH * (props.FloorID || 0);
        extrusionHeight = extrusionBase + floorH;
    }

    state.map.addLayer({
        id,
        type:   'fill-extrusion',
        source: id,
        paint: {
            'fill-extrusion-color':   '#ff5c00', // Orange highlight
            'fill-extrusion-opacity': 0.95,
            'fill-extrusion-height':  extrusionHeight,
            'fill-extrusion-base':    extrusionBase
        }
    }, beforeId);

    // Animate camera to the feature with a strong 3D tilt.
    state.map.fitBounds(bounds, {
        padding:  { top: 100, bottom: 100, left: 420, right: 100 },
        pitch:    60,
        bearing:  -18,
        minZoom:  16,
        maxZoom:  19.5,
        duration: 1600,
        essential: true
    });

    // Show the popup 0.8 s after the animation starts so it appears smoothly.
    setTimeout(() => {
        currentPopup = new maplibregl.Popup({
            offset: [15, 0], closeButton: true,
            className: 'unit-popup', maxWidth: '300px', anchor: 'left'
        })
            .setLngLat(popupPosition)
            .setHTML(popupHTML)
            .addTo(state.map);

        currentPopup.on('close', () => { currentPopup = null; });
    }, 800);
}

/**
 * Calculate a good [lng, lat] anchor position for a popup relative to
 * the feature's bounding box — slightly to the right and above center.
 *
 * @param {Object} bounds - MapLibre LngLatBounds
 * @returns {Array} [lng, lat]
 */
function getPopupAnchorPosition(bounds) {
    const ne = bounds.getNorthEast();
    const sw = bounds.getSouthWest();
    return [
        ne.lng + (ne.lng - sw.lng) * 0.3,  // 30% to the right of the box
        sw.lat + (ne.lat - sw.lat) * 0.65  // 65% up from the bottom
    ];
}

/**
 * Remove all 3D search highlight layers and close any open popup.
 */
function clearHighlight() {
    currentHighlightIds.forEach(id => {
        if (state.map?.getLayer(id))  state.map.removeLayer(id);
        if (state.map?.getSource(id)) state.map.removeSource(id);
    });
    currentHighlightIds = [];

    if (currentPopup) { currentPopup.remove(); currentPopup = null; }
    document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
}


// ============================================================================
// SECTION 14: RESTORE FUNCTIONS (after map style switch)
// ============================================================================
// Each function redraws the data for one mode after a style switch wipes
// all custom layers. They read from saved state so no API calls are needed.

/** Redraw A→B route lines after a style switch. */
function restoreRouteLayers() {
    if (!Array.isArray(state.lastRouteData)) return;
    state.lastRouteData.forEach((route, i) => {
        addRouteLayer(route, ROUTE_COLORS[i] || '#5e8bbe', i === 0 ? 0.9 : 0.6, i === 0 ? 7 : 5, `route-${i}`);
    });
}

/**
 * Redraw TSP route segments after a style switch.
 * Markers are DOM elements and survive style switches automatically.
 */
function restoreTSPRoute() {
    if (!state.lastTSPRouteData) return;

    const { segments, waypoint_order } = state.lastTSPRouteData;

    // Clean up any leftover TSP layers (support up to 50 segments).
    for (let i = 0; i < 50; i++) {
        const id = `tsp-segment-${i}`;
        if (state.map.getLayer(id)) state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }

    // In some MapLibre versions, 'idle' fires before the style is fully loaded.
    // Guard against this and retry on the next 'styledata' event if needed.
    if (!state.map.isStyleLoaded()) {
        state.map.once('styledata', () => restoreTSPRoute());
        return;
    }

    drawTSPSegments(segments, waypoint_order);
}

/**
 * Rebuild facility route lines after a style switch.
 * Markers survive style switches on their own (they are DOM elements, not map layers),
 * so we only recreate the sources and layers.
 */
function restoreFacilityData() {
    if (!state.lastFacilityData || !state.markers.facility) return;
    const facilityType = document.getElementById('facility-type-select')?.value || 'hospital';
    
    // Remove any leftover facility layers before we add new ones
    removeFacilityLayers();
    
    // Re-draw all facility result markers and their route lines
    rebuildFacilityRouteLayers(state.lastFacilityData, facilityType);
}

/** Redraw service area polygon and road network after a style switch. */
function restoreServiceArea() {
    if (!state.lastServiceData || !state.markers.service) return;
    drawServiceArea(state.lastServiceData);
}


// ============================================================================
// SECTION 15: CLEANUP FUNCTIONS
// ============================================================================
// Each function removes specific layers, markers, and saved state.

/** Remove all A→B route layers (supports up to 20 alternatives). */
function clearRouteLayers() {
    // Remove alternative route layers (support up to 20 alternatives)
    for (let i = 0; i < 20; i++) {
        const id = `route-${i}`;
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }
    // Legacy single-route layer (backward compatibility).
    if (state.map.getLayer('route'))  state.map.removeLayer('route');
    if (state.map.getSource('route')) state.map.removeSource('route');
    state.lastRouteData = null;
}

/** Remove all TSP route-segment layers (supports up to 50 segments). */
function clearTSPLayers() {
    for (let i = 0; i < 50; i++) {
        const id = `tsp-segment-${i}`;
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
    }
    state.lastTSPRouteData = null;
}

/**
 * Remove the facility search pin, result markers, and route lines.
 * Also clears the saved data so a style-switch restore doesn't redraw stale results.
 */
function clearFacilityData() {
    // Remove the search-location pin (the red map pin the user clicked)
    if (state.markers.facility) {
        state.markers.facility.remove();
        state.markers.facility = null;
    }

    // Remove all facility result markers (the hospital/police/etc. icons)
    state.facilityMarkers.forEach(m => m.remove());
    state.facilityMarkers = [];

    // Remove the route lines connecting search point to each facility
    removeFacilityLayers();

    // Clear the saved data so the style-switch restore doesn't redraw stale results
    state.lastFacilityData = null;
}

/**
 * Remove service area polygon and road-network layers.
 * Layers must be removed BEFORE their sources — MapLibre enforces this order.
 */
function clearServiceArea() {
    if (!state.map) return;
    ['service-border', 'service-hull', 'service-network'].forEach(id => {
        if (state.map.getLayer(id)) state.map.removeLayer(id);
    });
    ['service-hull', 'service-network'].forEach(id => {
        if (state.map.getSource(id)) state.map.removeSource(id);
    });
}

/**
 * Master cleanup — removes everything and resets to the initial state.
 * Called when switching modes.
 */
function clearAll() {
    clearRouteLayers();
    clearTSPLayers();
    clearFacilityData();
    clearServiceArea();
    clearHighlight();
    clearFeatureHighlight();

    // Remove all individual map markers.
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
// SECTION 16: UTILITY FUNCTIONS
// ============================================================================
// Small helpers used across multiple features.

/**
 * Fetch a URL with an automatic timeout.
 * Throws a friendly error if the server doesn't respond in time, or returns
 * a non-2xx status code.
 *
 * @param {string} url     - The URL to request
 * @param {Object} options - Standard fetch() options (method, headers, body, etc.)
 * @returns {Promise<Object>} Parsed JSON response
 */
async function fetchWithTimeout(url, options = {}) {
    const controller = new AbortController();
    const timeoutId  = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);

    try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        clearTimeout(timeoutId);

        if (!response.ok) {
            // Try to read a detailed error message from the JSON body (FastAPI sends these).
            let detail = `${response.status} ${response.statusText}`;
            try {
                const body = await response.json();
                if (body.detail) detail = body.detail;
            } catch { /* body wasn't JSON — fall back to the status text */ }
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
 * Log an error and show a user-friendly message in the info box.
 *
 * @param {string} context - Brief description of what failed (e.g. 'Route calculation')
 * @param {Error}  error   - The thrown error
 */
function handleError(context, error) {
    console.error(`${context} error:`, error);
    const message = error.message.includes('Failed to fetch')
        ? 'Network error. Check your connection and try again.'
        : error.message;
    showInfo(`❌ ${message}`);
}

/**
 * Create a custom circular marker element for use with MapLibre Marker().
 *
 * @param {string} text    - Text or emoji displayed inside the circle
 * @param {string} bgColor - Background CSS color (e.g. '#ef4444')
 * @returns {HTMLElement}
 */
function createMarker(text, bgColor = null) {
    const el = document.createElement('div');
    el.className = 'marker';

    // Special CSS classes used by the stylesheet for specific marker types.
    if (text === '🚚') el.classList.add('marker-service');
    if (text === 'E')  el.classList.add('marker-end');

    if (bgColor) el.style.backgroundColor = bgColor;
    el.textContent = text;
    return el;
}

/**
 * Display a message in the info box at the bottom of the sidebar.
 * Supports HTML so you can use <strong>, <small>, <br>, colour spans, etc.
 *
 * @param {string} text - HTML string to display
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
 * Add a GeoJSON line layer to the map. If a source/layer with the same ID
 * already exists it is removed first to prevent "already exists" errors.
 *
 * @param {Object} data    - GeoJSON FeatureCollection
 * @param {string} color   - Line color in CSS hex
 * @param {number} opacity - Line opacity 0–1
 * @param {number} width   - Line width in pixels
 * @param {string} layerId - Unique ID for cleanup later
 */
function addRouteLayer(data, color, opacity = 0.9, width = 7, layerId = 'route') {
    if (state.map.getLayer(layerId))  state.map.removeLayer(layerId);
    if (state.map.getSource(layerId)) state.map.removeSource(layerId);

    state.map.addSource(layerId, { type: 'geojson', data });
    state.map.addLayer({
        id: layerId, type: 'line', source: layerId,
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint:  { 'line-color': color, 'line-width': width, 'line-opacity': opacity }
    });
}

/**
 * Zoom and pan the map to fit a GeoJSON FeatureCollection in the viewport.
 * @param {Object} data - GeoJSON FeatureCollection
 */
function fitToFeatures(data) {
    const bounds = new maplibregl.LngLatBounds();
    
    // Extend bounds to include all coordinates
    data.features.forEach(f => {
        if (f.geometry?.coordinates) {
            f.geometry.coordinates.forEach(c => bounds.extend(c));
        }
    });
    
    // Animate to bounds
    state.map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 1500 });
}

/**
 * Change the cursor to a grab icon when the user starts panning the map,
 * and restore it when they release the mouse button.
 */
function onMapMouseDown(e) {
    const mapContainer = document.getElementById('map');
    if (!mapContainer) return;
    mapContainer.classList.add('map-info');

    const onMouseMove = () => {
        mapContainer.classList.remove('map-info');
        mapContainer.classList.add('map-panning');
    };

    document.addEventListener('mousemove', onMouseMove, { once: true });
    document.addEventListener('mouseup',   () => {
        mapContainer.classList.remove('map-info', 'map-panning');
        document.removeEventListener('mousemove', onMouseMove);
    }, { once: true });
}

function onMapMouseUp() {
    const mapContainer = document.getElementById('map');
    if (mapContainer) mapContainer.classList.remove('map-info', 'map-panning');
}


// ============================================================================
// SECTION 17: APPLICATION BOOT
// ============================================================================
// Start the application when page loads

/**
 * Initialize the application
 * This is the entry point that runs when the DOM is ready
 */
window.addEventListener('load', initMap);