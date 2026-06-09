building_unit.geojson
```json
PUT /building_units
{
  "settings": {
    "analysis": {
      "normalizer": {
        "lowercase_normalizer": {
          "type": "custom",
          "filter": ["lowercase"]
          // Optional but very useful for Vietnamese addresses: "asciifolding"  → turns "Lê" → "Le", "Đ" → "D", etc.
        }
      }
    }
  },
  "mappings": {
    "dynamic": "true",              // ← change to "strict" later when stable
    "properties": {
      "UNIT_ID": { "type": "keyword", "normalizer": "lowercase_normalizer"},           // ← this is the key, Exact match for IDs
      "USE_TYPE": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "NAME": { "type": "text" },        // Full-text search
      "NAME_LONG": { "type": "text" },
      "LEVEL_ID": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "HEIGHT": { "type": "float" },
      "LabelNames": { "type": "text" },
      "UnitAddres": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "Sequance": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "Base": { "type": "float" },
      "geometry": { "type": "geo_shape" }  // For MultiPolygon spatial data
    }
  }
}

```json
// Count number imported records
GET /building_units/_count

// Show data structure
GET /building_units/_mapping

// Searching
GET /building_units/_search
{
  "query": {
    "match_all": {}
  },
  "size": 5                       // show only first 5 documents (default is 10)
}

// Remove old one 

DELETE building_units
```
Reindex:
```json
POST _reindex
{
  "source": {
    "index": "building_units"
  },
  "dest": {
    "index": "building_units_v2"
  }
}
```

Alias new one with old name incase we don't want to change the code of app

```json
POST _aliases
{
  "actions": [
    { "add": { "index": "building_units_v2", "alias": "building_units" } }
  ]
}


building_verticals
```json
PUT /building_verticals
{
  "settings": {
    "analysis": {
      "normalizer": {
        "lowercase_normalizer": {
          "type": "custom",
          "filter": ["lowercase"]
        }
      }
    }
  },    
  "mappings": {
    "dynamic": "true",              // ← change to "strict" later when stable
    "properties": {
      "ShortAddress": { "type": "keyword", "normalizer": "lowercase_normalizer" }, // exact match, no full-text needed (codes like "12345 67890") 
      "NoofFloors": { "type": "integer" },  
      "BuildingHeight": { "type": "float" },
      "fkFloorID": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "UnitAddress": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "FloorNumber": { "type": "integer" },
      "FloorUsage": { "type": "keyword", "normalizer": "lowercase_normalizer"  },
      "geometry": { "type": "geo_shape" }
    }
  }
}
```

pois
```json
PUT /pois
{
  "settings": {
    "analysis": {
      "normalizer": {
        "lowercase_normalizer": {
          "type": "custom",
          "filter": ["lowercase"]
        }
      }
    }
  },    
  "mappings": {
    "dynamic": "true",              // ← change to "strict" later when stable
    "properties": {
      "id": { "type": "interger" }, // exact match, no full-text needed (codes like "12345 67890") 
      "name": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "address": { "type": "text" },
      "type": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "class": { "type": "keyword", "normalizer": "lowercase_normalizer"  },
      "geometry": { "type": "geo_point" }
    }
  }
}
```
building_floors
```json
PUT /building_floors
{
  "settings": {
    "analysis": {
      "normalizer": {
        "lowercase_normalizer": {
          "type": "custom",
          "filter": ["lowercase"]
        }
      }
    }
  },    
  "mappings": {
    "dynamic": "true",              // ← change to "strict" later when stable
    "properties": {
      "OBJECTID" : {"type": "integer" },
      "ShortAddress" : {"type": "keyword", "normalizer": "lowercase_normalizer" },
      "VerticalAddress" : {"type": "keyword", "normalizer": "lowercase_normalizer" },
      "BuildingUseType" : {"type": "text" },
      "Building_Type" : {"type": "integer" },
      "BuildingHeight" : {"type": "float" },
      "BuildingID" : {"type": "text" },
      "NoofFloors" : {"type": "integer" },
      "FloorsBelowGround" : {"type": "integer" },
      "FloorsAboveGround" : {"type": "integer" },
      "FloorName" : {"type": "text" },
      "FloorID" : {"type": "text" },
      "FloorNumber" : {"type": "integer" },
      "FloorUsage" : {"type": "text" },
      "NoOfUnits" : {"type": "integer" },
      "Unit_Type" : {"type": "integer" },
      "geometry": { "type": "geo_shape" }
    }
  }
}
```