from pathlib import Path

from address_validation.result_parser import extract_endpoint_result

ALS_RESPONSE = Path("scripts/fixtures/als_ifc.json").read_text(encoding="utf-8-sig")
MAP_RESPONSE = Path("scripts/fixtures/map_ifc.json").read_text(encoding="utf-8-sig")
ASE_RESPONSE = """{
  "status": "success",
  "data": {
    "apm": [{
      "building_csuid": "4124619266T20050430",
      "easting": 841248.166,
      "northing": 819265.196
    }]
  }
}"""

als_coords, als_csuid = extract_endpoint_result(
    ALS_RESPONSE,
    {
        "selection": "first_in_path",
        "array_path": "SuggestedAddress",
        "item_coordinates_path": "Address.PremisesAddress.GeospatialInformation",
        "building_csuid_path": "Address.PremisesAddress.GeoAddress",
    },
    easting_field="Easting",
    northing_field="Northing",
)
assert als_coords.easting == 834325.0
assert als_coords.northing == 816207.0
assert als_csuid == "3432516207T20050430"

map_coords, map_csuid = extract_endpoint_result(
    MAP_RESPONSE,
    {"selection": "root_first"},
    easting_field="x",
    northing_field="y",
)
assert map_coords.easting == 834366.0
assert map_coords.northing == 816175.0
assert map_csuid is None

ase_coords, ase_csuid = extract_endpoint_result(
    ASE_RESPONSE,
    {
        "selection": "first_in_data_buckets",
        "data_path": "data",
        "building_csuid_path": "building_csuid",
    },
)
assert ase_coords.easting == 841248.166
assert ase_coords.northing == 819265.196
assert ase_csuid == "4124619266T20050430"

print("ok")
