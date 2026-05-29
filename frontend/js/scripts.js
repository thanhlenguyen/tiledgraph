// =============================================================================
// GeoRouting Lab — scripts.js
//
// Features:
//  1. Route comparison        (Valhalla, 3 alternatives, Car/Bike/Walk)
//  2. VRP / TSP               (VROOM, per-segment coloring)
//  3. Isochrone               (Valhalla, configurable levels + interval)
//  4. Nearest Facility        (PostGIS spatial query → Valhalla routing)
//  5. Info Pointer            (click any map feature to inspect its properties)
//  6. Building Search         (Elasticsearch, SPL Units + Vertical Addresses)
// =============================================================================

// ---------------------------------------------------------------------------
// Map styles
// ---------------------------------------------------------------------------
const STYLES = [
  { id: 'default', name: 'Default',   url: '/styles/style.json',     pitch: 0,  zoom: 13, bearing: 0  },
  { id: 'sat',     name: 'Satellite', url: '/styles/style_sat.json', pitch: 0,  zoom: 13, bearing: 0  },
  { id: '3d',      name: '3D',        url: '/styles/style_3d.json',  pitch: 45, zoom: 14, bearing: 0  },
  { id: 'bdf',     name: 'BDF',       url: '/styles/style_bdf.json', pitch: 60, zoom: 17, bearing: -20}
];

// ---------------------------------------------------------------------------
// API endpoints  (via Nginx /api/ proxy)
// ---------------------------------------------------------------------------
const BACKEND_URL = '/api';  // Nginx proxies /api/ → routing-api:5000
const API = {
  route:          `${BACKEND_URL}/route`,
  optimize:       `${BACKEND_URL}/optimize_route`,
  isochrone:      `${BACKEND_URL}/isochrone`,
  nearestFacility:`${BACKEND_URL}/nearest_facility`,   // PostGIS + Valhalla
};
const TIMEOUT = 120000;
const ES_URL = 'http://localhost:9200';

// ---------------------------------------------------------------------------
// Colour palettes
// ---------------------------------------------------------------------------
// 3 alternative routes
const ROUTE_COLORS = ['#3b9eff', '#ff9f3b', '#a855f7'];
const ROUTE_NAMES = ['Fastest', 'Alt — avoid highways', 'Alt — shortest distance'];

// Up to 10 VRP stops (css variables mirror these)
const STOP_COLORS = [
  '#f43f5e','#fb923c','#facc15','#4ade80','#22d3ee',
  '#818cf8','#e879f9','#f9a8d4','#86efac','#fde68a'
];

const ISO_COLORS = ['#22d98a', '#3b9eff', '#f5a623', '#f43f5e', '#a855f7'];

// Facility type → display config.
// Keys MUST match _ALLOWED_FACILITY_TYPES in app.py and data-ftype in index.html.
const FACILITY_CONFIG = {
  'hospital':     { icon: '🏥', color: '#ef4444', label: 'Hospital'     },
  'fire station': { icon: '🚒', color: '#f97316', label: 'Fire Station'  },
  'police':       { icon: '👮', color: '#8b5cf6', label: 'Police'        },
  'clinic':       { icon: '🩺', color: '#10b981', label: 'Clinic'        },
};

// ---------------------------------------------------------------------------
// Global map state
// ---------------------------------------------------------------------------
let map;
let currentCenter  = [46.597, 24.876];  // Default center Riyahd;
let currentZoom    = 13;
let currentPitch   = 0;
let currentBearing = 0;
let currentStyleId = 'default';

// ---------------------------------------------------------------------------
// Mode system — only one mode active at a time
// ---------------------------------------------------------------------------
const MODE = {
  NONE:  'none',
  ROUTE: 'route',       // picking S then E for route comparison
  VRP:   'vrp',         // adding stops for VRP/TSP
  ISO:   'iso',         // single click for isochrone
  FACILITY: 'facility'  // single click for nearest facilities
};
let activeMode = MODE.NONE;

function enterMode(mode) {
  activeMode = mode;
  const mapEl = document.getElementById('map');
  if (mode !== MODE.NONE && !infoPointerActive) {
    map.getCanvas().style.cursor = 'crosshair';
    mapEl.classList.add('info-mode');
  }
}
function exitMode() {
  activeMode = MODE.NONE;
  if (!infoPointerActive) {
    map.getCanvas().style.cursor = '';
    document.getElementById('map').classList.remove('info-mode');
  }
}

// ===========================================================================
// MAP INIT
// ===========================================================================
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

  map.on('moveend', () => {
    currentCenter  = map.getCenter().toArray();
    currentZoom    = map.getZoom();
    currentPitch   = map.getPitch();
    currentBearing = map.getBearing();
  });

  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  map.addControl(new maplibregl.ScaleControl(),      'bottom-left');
  map.addControl(new LayerSwitcherControl(),         'bottom-right');

  // Inject info-pointer button into top-right control group
  map.once('load', () => {
    _injectInfoPointerBtn();
    console.log('[map] loaded');
  });

  map.on('styledata', () => {
    map.once('idle', _restoreActiveLayers);
  });

  map.on('click', handleMapClick);

  // Dataset radio listeners
  document.querySelectorAll('input[name="dataset"]').forEach(r => {
    r.addEventListener('change', e => {
      currentDataset = e.target.value;
      document.getElementById('results').innerHTML = '';
    });
  });
}

// ---------------------------------------------------------------------------
// Master click handler
// ---------------------------------------------------------------------------
function handleMapClick(e) {
  if (infoPointerActive) {
    handleInfoPointerClick(e);
    return;
  }
  const ll = [e.lngLat.lng, e.lngLat.lat];
  if      (activeMode === MODE.ROUTE)    routeHandleClick(ll);
  else if (activeMode === MODE.VRP)      vrpHandleClick(ll);
  else if (activeMode === MODE.ISO)      isoHandleClick(ll);
  else if (activeMode === MODE.FACILITY) facilityHandleClick(ll);
}

// ---------------------------------------------------------------------------
// Restore all active layers after a style switch
// ---------------------------------------------------------------------------
function _restoreActiveLayers() {
  // Re-draw route comparison lines
  routeState.drawn.forEach((d, i) => {
    _addRouteLayer(i, d.coordinates, d.color, d.dash);
  });
  // Re-draw VRP segments
  vrpState.drawnSegments.forEach(seg => {
    _addVrpSegmentLayer(seg.srcId, seg.layerId, seg.coordinates, seg.color);
  });
  // Re-draw isochrone
  if (isoState.geojson) {
    _renderIso(isoState.geojson);
  }
  //Re-draw nearest facilities
  if (facilityState.routeFeatures.length > 0){
    _renderFacilityRoutes(facilityState.routeFeatures, facilityState.facilityType);
  } 
  //Restore point location
  if (infoPointerActive) {
    _setupInfoPointerLayers();
  }
}

// ===========================================================================
// TAB SWITCHING
// ===========================================================================
window.switchTab = (name) => {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p =>
    p.classList.toggle('active', p.id === `tab-${name}`));

  exitMode();

  // Clear data belonging to the OTHER features when switching
  if (name !== 'route')    routeFullClear();
  if (name !== 'vrp')      vrpFullClear();
  if (name !== 'isochrone') isoFullClear();
  if (name !== 'facility') facilityFullClear();
};

// ============================================================================
// SECTION A: INFO POINTER FEATURE
// ============================================================================
// When active, clicking any feature on the map shows its raw data properties
// in a floating panel. Useful for inspecting building attributes, road types, etc.

/**
 * Turn the info pointer mode on or off.
 * When ON:  the cursor changes, and clicking the map shows feature data.
 * When OFF: clicking the map places markers as normal.
 */

let infoPointerActive = false;
let _isDragging       = false;
let _infoPanelDragOffset = { x: 0, y: 0 };

