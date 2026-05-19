building_units.geojson
```json
PUT /building_units_v2
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

buildings_vertical
```json
PUT /buildings_vertical_v2
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
Reindex:
```json
POST _reindex
{
  "source": {
    "index": "buildings_vertical"
  },
  "dest": {
    "index": "buildings_vertical_v2"
  }
}
```
Remove old one 
```json
DELETE buildings_vertical
```

Alias new one with old name incase we don't want to change the code of app

```json
POST _aliases
{
  "actions": [
    { "add": { "index": "buildings_vertical_v2", "alias": "buildings_vertical" } }
  ]
}