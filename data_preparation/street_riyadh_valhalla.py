# Change schema of street data for valhalla
import geopandas as gpd

input_file = r"data\streets_riyadh.geojson"
output_file = r"data\streets_riyadh_valhalla_python.geojson"

# -------- Safe numeric parsing --------
# Using a helper to avoid repetitive try/except blocks
def to_int(val, default=None):
    try: return int(float(val)) # float then int handles "1.0" strings
    except: return default

# Define transform fucntion
def transform_geojson(input_file, output_file):

    # 1. Load the GeoJSON file
    streets_gdf = gpd.read_file(input_file)

    # 2. Define the transformation per row
    def apply_mapping(row):
        new_row = {}

        # Basic attribute mappings
        new_row["id"] = row.get("pkStreetID")
        new_row["name:ar"] = row.get("ArabicName")
        new_row["name"] = row.get("EnglishNam")
        new_row["name:ar1"] = row.get("Strar")
        new_row["name:en1"] = row.get("Stren")
        new_row["width"] = row.get("Width")

        # Parse numeric fields safely    
        subtype = to_int(row.get("Subtype"))
        lanes = to_int(row.get("NoOfLane"), 0)
        fowid = to_int(row.get("StreetFOWI"))
        status_id = to_int(row.get("StreetStat"))
        direction = to_int(row.get("StreetCent")) 
        speed = to_int(row.get("SpeedLimit"))

        new_row["lanes"] = str(lanes) if lanes > 0 else "1"

        # Oneway logic: if value in StreetCenterlineDirection is 1, then oneway is yes, else no   
        new_row["oneway"] = "yes" if direction == 1 else "no"

        # Junction logic: if value in StreetFOWID is 1, then junction is roundabout, else None (JSON null) not "None" (string)
        new_row["junction"] = "roundabout" if fowid == 1 else None 

        # -------- Maxspeed --------
        if speed: new_row["maxspeed"] = str(speed)

        # Access logic: if value in StreetStat is 3 then access is no, else yes 
        if status_id == 3:
            new_row["access"] = "no"
            new_row["surface"] = "dirt"
        # Surface logic: if value in StreetStatusID is 2, then surface is gravel, if value in StreetStatusID is 3, then surface is dirt, else asphalt
        else:
            new_row["access"] = "yes"    
            new_row["surface"] = "gravel" if status_id == 2 else "asphalt"

        # Highway logic with Subtype and StreetFOWID:     
        # if value in StreetFOWID is 3 or 4 and
        #     if subtype is 1 then highway is trunk_link, 
        #     if subtype is 2 then highway is primary_link, 
        #     if subtype is 3 then highway is secondary_link, 
        #     the rest the highway is tertiary_link)
        if fowid in [3, 4]:
            if subtype == 1: new_row["highway"] = "trunk_link"
            elif subtype == 2: new_row["highway"] = "primary_link"
            elif subtype == 3: new_row["highway"] = "secondary_link"
            else: new_row["highway"] = "tertiary_link"

        # Highway logic with Subtype and NoOfLanes: 
        # if value in Subtype is 1, then highway is trunk, if value in Subtype is 2, then highway is primary, 
        # if value in Subtype is 3 and if NoOfLanes is greater than 3, then highway is primary, else highway is secondary,
        # if value in Subtype is 4 and if NoOfLanes is greater than 3, then highway is secondary, els highway is tertiary,
        # if value in Subtype is 5, then highway is tertiary, if value in Subtype is 6, then highway is residential,
        # if value in Subtype is 7, then highway is footway, else highway is road.
        else:
            if subtype == 1: new_row["highway"] = "trunk"
            elif subtype == 2: new_row["highway"] = "primary"
            elif subtype == 3: new_row["highway"] = "primary" if lanes > 3 else "secondary"
            elif subtype == 4: new_row["highway"] = "secondary" if lanes > 3 else "tertiary"
            elif subtype == 5: new_row["highway"] = "tertiary"
            elif subtype == 6: new_row["highway"] = "residential"
            elif subtype == 7: new_row["highway"] = "footway"
            else: new_row["highway"] = "road"    

        return new_row
    # 3. Apply the logic to create a new DataFrame with the transformed schema
    #  Keep the geometry and apply the transformation to the attributes
    transformed_data = streets_gdf.apply(apply_mapping, axis=1, result_type="expand") 

    # Merge the transformed attributes with the original geometry
    transformed_gdf = gpd.GeoDataFrame(transformed_data, geometry=streets_gdf.geometry, crs=streets_gdf.crs)

    # 4. Save the modified GeoJSON file
    transformed_gdf.to_file(output_file, driver="GeoJSON")
    print(f"✅ Transformed GeoJSON saved to {output_file} with {len(transformed_gdf)} rows.")

# Run the transformation
transform_geojson(input_file, output_file)
