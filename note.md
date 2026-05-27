# Self-Hosted Map Service with PostGIS, Martin, and Valhalla, VROOM for route optimisation and fleet management

This guide outlines the steps to set up a Map service for for route optimisation and fleet management on localhost environment using a stack of open-source tools for serving static and dynamic vector tiles, and route optimiser

## Phase 1: Foundation - Database and Environment Setup

We will use Docker Compose to manage all services (PostGIS, Martin, Valhalla, Vroom).
### Step 1: Install Docker and Docker Compose

Ensure you have Docker and Docker Compose (or Docker Desktop) installed on your system.
We have to use WSL2 to install docker 
```bash
# 1. Add Docker repo
sudo apt update
sudo apt install ca-certificates curl gnupg lsb-release

sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg

echo  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg]  https://download.docker.com/linux/ubuntu  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 2. Install Docker Engine + Compose plugin
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 3. Enable non-sudo usage
sudo usermod -aG docker $USER
exec sudo su -l $USER
```
### Step 2: Create script to set up system

- Create a ```docker-compose.yml``` file for your entire stack.
```yaml
# version: '3.9'

services:
  postgis:
    image: postgis/postgis:16-3.5
    container_name: postgis_db
    restart: unless-stopped
    env_file:
      - .env
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    ports:
      - "5432:5432"
    volumes:
      - vsol:/var/lib/postgresql/data
      - ./data:/data
    shm_size: 1g
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 10

  martin:
    image: ghcr.io/maplibre/martin:1.5.0
    container_name: martin_server
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "3000:3000"
    volumes:
      - ./config/martin-config.yml:/config.yml:ro
      - ./data/mbtiles:/mbtiles:ro
      - ./data/pmtiles:/pmtiles:ro
      - ./styles:/styles:ro
    command: --config /config.yml
    depends_on:
      postgis:
        condition: service_healthy

  valhalla:
    image: ghcr.io/valhalla/valhalla-scripted:latest   # flexible, well-maintained image
    container_name: valhalla
    restart: unless-stopped
    ports:
      - "8002:8002"          # internal; we will proxy via Nginx later
    environment:
      - tile_urls=    # change if you want a smaller extract
      - use_tiles_ignore_pbf=False      # Once built, don't rebuild from PBF on restart
      - force_rebuild=False          # set True only when you want to force a full rebuild
      - server_threads=4     # adjust to your CPU cores (4–8 is good for HCMC/Vietnam)
      - build_elevation=False        # set True only if you need elevation data
      - build_admins=False
      - build_time_zones=False
      - build_tar=True               # creates fast .tar index
    volumes:
      - ./data/pbf:/custom_files
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/status"]
      interval: 30s
      timeout: 10s
      retries: 10
      start_period: 300s   # first build can take a while for Vietnam

  vroom:
    image: ghcr.io/vroom-project/vroom-docker:latest
    container_name: vroom
    restart: unless-stopped
    ports:
      - "3003:3000"   # internal only (proxied via Nginx)
    volumes:
      - ./config/vroom-config.yml:/conf/config.yml:ro
    environment:
      - VROOM_ROUTER=valhalla
    depends_on:
      valhalla:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  routing-api:
    build:
      context: ./routing-api
      dockerfile: Dockerfile
    container_name: routing-api
    restart: unless-stopped
    ports:
      - "5000:5000"
    depends_on:
      postgis:
        condition: service_healthy
      valhalla:
        condition: service_healthy
      vroom:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  pgadmin:
    image: dpage/pgadmin4:9.14.0
    container_name: pgadmin4
    restart: always
    env_file: 
      - .env    
    environment:
      - PGADMIN_DEFAULT_EMAIL=${PGADMIN_EMAIL:-admin@admin.com}
      - PGADMIN_DEFAULT_PASSWORD=${PGADMIN_PASSWORD:-admin}
    ports:
      - "5050:80"
    depends_on:
      - postgis

  nginx:
    image: nginx:alpine
    container_name: nginx
    restart: always
    ports:
      - "3001:80"
    volumes:
      - ./frontend:/usr/share/nginx/html
      - ./data/pmtiles:/usr/share/nginx/html/pmtiles:ro
      - ./data/sprites:/usr/share/nginx/html/sprites:ro
      - ./data/fonts:/usr/share/nginx/html/fonts:ro
      - ./styles:/usr/share/nginx/html/styles:ro
      - ./config/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      - martin
      - routing-api
      - valhalla
      - vroom

volumes:
  vsol:
 
```
- Create vroom-config.yml file
```yml
cliArgs:
  geometry: true # retrieve geometry (-g) return encoded polyline geometry
  planmode: false # run vroom in plan mode (-c) if set to true
  threads: 4 # number of threads to use (-t)
  explore: 5 # exploration level to use (0..5) (-x)
  limit: '5mb' # max request size
  logdir: '..' # the path for the logs relative to ./src
  logsize: '100M' # max log file size for rotation
  maxlocations: 1000 # max number of jobs/shipments locations
  maxvehicles: 200 # max number of vehicles
  override: ['c', 'g', 'l', 't', 'x'] # allow cli options override (c, g, l, t, and x)
  path: '' # VROOM path (if not in $PATH)
  port: 3000 # expressjs port
  router: 'valhalla' # routing backend (osrm, libosrm, ors, or valhalla)
  timeout: 300000 # milli-seconds
  baseurl: '/' # base url for api

routingServers:
  valhalla:
    auto:
      host: 'valhalla'
      port: '8002'
    bicycle:
      host: 'valhalla'
      port: '8002'
    pedestrian:
      host: 'valhalla'
      port: '8002'
    motorcycle:
      host: 'valhalla'
      port: '8002'
    motor_scooter:
      host: 'valhalla'
      port: '8002'
    taxi:
      host: 'valhalla'
      port: '8002'
    hov:
      host: 'valhalla'
      port: '8002'
    truck:
      host: 'valhalla'
      port: '8002'
    bus:
      host: 'valhalla'
      port: '8002'
```
- Create martin-config.yml
```yml
keep_alive: 75
listen_addresses: '0.0.0.0:3000'
base_path: 
worker_processes: 8
cache_size_mb: 1024
preferred_encoding: gzip
web_ui: enable-for-all
observability:
  metrics:
    add_labels: {}

cors: 
  origin: 
    - "*"
  max_age: 3600

postgres:
  connection_string: "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgis:5432/${POSTGRES_DB}?sslmode=disable"
  default_srid: 4326
  auto_publish:
    tables:
      from_schemas:
        - topology
      source_id_format: '{schema}.{table}'
      id_columns: id
      clip_geom: true
      buffer: 64
      extent: 4096
      
pmtiles:
  directory_cache_size_mb: 128
  allow_http: true
  paths:
  - /pmtiles

mbtiles:
  paths:
  # - /mbtiles
  sources:
    # # named source matching source name to a single file


sprites:
  cache_size_mb: 64

  paths:
  # - /sprites
  sources:

fonts:
  cache_size_mb: 64
  paths:
  # - /fonts

styles:
  paths:
  - /styles

tilejson_url_version_param: null 
```
- Create nginx.conf:
```yml
server {
    listen 80;
    server_name localhost;

    root /usr/share/nginx/html;
    index index.html;

    # 1. Static assets (fix regex)
    location ~* \.(css|js|png|jpg|jpeg|gif|ico|woff2?|ttf|svg|eot|otf)$ {
        try_files $uri =404;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # 2. PMTiles files - important for range requests
    location ~* \.pmtiles$ {
        add_header Accept-Ranges bytes;
        add_header Cache-Control "public, immutable";
        expires 1y;
    }

    # 3. Fonts and sprites (also needed for range requests if using pbf fonts)
    location ~* \.(pbf|json)$ {
        add_header Accept-Ranges bytes;
        expires 1y;
    }

    # 4. Proxy to Martin (use a distinct path)
    location /tiles/ {
        proxy_pass http://martin_server:3000/;   # Note: container name is martin_server (from docker-compose)
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 5. Proxy to Valhalla - use distinct paths to avoid conflicts
    location /routing/ {
        proxy_pass http://valhalla:8002/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 6. Proxy to Vroom - use distinct path
    location /optimize/ {
        proxy_pass http://vroom:3000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    # 7. Proxy to FastAPI Wrapper (The brain)
    location /api/ {
        proxy_pass http://routing-api:5000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # CORS
        add_header Access-Control-Allow-Origin * always;
        add_header Access-Control-Allow-Methods 'GET, POST, OPTIONS' always;
        add_header Access-Control-Allow-Headers 'Content-Type, Authorization' always;

        if ($request_method = OPTIONS) {
            add_header Content-Length 0;
            add_header Content-Type text/plain;
            return 204;
        }
    }
 
    # 8. SPA fallback - must be last and more specific
    location / {
        try_files $uri /index.html;
    }
}
```
- Create Docker file for API
```bash
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 5000

CMD ["python", "app.py"]

```
### Step 3: Importing tiled graph data to Valhalla
#### Download data from internet
- Browse to `https://download.geofabrik.de/`, choose interesting region (country) to download
- Put in folder ./data/pbf
- When deploy Valhalla, it will extract tiled graph