function _injectInfoPointerBtn() {
  const topRight = document.querySelector('.maplibregl-ctrl-top-right');
  if (!topRight) return;

  const group = document.createElement('div');
  group.className = 'maplibregl-ctrl maplibregl-ctrl-group';
  group.style.marginTop = '8px';

  const btn = document.createElement('button');
  btn.className = 'info-pointer-btn';
  btn.type      = 'button';
  btn.innerHTML = 'ℹ️';
  btn.title     = 'Toggle Info Pointer';
  btn.onclick   = () => toggleInfoPointer();

  group.appendChild(btn);
  topRight.appendChild(group);
}

window.toggleInfoPointer = function() {
  infoPointerActive = !infoPointerActive;
  const btn    = document.querySelector('.info-pointer-btn');
  const panel  = document.getElementById('feature-info-panel');
  const mapEl  = document.getElementById('map');

  if (infoPointerActive) {
    _setupInfoPointerLayers();
    btn?.classList.add('active');
    mapEl.classList.add('info-mode');
    map.getCanvas().style.cursor = 'crosshair';
    panel.classList.remove('hidden');
    _resetInfoPanelPosition();
    _setupDraggablePanel();
    _updateFeatureInfo('<p class="info-hint">Click on any feature to inspect its properties</p>');
  } else {
    btn?.classList.remove('active');
    mapEl.classList.remove('info-mode');
    map.getCanvas().style.cursor = activeMode !== MODE.NONE ? 'crosshair' : '';
    panel.classList.add('hidden');
    _clearFeatureHighlight();
  }
};

document.addEventListener('DOMContentLoaded', () => {
  const closeBtn = document.getElementById('close-feature-info');
  if (closeBtn) closeBtn.onclick = () => {
    if (infoPointerActive) toggleInfoPointer();
  };
});


/**
 * Create the hidden highlight layers that are used to visually mark
 * the feature the user clicked on.
 * There are three layers (fill, line, circle) to handle all geometry types.
 */
function _setupInfoPointerLayers() {
  if (map.getSource('feature-highlight')) return;
  map.addSource('feature-highlight', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] }
  });
  const layers = [
    { id: 'hl-fill',   type: 'fill',   filter: ['==', ['geometry-type'], 'Polygon'],    paint: { 'fill-color': '#3b82f6', 'fill-opacity': 0.3 } },
    { id: 'hl-line',   type: 'line',   filter: ['any', ['==', ['geometry-type'], 'LineString'], ['==', ['geometry-type'], 'Polygon']], paint: { 'line-color': '#3b82f6', 'line-width': 2.5 } },
    { id: 'hl-circle', type: 'circle', filter: ['==', ['geometry-type'], 'Point'],       paint: { 'circle-radius': 7, 'circle-color': '#3b82f6', 'circle-opacity': 0.7 } },
  ];
  layers.forEach(l => {
    if (!map.getLayer(l.id)) map.addLayer({ ...l, source: 'feature-highlight' });
  });
}

/**
 * Handle a map click while the info pointer is active.
 * Finds whatever feature is at the clicked pixel and displays its properties.
 *
 * @param {Object} e - The MapLibre click event (contains e.point = pixel coords)
 */
function handleInfoPointerClick(e) {
  if (_isDragging) return;
  const features = map.queryRenderedFeatures(e.point);
  if (!features || features.length === 0) {
    _clearFeatureHighlight();
    _updateFeatureInfo('<p class="info-hint">No features at this location</p>');
    return;
  }
  // Filter out our own overlay layers
  const valid = features.filter(f => {
    const id = f.layer.id;
    return !id.startsWith('hl-')       && !id.startsWith('route-src-') &&
           !id.startsWith('vrp-')      && !id.startsWith('iso-') &&
           !id.startsWith('fac-')      && !id.startsWith('search-hl');
  });
  if (!valid.length) {
    _clearFeatureHighlight();
    _updateFeatureInfo('<p class="info-hint">No base-layer features here</p>');
    return;
  }
  const feat = valid[0];
  _highlightFeature(feat);
  _displayFeatureInfo(feat);
}

function _highlightFeature(feature) {
  _clearFeatureHighlight();
  const src = map.getSource('feature-highlight');
  if (src) src.setData({ type: 'FeatureCollection', features: [feature] });
}

/**
 * Update the highlight source to contain just the clicked feature,
 * which causes the blue highlight to appear on the map.
 *
 * @param {Object} feature - A GeoJSON feature from queryRenderedFeatures()
 */

function _clearFeatureHighlight() {
  const src = map.getSource('feature-highlight');
  if (src) src.setData({ type: 'FeatureCollection', features: [] });
}



/**
 * Display feature information in the info panel
 * @param {Object} feature - GeoJSON feature to display
 */
function _displayFeatureInfo(feature) {
  const props    = feature.properties || {};
  const geomType = feature.geometry.type;

  let html = `<div class="feature-layer-info">
    <strong>Layer:</strong> ${feature.layer.id}<br>
    <strong>Source layer:</strong> ${feature.sourceLayer || 'N/A'}<br>
    <strong>Geometry:</strong> ${geomType}
  </div>`;

  const keys = Object.keys(props).sort();
  if (keys.length > 0) {
    keys.forEach(k => {
      const val = props[k];
      if (val === null || val === undefined) return;
      const formatted = typeof val === 'object' ? JSON.stringify(val)
                      : typeof val === 'number'  ? val.toLocaleString()
                      : String(val);
      const label = k === k.toUpperCase()
        ? k.replace(/_/g, ' ')
        : k.replace(/_/g, ' ').replace(/([A-Z])/g, ' $1').replace(/^./, s => s.toUpperCase()).trim();
      html += `<div class="feature-property">
        <span class="property-key">${label}</span>
        <span class="property-value">${formatted}</span>
      </div>`;
    });
  } else {
    html += '<p class="info-hint" style="margin-top:8px">No properties available</p>';
  }

  _updateFeatureInfo(html);
}

/**
 * Update the feature info panel content
 * @param {Object} options - Options object
 * @param {string} options.html - HTML content to display
 */
function _updateFeatureInfo(html) {
  const el = document.getElementById('feature-info-content');
  if (el) el.innerHTML = html;
}


//
function _resetInfoPanelPosition() {
  const panel = document.getElementById('feature-info-panel');
  if (!panel) return;
  panel.style.left      = 'auto';
  panel.style.right     = '1rem';
  panel.style.top       = '1rem';
  panel.style.transform = 'none';
}

// ============================================================================
// SECTION 6: DRAGGABLE PANEL
// ============================================================================
// Allows the user to click and drag the feature info panel to any position on screen.

/**
 * Attach mouse/touch drag handlers to the feature info panel's header bar.
 * The panel can then be repositioned by dragging.
 * 
 * NOTE: We store handler references on the panel itself so we can remove them
 * later if this function is called again, preventing stacked listeners.
 */
function _setupDraggablePanel() {
  const panel  = document.getElementById('feature-info-panel');
  const header = document.querySelector('.feature-info-header');
  if (!panel || !header) return;

  // Remove stacked listeners from previous activations
  if (panel._dragStart) {
    header.removeEventListener('mousedown',  panel._dragStart);
    document.removeEventListener('mousemove', panel._drag);
    document.removeEventListener('mouseup',   panel._dragEnd);
  }

  let dragging = false;
  let ox = 0, oy = 0;
  /**
   * Start dragging
   * @param {Event} e - Mouse or touch event
   */
  const dragStart = e => {
    if (e.target.closest('.close-btn')) return;
    dragging = true;
    _isDragging = false;
    const rect = panel.getBoundingClientRect();
    ox = e.clientX - rect.left;
    oy = e.clientY - rect.top;
    panel.style.right     = 'auto';
    panel.style.left      = rect.left + 'px';
    panel.style.top       = rect.top  + 'px';
    panel.style.transform = 'none';
    e.preventDefault();
  };
  const drag = e => {
    if (!dragging) return;
    _isDragging = true;
    const x = Math.max(0, Math.min(e.clientX - ox, window.innerWidth  - panel.offsetWidth));
    const y = Math.max(0, Math.min(e.clientY - oy, window.innerHeight - panel.offsetHeight));
    panel.style.left = x + 'px';
    panel.style.top  = y + 'px';
  };
  const dragEnd = () => {
    dragging = false;
    setTimeout(() => { _isDragging = false; }, 50);
  };

  header.addEventListener('mousedown', dragStart);
  document.addEventListener('mousemove', drag);
  document.addEventListener('mouseup', dragEnd);

  panel._dragStart = dragStart;
  panel._drag      = drag;
  panel._dragEnd   = dragEnd;
}

