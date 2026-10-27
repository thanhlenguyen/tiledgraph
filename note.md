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

# version: '3.9'

services:
  postgis:
    image: pgrouting/pgrouting:18-3.6-4.0
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
      - vsol:/var/lib/postgresql
      - ${DATA_DIR}:/data
    shm_size: 1g
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 10

  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:9.5.3
    container_name: elasticsearch
    restart: unless-stopped
    environment:
      - discovery.type=single-node                  # Required for single node
      - xpack.security.enabled=false                # Disable security for dev (no username/password)
      - ES_JAVA_OPTS=-Xms1g -Xmx1g                  # Adjust heap size (1GB each — change based on your RAM)
      - "logger.org.elasticsearch: WARN"            # Reduce log noise
      - http.cors.enabled=true
      - http.cors.allow-origin="*"
      - http.cors.allow-methods=OPTIONS,HEAD,GET,POST,PUT,DELETE
      - http.cors.allow-headers=X-Requested-With,Content-Type,Content-Length,Authorization
      - http.cors.allow-credentials=true   # optional but useful sometimes
    ports:
      - "9200:9200"                                 # HTTP API
      # - "9300:9300"                                 # Transport (optional, can remove if not needed)
    volumes:
      - esdata:/usr/share/elasticsearch/data        # Persist ES data
    healthcheck:
      test: ["CMD-SHELL", "curl -s -f http://localhost:9200/_cat/health?h=status"]
      interval: 30s
      timeout: 10s
      retries: 5
    ulimits:
      memlock: -1
      nofile: 65536

  kibana:
    image: docker.elastic.co/kibana/kibana:9.5.3
    container_name: kibana
    restart: unless-stopped
    ports:
      - "5601:5601"
    environment:
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
      - SERVER_PUBLICBASEURL=http://localhost:5601  # optional, helps with some proxy issues
    volumes:
      - kibana-data:/usr/share/kibana/data
    depends_on:
      elasticsearch:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "curl -s -f http://localhost:5601/api/status | grep -q 'green' || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5    

  typesense:
    image: typesense/typesense:30.2
    container_name: typesense
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "8108:8108"
    volumes:
      - typesense-data:/data
    command: >
      --data-dir /data
      --api-key=${TYPESENSE_API_KEY:-xyz}
      --enable-cors
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8108/health"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 10s

  martin:
    image: ghcr.io/maplibre/martin:1.16.0
    container_name: martin_server
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "3000:3000"
    volumes:
      - ./config/martin-config.yml:/config.yml:ro
      - ${DATA_DIR}/mbtiles:/mbtiles:ro
      - ${DATA_DIR}/pmtiles:/pmtiles:ro
      - ${DATA_DIR}/fonts:/fonts:ro           # RAW .ttf / .otf folders
      - ${DATA_DIR}/sprites:/sprites:ro       # RAW .svg icon folders
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
      - use_tiles_ignore_pbf=True      # Once built, don't rebuild from PBF on restart
      - force_rebuild=False          # set True only when you want to force a full rebuild
      - server_threads=4     # adjust to your CPU cores 
      - build_elevation=False        # set True only if you need elevation data
      - build_admins=False
      - build_time_zones=False
      - build_tar=True               # creates fast .tar index
    volumes:
      - ${DATA_DIR}/pbf:/custom_files
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/status"]
      interval: 30s
      timeout: 10s
      retries: 10
      start_period: 500s   # first build can take a while 

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
    image: python:3.12-slim
    container_name: routing-api
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "5000:5000"  # Expose if direct access; else proxy via Nginx
    volumes:
      - ./routing-api:/app  # Create this folder
    working_dir: /app
    command: >
      sh -c "pip install --no-cache-dir -r requirements.txt && python app.py"
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
    image: dpage/pgadmin4:9.18
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

  typesense-dashboard:
    image: bfritscher/typesense-dashboard:latest
    container_name: typesense_dashboard
    restart: unless-stopped
    ports:
      - "8109:80"   #http://localhost:8109/#/collection  to check data
    depends_on:
      - typesense
      
  nginx:
    image: nginx:alpine
    container_name: nginx
    restart: always
    ports:
      - "3001:80"
    volumes:
      - ./frontend:/usr/share/nginx/html
      - ./config/nginx.conf:/etc/nginx/conf.d/default.conf
    depends_on:
      - martin
      - routing-api
      - valhalla
      - vroom