#### Create custom data
- All intersection have been updated, all connections are connected, it’s building correct topology:
  - shared nodes at intersections ✅
  - consistent node IDs across ways ✅
  - split lines where they intersect ✅

- Transform data from original to osm format:
```
# Road Attribute Priorities

| Priority     | Key             | Common Values                                                | Purpose                                                                 |
|--------------|-----------------|--------------------------------------------------------------|-------------------------------------------------------------------------|
| Must have    | highway         | motorway, trunk, primary, secondary, tertiary, unclassified, | Required. Determines the road type and base speed.                      |
|              |                 | residential,service, track, footway, cycleway and _link      | Required. Determines the road type and base speed.                      |
|              | oneway          | yes, no, -1                                                  | Defines flow direction. -1 means flow is against the digitized line.    |
|              | access          | yes, no, permissive, private, delivery                       | General access; serves as a fallback for specific modes.                |
|              | junction        | roundabout                                                   | Triggers "At the roundabout, take the 2nd exit" instructions.           |
| Should have  | maxspeed        | e.g., 100, 50, 30 mph                                        | Overrides default speeds for time calculations.                         |
|              | name            | Main Street                                                  | The primary name used in "Turn right on Main Street."                   |
|              | ref             | I-95, M1                                                     | The highway reference number; prioritized over name on major roads.     |
|              | lanes           | 1, 2, 3, etc.                                                | Affects costing and guidance (e.g., "stay in the left two lanes").      |
|              | surface         | paved, asphalt, unpaved, gravel, dirt                        | Critical for bicycle and auto costing (to avoid rough terrain).         |
|              | layer           | -1, 0, 1, 2                                                  | Defines vertical relative order (crucial for overpasses/underpasses).   |
| Nice to have | destination     | San Jose; Oakland                                            | Used for "Follow signs for San Jose."                                   |
|              | destination:ref | I-880                                                        | The highway ref mentioned on the exit sign.                             |
|              | int_ref         | E 15                                                         | International reference numbers.                                        |
|              | motorcar        | yes, no                                                      | Explicit restriction for cars.                                          |
|              | bicycle         | yes, no, designated                                          | Explicit restriction for bikes.                                         |
|              | foot            | yes, no, designated                                          | Explicit restriction for pedestrians.                                   |
|              | hgv / truck     | yes, no                                                      | Heavy Goods Vehicle restrictions.                                       |
|              | bridge          | yes                                                          | Identifies elevated segments; useful for map matching.                  |
|              | tunnel          | yes                                                          | Critical for GPS-loss logic and display.                                |

```
- Use python scripts with DuckDB to transform source to osm format, before that we can convert spatial file to `parquet` file with `duckdb`