// =============================================================================
// SECTION B: SEARCH (Elasticsearch)
// =============================================================================

let currentDataset   = 'spl_units';
let searchHighlights = [];
let searchPopup      = null;

// Toggle the search panel open/closed
window.toggleSearchPanel = function() {
  document.getElementById('search-section').classList.toggle('open');
};

// Search → called by button click or Enter key
window.searchUnits = async function() {
  const q = document.getElementById('searchInput').value.trim();
  const resultsEl = document.getElementById('results');

  if (!q) { resultsEl.innerHTML = ''; resultsEl.classList.remove('has-results'); return; }
 

  resultsEl.innerHTML = '<div class="search-loading">Searching…</div>';
  resultsEl.classList.add('has-results');
  _openSearchPanel();
  _searchClearHighlight();

  const index  = currentDataset === 'spl_units' ? 'building_unit' : 'building_vertical';
  const fields = currentDataset === 'spl_units'
    ? ['properties.UNIT_ID', 'properties.NAME', 'properties.NAME_LONG', 'properties.UnitAddres^3', 'properties.LabelNames']
    : ['properties.UnitVerticalAddress^3', 'properties.ShortAddress^1.8'];

  const esQuery = {
    query: {
        multi_match: { 
            query: q, 
            fields, 
            type: 'best_fields', 
            fuzziness: 'AUTO' 
      } 
    },
    size: 20
  };

  try {
    const res  = await fetch(`${ES_URL}/${index}/_search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(esQuery)
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    console.log(data.hits.hits.length)
    resultsEl.innerHTML = '';

    if (data.hits.hits.length === 0) {
      resultsEl.innerHTML = '<div class="search-no-results">No results found</div>';
      return;
    }

    data.hits.hits.forEach(hit => {
      const feat  = hit._source;
      const props = feat.properties || {};
      const item  = document.createElement('div');
      item.className = 'result-item';
      const isUnit = currentDataset === 'spl_units';
      if (isUnit) {
        item.innerHTML = `<strong>${props.UNIT_ID || 'N/A'}</strong>
          ${props.LabelNames || props.NAME_LONG || 'Unnamed'}
          ${props.UnitAddres ? '· ' + props.UnitAddres : ''}<br>
          <small>Floor: ${props.Base != null ? props.Base.toFixed(2) + 'm' : '—'} · Type: ${props.USE_TYPE || '—'}</small>`;
      } else {
        item.innerHTML = `<strong>${props.UnitVerticalAddress || props.fkFloorGUID || '—'}</strong>
          Floor ${props.FloorID ?? '—'} · ${props.UseType || '—'}<br>
          <small>Bldg height: ${props.BuildingHeight ? props.BuildingHeight.toFixed(1) + 'm' : '—'}</small>`;
      }
      item.onclick = () => _zoomToFeature(feat, item, isUnit);
      resultsEl.appendChild(item);
    });

    // Expand sidebar search body to show results
    _expandSearchBody(true);

  } catch (err) {
    resultsEl.innerHTML = `<div class="search-error">Error: ${err.message}</div>`;
  }
};

function _openSearchPanel() {
  document.getElementById('search-section').classList.add('open');
}

function _expandSearchBody(hasResults) {
  const body = document.getElementById('search-body');
  if (hasResults) {
    // slide panel open further; results list expands internally via CSS
    document.getElementById('results').classList.add('has-results');
    document.getElementById('search-section').style.setProperty('--search-body-max', '420px');
    body.style.maxHeight = '420px';
  } else {
    document.getElementById('results').classList.remove('has-results');
    body.style.maxHeight = '';
  }
}

function _searchClearHighlight() {
  searchHighlights.forEach(id => {
    if (map?.getLayer(id))  map.removeLayer(id);
    if (map?.getSource(id)) map.removeSource(id);
  });
  searchHighlights = [];
  if (searchPopup) { searchPopup.remove(); searchPopup = null; }
  document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
}

async function _zoomToFeature(feature, itemEl, isUnit) {
  document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
  itemEl.classList.add('active');
  _searchClearHighlight();

  if (!feature.geometry) { setStatus('No geometry for this feature.', 'warn'); return; }

  const bounds = new maplibregl.LngLatBounds();
  const flatten = arr => {
    if (typeof arr[0] === 'number') bounds.extend([arr[0], arr[1]]);
    else arr.forEach(flatten);
  };
  flatten(feature.geometry.coordinates);
  if (bounds.isEmpty()) { setStatus('Invalid geometry.', 'warn'); return; }

  const props  = feature.properties || {};
  const safeId = `search-hl-${Date.now()}`;
  searchHighlights.push(safeId);

  const hasHeight = isUnit && props.Base != null;

  const doHighlight = () => {
    if (map.getSource(safeId)) map.removeSource(safeId);
    map.addSource(safeId, {
      type: 'geojson',
      data: { type: 'Feature', geometry: feature.geometry, properties: props }
    });

    if (hasHeight) {
      const base   = props.Base || 0;
      const height = base + (props.HEIGHT || 4.25);
      map.addLayer({ id: safeId, type: 'fill-extrusion', source: safeId,
        paint: { 'fill-extrusion-color': '#ff7c3b', 'fill-extrusion-opacity': 0.9,
                 'fill-extrusion-base': base, 'fill-extrusion-height': height } });
    } else {
      map.addLayer({ id: safeId, type: 'fill', source: safeId,
        paint: { 'fill-color': '#ff7c3b', 'fill-opacity': 0.55 } });
      map.addLayer({ id: safeId + '-line', type: 'line', source: safeId,
        paint: { 'line-color': '#ff7c3b', 'line-width': 2 } });
      searchHighlights.push(safeId + '-line');
    }

    const sidebarW = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w')) + 20;
    map.fitBounds(bounds, {
      padding: { top: 80, bottom: 80, left: sidebarW, right: 80 },
      maxZoom: 19.5, duration: 1400, essential: true, pitch: hasHeight ? 55 : 0
    });

    const ne = bounds.getNorthEast(), sw = bounds.getSouthWest();
    const popupLng = ne.lng + (ne.lng - sw.lng) * 0.2;
    const popupLat = sw.lat + (ne.lat - sw.lat) * 0.65;

    setTimeout(() => {
      let html = `<div style="font-size:12px;line-height:1.6;color:var(--text)">
        <strong style="color:var(--text);font-size:13px">${props.NAME || props.UnitVerticalAddress || props.UNIT_ID || 'Feature'}</strong><br>`;
      if (isUnit) {
        html += `<b>ID:</b> ${props.UNIT_ID || '—'}<br>
                 <b>Type:</b> ${props.USE_TYPE || '—'}<br>
                 <b>Floor:</b> ${props.Base != null ? props.Base.toFixed(2) + 'm' : '—'}`;
      } else {
        html += `<b>Address:</b> ${props.UnitVerticalAddress || '—'}<br>
                 <b>Floor:</b> ${props.FloorID ?? '—'}<br>
                 <b>Usage:</b> ${props.UseType || '—'}`;
      }
      html += '</div>';
      searchPopup = new maplibregl.Popup({ offset: 12, closeButton: true, maxWidth: '260px' })
        .setLngLat([popupLng, popupLat]).setHTML(html).addTo(map);
      searchPopup.on('close', () => { searchPopup = null; });
    }, 900);
  };

  // Switch to BDF style for 3D if needed
  const needsBDF = hasHeight;
  if (needsBDF && currentStyleId !== 'bdf') {
    const bdf = STYLES.find(s => s.id === 'bdf');
    if (bdf) {
      currentStyleId = 'bdf';
      map.setStyle(bdf.url);
      document.querySelectorAll('.layer-switcher button').forEach(b =>
        b.classList.toggle('active', b.textContent === 'BDF'));
      map.once('idle', () => { _restoreActiveLayers(); map.once('idle', doHighlight); });
    } else { doHighlight(); }
  } else {
    doHighlight();
  }
}

// =============================================================================
// SECTION C: ROUTE COMPARISON (Valhalla)
// =============================================================================

let routeProfile = 'auto';

// All drawn route state (survives style switches)
const routeState = {
  points:  [],    // [[lng,lat], [lng,lat]]
  markers: [],    // maplibregl.Marker[]
  drawn:   []     // [{coordinates, color, dash}]
};

const ROUTE_SRC = (i) => `route-src-${i}`;
const ROUTE_LYR = (i) => `route-lyr-${i}`;

window.routeSetProfile = (btn) => {
  _segActivate('#route-profile-seg', btn);
  routeProfile = btn.dataset.profile;
  // Recompute if both points already placed
  if (routeState.points.length === 2) _runRouteComparison();
};

window.routeActivate = () => {
  _routeResetPicks();
  enterMode(MODE.ROUTE);
  _setBtnPicking('route-pick-btn', true);
  setStatus('Click the map to place the Start point.', 'pick');
};

window.routeClear = () => {
  exitMode();
  _routeResetPicks();
};

// Called only from tab-switch — removes map data without touching UI
function routeFullClear() {
  exitMode();
  _routeRemoveLayers();
  routeState.drawn   = [];
  routeState.points  = [];
  routeState.markers.forEach(m => m.remove());
  routeState.markers = [];
  _setBtnPicking('route-pick-btn', false);
  document.getElementById('route-legend').classList.add('hidden');
}

function _routeResetPicks() {
  _routeRemoveLayers();
  routeState.drawn   = [];
  routeState.points  = [];
  routeState.markers.forEach(m => m.remove());
  routeState.markers = [];
  _updateRoutePickStatus();
  _setBtnPicking('route-pick-btn', false);
  document.getElementById('route-legend').classList.add('hidden');
  setStatus('', 'info');
}

function routeHandleClick(lngLat) {
  if (routeState.points.length >= 2) return;
  routeState.points.push(lngLat);

  const isStart = routeState.points.length === 1;
  const el = _makeStopEl(isStart ? 'S' : 'E', isStart ? '#22d98a' : '#ff5c5c');
  const m  = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
      `<b>${isStart ? 'Start' : 'End'}</b><br><code>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</code>`))
    .addTo(map);
  routeState.markers.push(m);
  _updateRoutePickStatus();

  if (routeState.points.length === 2) {
    exitMode();
    _setBtnPicking('route-pick-btn', false);
    _runRouteComparison();
  } else {
    setStatus('Now click the End point.', 'pick');
  }
}

function _updateRoutePickStatus() {
  document.getElementById('route-s-dot').classList.toggle('placed', routeState.points.length >= 1);
  document.getElementById('route-e-dot').classList.toggle('placed', routeState.points.length >= 2);
  document.getElementById('route-s-label').textContent = routeState.points.length >= 1
    ? `Start — ${routeState.points[0][1].toFixed(4)}, ${routeState.points[0][0].toFixed(4)}`
    : 'Start — click map';
  document.getElementById('route-e-label').textContent = routeState.points.length >= 2
    ? `End — ${routeState.points[1][1].toFixed(4)}, ${routeState.points[1][0].toFixed(4)}`
    : 'End — click map';
}

window.routeSwapPoints = () => {
  if (routeState.points.length < 2) return;          // nothing to swap yet
  // Swap coordinates
  [routeState.points[0], routeState.points[1]] = [routeState.points[1], routeState.points[0]];
  // Swap markers on the map
  const [mS, mE] = routeState.markers;
  mS.setLngLat(routeState.points[0]);
  mE.setLngLat(routeState.points[1]);
  _updateRoutePickStatus();
  _runRouteComparison();
};

async function _runRouteComparison() {
  setStatus('Computing 3 route alternatives…', 'loading');
  _routeRemoveLayers();
  routeState.drawn = [];
  document.getElementById('route-legend').classList.add('hidden');

  const [S, E] = routeState.points;
  const base = [
    { lat: S[1], lon: S[0] },
    { lat: E[1], lon: E[0] }
  ];
  const p     = routeProfile;

  // The costing_options key must always be the costing profile name.
  // Normalize: always provide at least an empty object.
  const variants = [
    // 0 — default fastest
    {
      locations: base, costing: p,
      costing_options: { [p]: {} },
      directions_options: { language: 'en-US' }
    },
    // 1 — avoid highways / prefer local
    {
      locations: base, costing: p,
      costing_options: {
        [p]: p === 'auto'      ? { use_highways: 0.05, use_tolls: 0.0 }
           : p === 'bicycle'   ? { use_roads: 0.1}
           :                     {}
      },
      directions_options: { language: 'en-US' }
    },
    // 2 — shortest distance
    {
      locations: base, costing: p,
      costing_options: { [p]: p === 'auto'     ? { shortest: true }
                                : p === 'bicycle'  ? { shortest: true }
                                :                    { shortest: true } },
      directions_options: { language: 'en-US' }
    },
  ];

  const DASHES = [
    [1, 0],     // solid
    [6, 4],     // dashed
    [2, 4],     // dotted
  ];

  const results = await Promise.allSettled(
    variants.map(v => fetchJSON(API.route, v))
  );

  const legendEl = document.getElementById('route-legend');
  legendEl.innerHTML = '';
  let drawn = 0;

  results.forEach((res, i) => {
    if (res.status !== 'fulfilled') {
      console.warn(`Route ${i} failed:`, res.reason);
      return;
    }
    const trip = res.value?.trip;
    if (!trip) return;

    const coords = trip.legs.flatMap(leg => decodePolyline6(leg.shape));
    const color  = ROUTE_COLORS[i];
    const dash   = DASHES[i];

    routeState.drawn.push({ coordinates: coords, color, dash });
    _addRouteLayer(i, coords, color, dash);

    const km   = trip.summary.length.toFixed(1);
    const mins = Math.round(trip.summary.time / 60);

    const item = document.createElement('div');
    item.className = 'legend-item';
    item.innerHTML = `
      <div class="legend-swatch" style="background:${color}"></div>
      <div class="legend-label">${ROUTE_NAMES[i]}</div>
      <div class="legend-stat">${km} km · ${mins} min</div>`;
    legendEl.appendChild(item);
    drawn++;
  });

  if (drawn > 0) {
    legendEl.classList.remove('hidden');
    setStatus(`✅ ${drawn} routes drawn. Legend shows distance and time.`, 'success');
  } else {
    setStatus('No routes returned — are the points within the routable area?', 'error');
  }
}

function _routeRemoveLayers() {
  [0, 1, 2].forEach(i => {
    if (map.getLayer(ROUTE_LYR(i)))  map.removeLayer(ROUTE_LYR(i));
    if (map.getSource(ROUTE_SRC(i))) map.removeSource(ROUTE_SRC(i));
  });
}

function _addRouteLayer(i, coordinates, color, dash) {
  const geojson = { type: 'Feature', geometry: { type: 'LineString', coordinates } };
  if (map.getSource(ROUTE_SRC(i))) {
    // Source already exists (same style session) — update data in place, no flicker
    map.getSource(ROUTE_SRC(i)).setData(geojson);
    return;
  }
  // First draw or after a style switch: add from scratch
  map.addSource(ROUTE_SRC(i), { type: 'geojson', data: geojson });
  map.addLayer({
    id: ROUTE_LYR(i), type: 'line', source: ROUTE_SRC(i),
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color':     color,
      'line-width':     i === 0 ? 5 : 3.5,
      'line-opacity':   i === 0 ? 0.95 : 0.80,
      'line-dasharray': dash
    }
  });
}

// =============================================================================
// SECTION D: VRP / TSP
// =============================================================================

let vrpProfile = 'auto';
let vrpMode    = 'vrp';        // 'vrp' (closed) | 'tsp' (open)
let vrpDebounceTimer = null;
const VRP_DEBOUNCE_MS = 2000;

const vrpState = {
  stops:          [],   // [{id, lngLat, color}]
  markers:        [],   // maplibregl.Marker[]
  drawnSegments:  []    // [{srcId, layerId, coordinates, color}]  — for style restore
};

window.vrpSetProfile = (btn) => {
  _segActivate('#vrp-profile-seg', btn);
  vrpProfile = btn.dataset.profile;
  if (vrpState.stops.length >= 2) _runVRP();
};

window.vrpSetMode = (btn) => {
  document.querySelectorAll('[data-vrpmode]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  vrpMode = btn.dataset.vrpmode;
  if (vrpState.stops.length >= 2) _runVRP();
};

window.vrpActivate = () => {
  enterMode(MODE.VRP);
  _setBtnPicking('vrp-pick-btn', true);
  setStatus('Click the map to add stops. Optimal route draws 2 s after last click.', 'pick');
};

window.vrpClear = () => {
  exitMode();
  clearTimeout(vrpDebounceTimer);
  _vrpRemoveAllSegments();
  vrpState.stops   = [];
  vrpState.markers.forEach(m => m.remove());
  vrpState.markers = [];
  _vrpUpdateCounter();
  _vrpHideTimer();
  _setBtnPicking('vrp-pick-btn', false);
  document.getElementById('vrp-legend').classList.add('hidden');
  setStatus('', 'info');
};

function vrpFullClear() {
  exitMode();
  clearTimeout(vrpDebounceTimer);
  _vrpRemoveAllSegments();
  vrpState.stops   = [];
  vrpState.markers.forEach(m => m.remove());
  vrpState.markers       = [];
  vrpState.drawnSegments = [];
  _vrpUpdateCounter();
  _vrpHideTimer();
  _setBtnPicking('vrp-pick-btn', false);
  document.getElementById('vrp-legend').classList.add('hidden');
}

function vrpHandleClick(lngLat) {
  const idx   = vrpState.stops.length;
  const color = STOP_COLORS[idx % STOP_COLORS.length];
  vrpState.stops.push({ id: idx + 1, lngLat, color });

  const el = _makeStopEl(idx + 1, color);
  const m  = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
      `<b>Stop ${idx + 1}</b><br><code>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</code>`))
    .addTo(map);
  vrpState.markers.push(m);
  _vrpUpdateCounter();

  // Reset debounce
  clearTimeout(vrpDebounceTimer);
  if (vrpState.stops.length >= 2) {
    _vrpShowTimer();
    vrpDebounceTimer = setTimeout(() => {
      _vrpHideTimer();
      _runVRP();
    }, VRP_DEBOUNCE_MS);
  }
}

function _vrpUpdateCounter() {
  const n = vrpState.stops.length;
  document.getElementById('vrp-counter').textContent =
    `${n} stop${n !== 1 ? 's' : ''} added`;
}

function _vrpShowTimer() {
  const bar  = document.getElementById('vrp-timer-bar');
  const fill = document.getElementById('vrp-timer-fill');
  bar.classList.remove('hidden');
  fill.style.transition = 'none';
  fill.style.width = '0%';
  fill.getBoundingClientRect(); // force reflow
  fill.style.transition = `width ${VRP_DEBOUNCE_MS}ms linear`;
  fill.style.width = '100%';
}
function _vrpHideTimer() {
  document.getElementById('vrp-timer-bar').classList.add('hidden');
  document.getElementById('vrp-timer-fill').style.width = '0%';
}

async function _runVRP() {
  if (vrpState.stops.length < 2) return;

  exitMode();
  _setBtnPicking('vrp-pick-btn', false);
  setStatus(`Optimising ${vrpState.stops.length} stops…`, 'loading');

  // ── Build VROOM payload ──────────────────────────────────
  // VRP mode: first stop is depot, vehicle returns to it (closed loop)
  // TSP mode: open tour — no fixed end (vehicle keeps going)
  const isTSP  = vrpMode === 'tsp';
  const depot  = vrpState.stops[0].lngLat;
  const jobStops = isTSP ? vrpState.stops : vrpState.stops.slice(1);

  const vehicle = isTSP
    ? { id: 1, start: depot, profile: vrpProfile }
    : { id: 1, start: depot, end: depot, profile: vrpProfile };

  const jobs = jobStops.map(s => ({
    id:       s.id,
    location: s.lngLat,
    service:  30
  }));

  try {
    const result = await fetchJSON(API.optimize, { vehicles: [vehicle], jobs });
    console.log('[vrp] result', result);

    const route = result.vroom?.routes?.[0];
    if (!route) {
      setStatus('No route returned — ensure stops are within routable area.', 'warn');
      return;
    }

    // ── Extract ordered stop sequence from VROOM steps ─────
    // VROOM returns steps with type "job" in optimal visit order
    const jobSteps = route.steps?.filter(s => s.type === 'job') ?? [];

    // Map job id → stop color
    // Build a lookup map by stop id (id is set by us above as s.id = idx+1).
    const idToStop = {};
    vrpState.stops.forEach(s => { idToStop[s.id] = s; });

    /// Build ordered visit list: [depot, stop_a, stop_b, …, (depot if VRP)]
    let orderedLngLats = [depot];
    jobSteps.forEach(step => {
      const stop = idToStop[step.id];
      if (stop) orderedLngLats.push(stop.lngLat);
    });
    if (!isTSP) orderedLngLats.push(depot); // return to depot

    // ── Fetch individual segments from Valhalla ────────────
    // Each segment is colored by the color of the ORIGIN stop
    _vrpRemoveAllSegments();
    vrpState.drawnSegments = [];

    setStatus('Drawing route segments…', 'loading');

    const segmentRequests = [];
    for (let i = 0; i < orderedLngLats.length - 1; i++) {
      const from  = orderedLngLats[i];
      const to    = orderedLngLats[i + 1];

      // Origin stop color: for the leg from depot, use depot/first stop color
      let color;
      if (i === 0) {
        // depot → first job: color of depot (first stop in VRP) or first stop in TSP
        color = vrpState.stops[0].color;
      } else {
        // Find the stop at position `from`
        const fromStop = vrpState.stops.find(s =>
          Math.abs(s.lngLat[0] - from[0]) < 1e-8 &&
          Math.abs(s.lngLat[1] - from[1]) < 1e-8
        );
        color = fromStop ? fromStop.color : STOP_COLORS[i % STOP_COLORS.length];
      }

      segmentRequests.push({ from, to, color, index: i });
    }

    const segResults = await Promise.allSettled(
      segmentRequests.map(seg =>
        fetchJSON(API.route, {
          locations: [
            { lat: seg.from[1], lon: seg.from[0] },
            { lat: seg.to[1],   lon: seg.to[0]   }
          ],
          costing:   vrpProfile,
          directions_options: { language: 'en-US' }
        }).then(r => ({ ...seg, trip: r.trip }))
      )
    );

    let drawnSegs = 0;
    segResults.forEach((res) => {
      if (res.status !== 'fulfilled') return;
      const { index, color, trip } = res.value;
      if (!trip) return;

      const coords  = trip.legs.flatMap(leg => decodePolyline6(leg.shape));
      const srcId   = `vrp-seg-src-${index}`;
      const layerId = `vrp-seg-lyr-${index}`;

      vrpState.drawnSegments.push({ srcId, layerId, coordinates: coords, color });
      _addVrpSegmentLayer(srcId, layerId, coords, color);
      drawnSegs++;
    });

    // ── Build legend ───────────────────────────────────────
    const legendEl = document.getElementById('vrp-legend');
    legendEl.innerHTML = '';

    // Summary row
    const km   = (route.distance / 1000).toFixed(1);
    const mins = Math.round(route.duration / 60);
    const summaryRow = document.createElement('div');
    summaryRow.className = 'legend-item';
    summaryRow.innerHTML = `
      <div class="legend-label" 
        style="font-weight:600">${isTSP ? "TSP" : "VRP"} · ${vrpState.stops.length} stops
      </div>
      <div class="legend-stat">${km} km · ${mins} min</div>`;
    legendEl.appendChild(summaryRow);

    // Visit order row
    const orderRow = document.createElement('div');
    orderRow.className = 'legend-item';
    orderRow.style.flexWrap = 'wrap';
    orderRow.style.gap = '3px';
    const orderLbl = document.createElement('div');
    orderLbl.style.cssText = 'font-size:0.6rem;color:var(--text-faint);width:100%;margin-bottom:2px';
    orderLbl.textContent = 'Visit order →';
    orderRow.appendChild(orderLbl);

    // Depot dot
    const depotDot = _makeLegendDot(vrpState.stops[0].color, vrpState.stops[0].id);
    orderRow.appendChild(depotDot);

    jobSteps.forEach(step => {
      const stop = idToStop[step.id];
      if (!stop) return;
      const dot = _makeLegendDot(stop.color, stop.id);
      orderRow.appendChild(dot);
    });

    if (!isTSP) {
      // Return arrow + depot
      const arr = document.createElement('span');
      arr.style.cssText = 'font-size:10px;color:var(--text-faint);align-self:center';
      arr.textContent = '↩';
      orderRow.appendChild(arr);
    }

    legendEl.appendChild(orderRow);
    legendEl.classList.remove('hidden');
    setStatus(`✅ ${drawnSegs} segments drawn — ${km} km · ${mins} min`, 'success');

  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`, 'error');
  }
}

