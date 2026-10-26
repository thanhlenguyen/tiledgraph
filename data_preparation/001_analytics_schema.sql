-- =============================================================================
-- GeoRouting Lab — 001_analytics_schema.sql
-- Phase 3 (H3 analytics, coverage, hotspots) + Phase 5 (site selection,
-- delivery-point learning).
--
-- Run once:
--   docker compose exec -T postgis psql -U $POSTGRES_USER -d geodb < sql/001_analytics_schema.sql
--
-- Safe to re-run: everything uses IF NOT EXISTS.
-- H3 cell ids are stored as text (e.g. '8853a8d8a1fffff') and computed in
-- Python with the `h3` library, so no extra PostgreSQL extension is needed.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS analytics;

-- -----------------------------------------------------------------------------
-- 1. DEMAND POINTS
-- Anything that represents "where demand is": National Address points,
-- customers, households. weight = how much demand (1 per address by default,
-- or population, or parcels per month).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.demand_points (
    id          BIGSERIAL PRIMARY KEY,
    source_id   TEXT,                         -- id in your source system (e.g. National Address id)
    short_address TEXT,                       -- optional, shown in results
    weight      REAL NOT NULL DEFAULT 1,
    geom        GEOMETRY(Point, 4326) NOT NULL,
    h3_r7       TEXT,                         -- ~5.2 km²  hexagon
    h3_r8       TEXT,                         -- ~0.74 km² hexagon
    h3_r9       TEXT                          -- ~0.11 km² hexagon
);
CREATE INDEX IF NOT EXISTS demand_points_geom_idx  ON analytics.demand_points USING GIST (geom);
CREATE INDEX IF NOT EXISTS demand_points_h3r8_idx  ON analytics.demand_points (h3_r8);
CREATE UNIQUE INDEX IF NOT EXISTS demand_points_source_uidx ON analytics.demand_points (source_id)
    WHERE source_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 2. DELIVERIES (one row per delivery ATTEMPT)
-- geom      = the official address location (copied from demand_points)
-- drop_geom = where the driver's phone actually was at proof-of-delivery
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.deliveries (
    id             BIGSERIAL PRIMARY KEY,
    external_id    TEXT,                      -- id in your delivery system
    address_id     BIGINT REFERENCES analytics.demand_points(id) ON DELETE SET NULL,
    geom           GEOMETRY(Point, 4326) NOT NULL,
    drop_geom      GEOMETRY(Point, 4326),
    status         TEXT NOT NULL CHECK (status IN ('delivered', 'failed')),
    failure_reason TEXT,                      -- e.g. 'address_not_found', 'customer_absent'
    attempted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    vehicle_id     TEXT,
    h3_r7          TEXT,
    h3_r8          TEXT,
    h3_r9          TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_geom_idx      ON analytics.deliveries USING GIST (geom);
CREATE INDEX IF NOT EXISTS deliveries_time_idx      ON analytics.deliveries (attempted_at);
CREATE INDEX IF NOT EXISTS deliveries_address_idx   ON analytics.deliveries (address_id);
CREATE INDEX IF NOT EXISTS deliveries_h3r8_time_idx ON analytics.deliveries (h3_r8, attempted_at);

-- -----------------------------------------------------------------------------
-- 3. LEARNED DELIVERY POINTS (output of /analytics/delivery_points/learn)
-- Where drivers really deliver for each address, learned from GPS.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.learned_delivery_points (
    address_id    BIGINT PRIMARY KEY REFERENCES analytics.demand_points(id) ON DELETE CASCADE,
    official_geom GEOMETRY(Point, 4326) NOT NULL,
    learned_geom  GEOMETRY(Point, 4326) NOT NULL,
    n_used        INT  NOT NULL,              -- deliveries used after removing outliers
    n_total       INT  NOT NULL,              -- successful deliveries with GPS
    offset_m      REAL NOT NULL,              -- distance official → learned
    spread_m      REAL NOT NULL,              -- median distance of drops to learned point
    confidence    REAL NOT NULL,              -- 0..1
    review_status TEXT NOT NULL DEFAULT 'pending'
                  CHECK (review_status IN ('pending', 'approved', 'rejected')),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS learned_dp_geom_idx ON analytics.learned_delivery_points USING GIST (official_geom);
CREATE INDEX IF NOT EXISTS learned_dp_offset_idx ON analytics.learned_delivery_points (offset_m);

-- -----------------------------------------------------------------------------
-- 4. FACILITIES
-- Coverage and site selection read existing facilities from your table
-- topology.places (id, name, type, address, geom) — the same table used by
-- /nearest_facility_pg. Nothing to create here.
-- -----------------------------------------------------------------------------