```sql
INSTALL spatial;
LOAD spatial;

COPY (
    SELECT *
    FROM ST_Read('streetCenterline.gpkg')
)
TO 'street_ksa1.parquet'
(FORMAT PARQUET, COMPRESSION ZSTD);
```
#### Create custom script to converts **any vector road dataset** (GeoParquet, GeoJSON, Shapefile, GPKG, etc.) into a **topologically correct OSM XML file** suitable for routing engines like **Valhalla**, **OSRM**, or **GraphHopper**.
```
Raw GeoJSON/GeoPackage/Shapefile/Parquet
   ↓
DuckDB (validate + transform) ✅
   ↓
Parquet (fast storage) ✅
   ↓
OSM XML / PBF (for Valhalla)
```
**Script:** `build_osm.py`

##### 1. Overview
This tool converts **any vector road dataset** (GeoParquet, GeoJSON, Shapefile, GPKG, etc.) into a **topologically correct OSM XML file** suitable for routing engines like **Valhalla**, **OSRM**, or **GraphHopper**.
The main challenge it solves is **topological correctness**: ensuring that roads that should connect at intersections share the exact same node ID in the output OSM data.
---
##### 2. Purpose
- Read road network data from various GIS formats.
- Repair and clean geometries.
- Create shared nodes where roads meet (including fixing small gaps).
- Output a valid `.osm` XML file that can be converted to `.osm.pbf`.
---
##### 3. How It Works (High-Level)

The pipeline consists of **8 major steps**:

1. **Read input** into DuckDB.
2. **Validate & repair** geometries, explode MultiLineStrings.
3. **Extract & deduplicate vertices** → create node table.
4. **Build way_nodes** (ordered list of nodes per road).
5. **Build edges** (road attributes: name, highway type, etc.).
6. **Initial topology cleanup**.
7. **Connectivity fixes** (critical part):
   - 7a: Node-to-node snapping.
   - 7b: Point-to-edge snapping.
8. **Write OSM XML**.
---
##### 4. Key Features

- **Robust snapping** to fix real-world data gaps:
  - Node-to-node (tight + wide tolerance)
  - Point-to-edge projection (T-junctions, roundabout spokes)
