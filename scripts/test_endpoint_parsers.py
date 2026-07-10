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
    }],
    "ifc": [{
      "building_csuid": "3432516207T20050430",
      "easting": 834325.0,
      "northing": 816207.0
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

ase_settings = {
    "selection": "first_in_data_buckets",
    "data_path": "data",
    "building_csuid_path": "building_csuid",
}
ase_coords, ase_csuid = extract_endpoint_result(ASE_RESPONSE, ase_settings, query_address="apm")
assert ase_coords.easting == 841248.166
assert ase_coords.northing == 819265.196
assert ase_csuid == "4124619266T20050430"

ifc_coords, ifc_csuid = extract_endpoint_result(ASE_RESPONSE, ase_settings, query_address="ifc")
assert ifc_coords.easting == 834325.0
assert ifc_coords.northing == 816207.0
assert ifc_csuid == "3432516207T20050430"

from address_validation.fetcher import build_request, build_job_units, get_fetch_mode
from address_validation.dataset import FetchTask

one_req = build_request(
    {"url": "https://example/query_debug", "request": {"address_in": "json_array", "address_key": "address"}},
    ["apm"],
)
assert one_req["json"] == {"address": ["apm"]}

batch_req = build_request(
    {"url": "https://example/query_debug", "request": {"address_in": "json_array", "address_key": "address"}},
    ["apm", "ifc"],
)
assert batch_req["json"] == {"address": ["apm", "ifc"]}

endpoint = {
    "name": "ase_query_debug",
    "url": "https://example/query_debug",
    "request": {"address_in": "json_array", "address_key": "address", "fetch_mode": "batch", "batch_size": 2},
}
assert get_fetch_mode(endpoint) == "batch"
tasks = [
    FetchTask(1, "EADDRESS", "apm", None, None, None),
    FetchTask(2, "EADDRESS", "ifc", None, None, None),
    FetchTask(3, "EADDRESS", "moko", None, None, None),
]
units = build_job_units([(endpoint, task) for task in tasks])
assert len(units) == 2
assert [t.address for t in units[0][1]] == ["apm", "ifc"]
assert [t.address for t in units[1][1]] == ["moko"]

print("ok")