function _makeLegendDot(color, label) {
  const dot = document.createElement('span');
  dot.style.cssText = `
    display:inline-flex;
    align-items:center;
    justify-content:center;
    width:16px;
    height:16px;
    border-radius:50%;
    background:${color};
    font-size:8px;color:#fff;
    font-weight:700;
    font-family:var(--font-mono)`;
  dot.textContent = label;
  return dot;
}

function _vrpRemoveAllSegments() {
  vrpState.drawnSegments.forEach(({ srcId, layerId }) => {
    if (map.getLayer(layerId)) map.removeLayer(layerId);
    if (map.getSource(srcId))  map.removeSource(srcId);
  });
  vrpState.drawnSegments = [];
}

function _addVrpSegmentLayer(srcId, layerId, coordinates, color) {
  const geojson = { type: 'Feature', geometry: { type: 'LineString', coordinates } };
  if (map.getSource(srcId)) {
    // Source exists — update data in place to avoid flicker
    map.getSource(srcId).setData(geojson);
    return;
  }
  // First draw or after a style switch: add from scratch
  map.addSource(srcId, { type: 'geojson', data: geojson });
  map.addLayer({
    id: layerId, type: 'line', source: srcId,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': color, 'line-width': 4.5, 'line-opacity': 0.92 }
  });
}