volumes:
  vsol:
  esdata:        
  kibana-data: 
  typesense-data:
 
 
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

## Phase 2: Update Valhalla data
When the street data is updated, regenerate the OSM file for the entire street network and convert it to .osm.pbf format.
Rebuilding the Valhalla graph tiles inside the Docker container can be time-consuming. To optimize this process:
- Install Valhalla on the host machine
- Create the new tiles.tar file outside the Docker container (on the host machine).
- Copy the updated tiles.tar file into the Valhalla data folder (mounted volume).
- Restart the Docker container for the changes to take effect.

### Step 1: Install Valhalla
1. Update System & Install Dependencies
```Bash
sudo apt update && sudo apt upgrade -y

# Basic tools + build tools
sudo apt install -y git wget curl ca-certificates gnupg2 \
    cmake build-essential pkg-config autoconf libtool \
    libsqlite3-dev libspatialite-dev spatialite-bin \
    libcurl4-openssl-dev zlib1g-dev libgeos-dev libgeos++-dev \
    libprotobuf-dev protobuf-compiler libboost-all-dev \
    libluajit-5.1-dev liblz4-dev libczmq-dev libzmq5 jq unzip
```    
2. Build and Install prime_server (required dependency)
```Bash
git clone https://github.com/kevinkreiser/prime_server.git
cd prime_server
git submodule update --init --recursive

./autogen.sh
./configure --prefix=/usr LIBS="-lpthread"

make -j$(nproc)
sudo make install
cd ..
```
3. Build and Install Valhalla
```Bash
git clone https://github.com/valhalla/valhalla.git
cd valhalla
git submodule sync
git submodule update --init --recursive

mkdir build && cd build

cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr \
  -DENABLE_BENCHMARKS=OFF

make -j$(nproc)
sudo make install

# Verify installation
valhalla_build_config --help
```
This installs the binaries (valhalla_build_tiles, valhalla_build_config, etc.) system-wide.
4. Prepare Your OSM Data.
```Bash
mkdir -p ~/valhalla-data
cd ~/valhalla-data

# Prepare osm.pbf file as previous phase
```
5. Create Configuration File (valhalla.json)
This command creates a configuration file named valhalla.json.

Valhalla needs this file to know where to put the data and where to find supporting files when building tiles and when running the routing service.   
```Bash
mkdir -p valhalla_tiles

valhalla_build_config \
  --mjolnir-tile-dir ${PWD}/valhalla_tiles \
  --mjolnir-tile-extract ${PWD}/valhalla_tiles.tar \
  --mjolnir-timezone ${PWD}/valhalla_tiles/timezones.sqlite \
  --mjolnir-admin ${PWD}/valhalla_tiles/admins.sqlite > valhalla.json
```
![alt text](image.png)
6. Build Supporting Files (Admins + Timezones) if needed
```Bash
# Timezones
valhalla_build_timezones > valhalla_tiles/timezones.sqlite

# Admins (important for driving side, access, etc.)
valhalla_build_admins -c valhalla.json your-file.osm.pbf
```
### Step 2 Create *.tar file
1. Build the Graph Tiles
```Bash
valhalla_build_tiles -c valhalla.json your-file.osm.pbf
```
This step creates the hierarchy folders (0/, 1/, 2/) with .gph files inside valhalla_tiles/.
2. Create tiles.tar:
```Bash
valhalla_build_extract -c valhalla.json -v
```
### Step 3: Replace current *.tar file
- Stop your running Valhalla Docker container.
- Copy the built files to your mounted volume (usually ./custom_files or similar on the host) 
- Update the hash file (the scripted image uses this to detect changes):
```Bash
docker compose stop valhalla
# Backup current working data (optional but safe)
cp -r ~/data/pbf ~/data/pbf_backup_$(date +%Y%m%d)

cd /path/to/your/valhalla-data

cp valhalla_tiles.tar ~/data/pbf/
cp valhalla.json ~/data/pbf/
cp -r valhalla_tiles ~/data/pbf/
cp valhalla_tiles/admins.sqlite ~/data/pbf/ 2>/dev/null || true
cp valhalla_tiles/timezones.sqlite ~/data/pbf/ 2>/dev/null || true

```
Restart the container with these environment variables:YAMLenvironment:
```bash
  - use_tiles_ignore_pbf=True
  - force_rebuild=False
  - build_tar=False

  docker compose start valhalla
```
The container should load the pre-built valhalla_tiles.tar quickly without rebuilding.