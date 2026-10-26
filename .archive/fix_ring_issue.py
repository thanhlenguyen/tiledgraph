import json
import numpy as np
from scipy.optimize import least_squares

# Load original
with open('issue_ring.geojson') as f:
    data = json.load(f)

points = np.array(data['features'][0]['geometry']['coordinates'][0])

# Fit circle
def circle_fit(p, x, y):
    xc, yc, r = p
    return np.sqrt((x-xc)**2 + (y-yc)**2) - r

x, y = points[:,0], points[:,1]
center = np.mean(points, axis=0)
r0 = np.mean(np.hypot(x - center[0], y - center[1]))

res = least_squares(circle_fit, [center[0], center[1], r0], args=(x, y))
xc, yc, r = res.x

# Generate smooth circle
theta = np.linspace(0, 2*np.pi, 360)
circle = np.column_stack([
    xc + r * np.cos(theta),
    yc + r * np.sin(theta)
])

# Save clean GeoJSON
new_feature = {
    "type": "Feature",
    "properties": data['features'][0]['properties'],
    "geometry": {
        "type": "LineString",
        "coordinates": circle.tolist() + [circle[0].tolist()]   # closed
    }
}

new_data = {
    "type": "FeatureCollection",
    "name": "issue_ring_fixed",
    "crs": data.get("crs"),
    "features": [new_feature]
}

with open('fixed_ring.geojson', 'w') as f:
    json.dump(new_data, f, indent=2)

print("✅ Saved fixed_ring.geojson with only 361 points")