// =============================================================================
// SECTION E: ISOCHRONE (Valhalla)
// =============================================================================

let isoProfile  = 'auto';
let isoInterval = 10;

const isoState = {
  geojson:      null,
  originMarker: null,
  originLngLat: null   // stored so parameter changes can re-run without a new click
};

const ISO_SRC    = 'iso-src';
const ISO_FILL   = 'iso-fill';
const ISO_BORDER = 'iso-border';

window.isoSetProfile = (btn) => {
  _segActivate('#iso-profile-seg', btn);
  isoProfile = btn.dataset.profile;
  if (isoState.originLngLat) isoHandleClick(isoState.originLngLat);
};

window.isoSetInterval = (btn) => {
  document.querySelectorAll('[data-interval]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  isoInterval = parseInt(btn.dataset.interval, 10);
  document.getElementById('iso-interval-val').textContent = `${isoInterval} min`;
  if (isoState.originLngLat) isoHandleClick(isoState.originLngLat);
};

window.isoActivate = () => {
  enterMode(MODE.ISO);
  _setBtnPicking('iso-pick-btn', true);
  setStatus('Click a location on the map to compute the isochrone.', 'pick');
};

window.isoClear = () => {
  exitMode();
  isoFullClear();
  setStatus('', 'info');
};

function isoFullClear() {
  exitMode();
  _isoRemoveLayers();
  isoState.geojson = null;
  isoState.originLngLat = null;
  if (isoState.originMarker) { isoState.originMarker.remove(); isoState.originMarker = null; }
  _setBtnPicking('iso-pick-btn', false);
  document.getElementById('iso-legend').classList.add('hidden');
}

async function isoHandleClick(lngLat) {
  exitMode();
  _setBtnPicking('iso-pick-btn', false);

  // Place / move origin marker only when lngLat differs from stored origin
  const isNewOrigin = !isoState.originLngLat
    || isoState.originLngLat[0] !== lngLat[0]
    || isoState.originLngLat[1] !== lngLat[1];

  if (isNewOrigin) {
    if (isoState.originMarker) isoState.originMarker.remove();
    const el = _makeStopEl('◉', '#3b9eff');
    el.style.fontSize = '11px';
    isoState.originMarker = new maplibregl.Marker({ element: el }).setLngLat(lngLat).addTo(map);
    isoState.originLngLat = lngLat;
  }

  const levels   = parseInt(document.getElementById('iso-levels').value, 10);

  // Build contours: Valhalla expects them sorted by ascending time
  // Outermost (largest) time first gives better polygon fill in some versions,
  // but Valhalla docs say ascending is fine — use ascending.
  const contours = [];
  for (let i = 1; i <= levels; i++) {
    const t     = i * isoInterval;
    // Color index 0 = innermost (smallest time)
    const color = ISO_COLORS[i - 1].replace('#', '');
    contours.push({ time: t, color });
  }

  setStatus(`Computing ${levels}-level isochrone (${isoInterval} min intervals)…`, 'loading');

  const payload = {
    locations: [{ lat: lngLat[1], lon: lngLat[0] }],
    costing:   isoProfile,
    contours,
    polygons:  true,
    denoise:   0.5,       // smooth noisy edges
    generalize: 50        // metres — simplify geometry for faster rendering
  };

  try {
    const geojson = await fetchJSON(API.isochrone, payload);
    isoState.geojson = geojson;
    _isoRemoveLayers();
    _renderIso(geojson);

    // Build legend (innermost first)
    const legendEl = document.getElementById('iso-legend');
    legendEl.innerHTML = '';
    for (let i = 1; i <= levels; i++) {
      const t     = i * isoInterval;
      const color = ISO_COLORS[i - 1];
      const item  = document.createElement('div');
      item.className = 'legend-item';
      item.innerHTML = `
        <div class="legend-swatch" 
          style="background:${color};height:3px;border-radius:1px">
        </div>
        <div class="legend-label">${t} min</div>`;
      legendEl.appendChild(item);
    }
    legendEl.classList.remove('hidden');
    setStatus(`✅ ${levels} isochrone ring${levels > 1 ? 's' : ''} — every ${isoInterval} min`, 'success');
  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`, 'error');
  }
}

function _renderIso(geojson) {
  if (map.getSource(ISO_SRC)) {
    // Update data in place — no layer flicker
    map.getSource(ISO_SRC).setData(geojson);
    return;
  }
  // First draw or after a style switch: add from scratch

  map.addSource(ISO_SRC, { type: 'geojson', data: geojson });
  map.addLayer({
    id: ISO_FILL, type: 'fill', source: ISO_SRC,
    paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.13 }
  });
  map.addLayer({
    id: ISO_BORDER, type: 'line', source: ISO_SRC,
    paint: { 'line-color': ['get', 'color'], 'line-width': 2, 'line-opacity': 0.88 }
  });
}

function _isoRemoveLayers() {
  if (map.getLayer(ISO_BORDER)) map.removeLayer(ISO_BORDER);
  if (map.getLayer(ISO_FILL))   map.removeLayer(ISO_FILL);
  if (map.getSource(ISO_SRC))   map.removeSource(ISO_SRC);
}


// =============================================================================
// SECTION F: NEAREST FACILITY  (PostGIS spatial query + Valhalla routing)
// =============================================================================
//
// FLASH-FREE RENDERING  ← key fix over the Overpass version
// ──────────────────────────────────────────────────────────
// The old code called _removeFacilityRouteLayers() then re-added the source
// and layer on every search, causing a visible flash: for one frame the map
// had no route lines, then they popped back in.
//
// Fix: _renderFacilityRoutes() now calls source.setData() when the source
// already exists (same session), never removing and re-adding the layer.
// The layer is only torn down fully on a style switch or explicit clear.
//
// STALE-REQUEST GUARD
// ───────────────────
// facilitySearchId increments on every click.  Each in-flight fetch captures
// its own copy.  If the user clicks elsewhere before the response arrives,
// the IDs won't match and the stale result is silently discarded.
// =============================================================================

let facilityType     = 'hospital';
let facilitySearchId = 0;          // stale-request guard counter

const facilityState = {
  originMarker:  null,
  originLngLat:  null,
  facMarkers:    [],        // maplibregl.Marker[]
  routeFeatures: [],        // GeoJSON features for all facility routes
  facilityType:  'hospital'
};

const FAC_ROUTE_SRC = 'fac-routes';
const FAC_ROUTE_LYR = 'fac-routes-lyr';
const FAC_ROUTE_GLOW = 'fac-routes-glow';   // soft halo beneath the line

window.facilitySetType = btn => {
  document.querySelectorAll('#facility-type-seg .seg').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  facilityType = btn.dataset.ftype;
};

window.facilityActivate = () => {
  enterMode(MODE.FACILITY);
  _setBtnPicking('fac-pick-btn', true);
  setStatus('Click the map to search for nearby facilities.', 'pick');
};

window.facilityClear = () => {
  exitMode();
  facilityFullClear();
  setStatus('', 'info');
};

function facilityFullClear() {
  exitMode();
  facilitySearchId++;   // invalidate any in-flight request
  if (facilityState.originMarker) {
    facilityState.originMarker.remove();
    facilityState.originMarker = null;
  }
  facilityState.facMarkers.forEach(m => {
    if (m._popup?.isOpen()) m._popup.remove();
    m.remove();
  });
  facilityState.facMarkers    = [];
  facilityState.originLngLat  = null;
  facilityState.routeFeatures = [];
  facilityState.facilityType  = 'hospital';
  _removeFacilityRouteLayers();
  document.getElementById('fac-legend').classList.add('hidden');
  document.getElementById('fac-legend').innerHTML = '';
  document.getElementById('fac-status').textContent = '';
  _setBtnPicking('fac-pick-btn', false);
}

function _removeFacilityRouteLayers() {
  if (map.getLayer(FAC_ROUTE_GLOW)) map.removeLayer(FAC_ROUTE_GLOW);
  if (map.getLayer(FAC_ROUTE_LYR))  map.removeLayer(FAC_ROUTE_LYR);
  if (map.getSource(FAC_ROUTE_SRC)) map.removeSource(FAC_ROUTE_SRC);
}

function facilityHandleClick(lngLat) {
  // Replace origin pin; clear previous markers and legend
  facilityState.facMarkers.forEach(m => {
    if (m._popup?.isOpen()) m._popup.remove();
    m.remove();
  });
  facilityState.facMarkers = [];
  document.getElementById('fac-legend').classList.add('hidden');
  document.getElementById('fac-legend').innerHTML = '';
  document.getElementById('fac-status').textContent = '';

  // DO NOT remove route layers here — we will update them in-place via
  // setData() to avoid the flash.  They get torn down only on full clear
  // or a style switch (where _restoreActiveLayers rebuilds from state).

  if (facilityState.originMarker) facilityState.originMarker.remove();
  const pinEl = document.createElement('div');
  pinEl.style.cssText = 'font-size:22px;line-height:1;cursor:default;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.6))';
  pinEl.textContent   = '📍';
  facilityState.originMarker = new maplibregl.Marker({ element: pinEl })
    .setLngLat(lngLat).addTo(map);
  facilityState.originLngLat = lngLat;
  facilityState.facilityType = facilityType;

  exitMode();
  _setBtnPicking('fac-pick-btn', false);
  _runFacilitySearch(lngLat, facilityType);
}

async function _runFacilitySearch(lngLat, ftype) {
  // Capture a unique ID for THIS search; discard response if superseded
  const myId    = ++facilitySearchId;
  const cfg     = FACILITY_CONFIG[ftype] || FACILITY_CONFIG['hospital'];
  const count   = parseInt(document.getElementById('fac-count').value, 10)  || 5;
  const radiusKm = parseInt(document.getElementById('fac-radius').value, 10) || 5;

  setStatus(`Searching POI for ${cfg.label}s within ${radiusKm} km…`, 'loading');

  // ------------------------------------------------------------------
  // Call /nearest_facility  (PostGIS + Valhalla matrix + route geometry)
  // ------------------------------------------------------------------
  let data;
  try {
    const params = new URLSearchParams({
      lon:             lngLat[0],
      lat:             lngLat[1],
      type:            ftype,
      limit:           count,
      max_distance_km: radiusKm,
      routes:          'true',
    });

    const res = await fetch(`${API.nearestFacility}?${params}`, {
      signal: AbortSignal.timeout(120_000)
    });

    if (myId !== facilitySearchId) return;   // superseded — discard

    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { const j = await res.json(); detail = j.detail || detail; } catch {}
      throw new Error(detail);
    }

    console.log('%c Facility API Data:', 'color: #22d98a; font-weight: bold;', res);
    console.table(res.facilities);   // Nice table view of facilities

    data = await res.json();

    console.log('%c Facility API Data:', 'color: #22d98a; font-weight: bold;', data);
    console.table(data.facilities);   // Nice table view of facilities
    // =======================================================
  } catch (err) {
    if (myId !== facilitySearchId) return;
    setStatus(`Error: ${err.message}`, 'error');
    return;
  }

  if (myId !== facilitySearchId) return;   // superseded after await

  if (!data.facilities || data.facilities.length === 0) {
    setStatus(`No ${cfg.label} found within ${radiusKm} km.`, 'warn');
    _renderFacilityRoutes([], ftype);        // clear route lines without flash
    return;
  }

  const facilities = data.facilities;

  // ------------------------------------------------------------------
  // Collect all route segments into a flat GeoJSON feature array.
  // Each feature already carries facility_rank from the API so the
  // line-width expression can vary by proximity.
  // ------------------------------------------------------------------
  const allFeatures = [];
  // facilities.forEach((f, index) => {
  //   (f.route?.features ?? []).forEach(feat => {
  //     allFeatures.push({
  //       ...feat,
  //       properties: {
  //         ...feat.properties,
  //         facility_rank: index + 1,
  //       },
  //     });
  //   });
  // });

  // ---------New code---------
  facilities.forEach((f, index) => {
    (f.route?.features ?? []).forEach(feat => {
      const rawCoords = feat.geometry?.coordinates;

      // Decode if string (encoded polyline), use as-is if already an array
      let coords;
      if (typeof rawCoords === 'string' && rawCoords.length > 0) {
        coords = decodePolyline6(rawCoords);
      } else if (Array.isArray(rawCoords) && rawCoords.length >= 2) {
        coords = rawCoords;
      } else {
        return;   // skip malformed feature
      }

      if (coords.length < 2) return;   // degenerate segment — skip

      allFeatures.push({
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: coords },
        properties: {
          ...feat.properties,
          facility_rank: index + 1,
        },
      });
    });
  });

  // Store for style-switch restore
  facilityState.routeFeatures = allFeatures;

  // ------------------------------------------------------------------
  // Draw route lines — setData() path avoids the flash
  // ------------------------------------------------------------------
  _renderFacilityRoutes(allFeatures, ftype);

  // ------------------------------------------------------------------
  // Place facility markers
  // ------------------------------------------------------------------
  facilityState.facMarkers.forEach(m => { if (m._popup?.isOpen()) m._popup.remove(); m.remove(); });
  facilityState.facMarkers = [];

  facilities.forEach((f, index) => {
    const icon  = cfg.icon;
    const color = cfg.color;
    const rank  = index + 1;

    const el = document.createElement('div');
    el.className = 'fac-marker';
    el.style.background = color;
    el.innerHTML = icon;

    const badge = document.createElement('div');
    badge.className = 'fac-badge';
    badge.style.borderColor = color;
    badge.style.color       = color;
    badge.textContent       = rank;
    el.appendChild(badge);

    const popup = new maplibregl.Popup({
      offset: 20, closeButton: true, maxWidth: '240px'
    }).setHTML(`
      <div style="font-size:12px;line-height:1.6;color:var(--text)">
        <div style="font-size:20px;margin-bottom:4px">${icon}</div>
        <strong style="color:var(--text)">${f.name}</strong>
        ${f.address ? `<br><span style="color:var(--text-dim)">${f.address}</span>` : ''}
        <br><span style="color:#22d98a">⏱ ${f.travel_minutes} min</span>
        &nbsp;·&nbsp;<span style="color:var(--text-dim)">${f.crow_distance_km} km</span><br>
        <small style="color:var(--text-faint)">Rank #${rank}</small>
      </div>
    `);

    const marker = new maplibregl.Marker({
      element: el, anchor: 'center', offset: [0, -4]
    })
      .setLngLat([parseFloat(f.facility_lon), parseFloat(f.facility_lat)])
      .setPopup(popup)
      .addTo(map);

    marker._popup = popup;

    let closeTimeout;
    el.addEventListener('mouseenter', () => {
      clearTimeout(closeTimeout);
      if (!popup.isOpen()) marker.togglePopup();
    });
    el.addEventListener('mouseleave', () => {
      closeTimeout = setTimeout(() => { if (popup.isOpen()) marker.togglePopup(); }, 200);
    });

    facilityState.facMarkers.push(marker);
  });

  // ------------------------------------------------------------------
  // Build sidebar legend
  // ------------------------------------------------------------------
  const legendEl = document.getElementById('fac-legend');
  legendEl.innerHTML = '';
  facilities.forEach((f, i) => {
    const item = document.createElement('div');
    item.className = 'fac-item';
    item.onclick   = () => facilityState.facMarkers[i]?.togglePopup();
    item.innerHTML = `
      <div class="fac-rank" style="background:${cfg.color}">${i + 1}</div>
      <div class="fac-info">
        <div class="fac-name">${f.name}</div>
        <div class="fac-meta">⏱ ${f.travel_minutes} min · ${f.crow_distance_km} km straight-line</div>
      </div>`;
    legendEl.appendChild(item);
  });
  legendEl.classList.remove('hidden');

  // ------------------------------------------------------------------
  // Zoom to fit all results + origin
  // ------------------------------------------------------------------
  const bounds = new maplibregl.LngLatBounds();
  bounds.extend(lngLat);
  facilities.forEach(f => bounds.extend([parseFloat(f.facility_lon), parseFloat(f.facility_lat)]));
  const sidebarW = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w')) + 20;
  map.fitBounds(bounds, {
    padding: { top: 80, bottom: 80, left: sidebarW, right: 80 },
    maxZoom: 14, duration: 1200
  });

  const closest = facilities[0];
  setStatus(`✅ ${facilities.length} ${cfg.label}(s) found — nearest is ${closest.name} (${closest.travel_minutes} min)`, 'success');
  document.getElementById('fac-status').textContent =
    `${facilities.length} result${facilities.length > 1 ? 's' : ''} — hover markers for details`;
}
// ------------------------------------------------------------------
// Render route lines

//
// If the source does NOT yet exist (first draw, or after a style switch
// cleared it), we create the source + both layers fresh.
// ------------------------------------------------------------------
function _renderFacilityRoutes(features, ftype) {
  const cfg   = FACILITY_CONFIG[ftype] || FACILITY_CONFIG['hospital'];
  const color = cfg.color;
  const geojsonData = { type: 'FeatureCollection', features };

  if (map.getSource(FAC_ROUTE_SRC)) {
    // Source already exists — update data AND repaint both layers so a
    // facility-type change (e.g. hospital → police) recolors the lines.
    map.getSource(FAC_ROUTE_SRC).setData(geojsonData);
    if (map.getLayer(FAC_ROUTE_GLOW)) map.setPaintProperty(FAC_ROUTE_GLOW, 'line-color', color);
    if (map.getLayer(FAC_ROUTE_LYR))  map.setPaintProperty(FAC_ROUTE_LYR,  'line-color', color);
    return;
  }

  // First draw or post-style-switch: create source + layers from scratch
  map.addSource(FAC_ROUTE_SRC, { type: 'geojson', data: geojsonData });

  // Glow layer — wide, semi-transparent halo for visual depth
  map.addLayer({
    id:     FAC_ROUTE_GLOW,
    type:   'line',
    source: FAC_ROUTE_SRC,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color':   color,
      'line-width':   ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 14, 5, 8],
      'line-opacity': 0.15,
      'line-blur':    6,
    },
  });

  // Crisp route line — narrower for lower-ranked (farther) facilities
  map.addLayer({
    id:     FAC_ROUTE_LYR,
    type:   'line',
    source: FAC_ROUTE_SRC,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color':   color,
      'line-width':   ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 5.5, 5, 2.5],
      'line-opacity': 0.85,
    },
  });
}


// ===========================================================================
// LAYER SWITCHER CONTROL
// ===========================================================================
class LayerSwitcherControl {
  onAdd(map) {
    this._map = map;
    this._container = document.createElement('div');
    this._container.className = 'maplibregl-ctrl maplibregl-ctrl-group layer-switcher';
    STYLES.forEach((s, i) => {
      const btn = document.createElement('button');
      btn.type = 'button'; btn.title = s.name; btn.textContent = s.name;
      if (i === 0) btn.classList.add('active');
      btn.addEventListener('click', () => {
        this._container.querySelectorAll('button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentStyleId = s.id;
        map.setStyle(s.url);
        map.once('idle', () => {
          map.easeTo({ center: currentCenter, zoom: s.zoom ?? currentZoom, pitch: s.pitch ?? currentPitch, bearing: s.bearing ?? currentBearing, duration: 800 });
          map.once('idle', _restoreActiveLayers);
        });
      });
      this._container.appendChild(btn);
    });
    return this._container;
  }
  onRemove() { this._container.parentNode?.removeChild(this._container); }
}

// ===========================================================================
// SHARED UTILITIES
// ===========================================================================

// Polyline decoder precision 6 → [[lng,lat],…]
function decodePolyline6(encoded) {
  const coords = [];
  let index = 0, lat = 0, lng = 0;
  const len = encoded.length;
  while (index < len) {
    let b, shift = 0, result = 0;
    do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);
    shift = 0; result = 0;
    do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
    lng += (result & 1) ? ~(result >> 1) : (result >> 1);
    coords.push([lng / 1e6, lat / 1e6]);
  }
  return coords;
}

// POST JSON with timeout → returns parsed response
async function fetchJSON(url, body) {
  const ctrl = new AbortController();
  const tid  = setTimeout(() => ctrl.abort(), TIMEOUT);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body:   JSON.stringify(body),
      signal: ctrl.signal
    });
    clearTimeout(tid);
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { const j = await res.json(); if (j.detail) detail = JSON.stringify(j.detail); } catch {}
      throw new Error(detail);
    }
    return res.json();
  } catch (err) {
    clearTimeout(tid);
    if (err.name === 'AbortError') throw new Error('Request timed out — please try again.');
    throw err;
  }
}

// Status bar
function setStatus(msg, type = 'info') {
  const el = document.getElementById('status-bar');
  if (!el) return;
  el.textContent = msg;
  el.className   = `status-${type}`;
  if (type === 'success' || type === 'info') {
    setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 7000);
  }
}

// Create a circular stop marker element
function _makeStopEl(label, color) {
  const el = document.createElement('div');
  el.className   = 'stop-marker';
  el.style.background = color;
  el.textContent = label;
  return el;
}

// Activate a single seg button within a group selector
function _segActivate(groupSelector, btn) {
  document.querySelectorAll(`${groupSelector} .seg`).forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// Toggle the "picking" pulsing state on an action button
function _setBtnPicking(id, on) {
  const btn = document.getElementById(id);
  if (btn) btn.classList.toggle('picking', on);
}

// ===========================================================================
// BOOT
// ===========================================================================
window.addEventListener('load', initMap);
// Expose internals needed by inline oninput handlers in index.html
window.isoState       = isoState;
window.isoHandleClick = isoHandleClick;