- Uses **DuckDB** with Spatial extension for high performance.
- Memory-efficient streaming for very large datasets.
- Comprehensive logging with progress bars.
- Safe geometry handling (avoids GDAL crashes).
---

##### 5. Dependencies

```bash
pip install duckdb lxml tqdm
Optional tools:

osmium (for converting .osm → .osm.pbf)
ogr2ogr (recommended to convert source to GeoParquet first)

Usage
Bashpython build_osm_topology.py <input_file> <output.osm> [memory_gb]
Examples:
Bash# Basic usage
python build_osm_topology.py roads.parquet output.osm

# With more memory
python build_osm_topology.py roads.gpkg output.osm 16
Next step:
Bashosmium cat output.osm -o output.osm.pbf
```
#### Detailed Process
##### Step 1: Input Ingestion

- Reads source file using native DuckDB (for Parquet) or GDAL via st_read().
- Creates raw_segments table with standardized columns:
  - id (surrogate key)
  - name, oneway, lanes, maxspeed
  - highway (computed via HIGHWAY_SQL)
  - Geometry as WKB

##### Step 2: Geometry Validation & Explode

- Repairs invalid geometries using ST_MakeValid().
- Explodes MultiLineString into individual LineString features.
- Filters out degenerate or overly complex segments (MAX_VERTICES).

##### Step 3: Node Extraction & Deduplication

- Extracts all vertices using ST_Points() + ST_Dump().
- Rounds coordinates to ROUNDING_DIGITS (default 7 ≈ 1.1 cm).
- Deduplicates vertices with same rounded coordinates → creates unique node_ids table.

##### Step 4: Build way_nodes

- Joins vertices back to their node IDs.
- Preserves original order within each way.

##### Step 5: Build Edges

- Stores road attributes (name, highway, oneway, etc.) per way.

##### Step 6: Initial Topology Cleanup

- Removes consecutive duplicate node references.
- Drops degenerate ways (fewer than 2 distinct nodes).

##### Step 7a: Node-to-Node Snapping
Two passes:
- All endpoints, tight tolerance (≈ 2m)
- Dangling endpoints only, wider tolerance (≈ 6m)
Uses Union-Find (label propagation) to handle transitive merges efficiently.
##### Step 7b: Point-to-Edge Snapping
Two sub-steps:
- Step A: Snap dangling endpoints to nearby existing nodes (≈ 2m).
- Step B: For remaining dangling points:

  - Build segment index.
  - Project point onto nearest road segment using perpendicular foot calculation.
  - Insert new shared node at projection point.
  - Split the target segment.
  - Remap dangling endpoint to new node.


##### Tuneable Constants

Constant,               Default,      Description
ROUNDING_DIGITS,        7,            Coordinate precision
SNAP_TOL_TIGHT_DEG,     0.00002,      ≈ 2m (all endpoints)
SNAP_TOL_WIDE_DEG,      0.00005,      ≈ 6m (dangling only)
ENDPOINT_SNAP_TOL_DEG,  0.00002,      Point-to-node snap
EDGE_SNAP_TOL_DEG,      .00005,       Point-to-edge snap
MAX_VERTICES,           "10,000",     Reject overly complex segments

##### Core Helper Functions

_build_dangle_table() — Identifies unconnected road ends.
_union_find() — Efficient graph component merging via label propagation.
_apply_labels() — Remaps merged nodes across tables.
_clean_way_nodes() — Removes duplicates and degenerate ways.
_snap_node_to_node() — Orchestrates node snapping.
_snap_point_to_edge() — Handles complex T-junction fixes.
_write_osm_xml() — Memory-efficient streaming XML writer using lxml.xmlfile.


##### Logging & Monitoring

- Creates timestamped log file: build_osm_topology_YYYYMMDD_HHMMSS.log
- Uses tqdm progress bars.
- Detailed timing and statistics at each step.


##### Memory Management

- DuckDB configured with user-defined memory limit.
- Spills to disk when needed.
- gc.collect() calls and strategic table drops.
- Streaming XML output (O(1) memory for writing).


##### Output Format
Valid OSM XML with:

- Negative node/way IDs (standard for new data).
- Proper <node> and <way> elements.
- Relevant tags: highway, name, oneway, lanes, maxspeed.


##### Best Practices

1. Convert source to GeoParquet first for best performance.
2. Run on a machine with sufficient RAM (8–32 GB recommended depending on dataset size).
3. Use WSL/Linux for better I/O performance.
4. Always check the log file after running.

#### Final convert to osm.pbf tile
```bash
osmium cat street_ksa.osm -o street_ksa.osm.pbf
```

Noted when testing script
- Error when extract data when features have many nodes
- This script do not create node when features cross together.