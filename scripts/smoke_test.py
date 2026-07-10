from address_validation.address_utils import is_chinese_address
from address_validation.comparison_rules import (
    CoordinatePair,
    build_comparison_payload,
    coordinates_match,
    get_comparison_settings,
)
from address_validation.dataset import iter_fetch_tasks, load_address_dataset
from address_validation.database import Database
from address_validation.comparator import RoutineComparator

assert is_chinese_address("香港九龍") is True
assert is_chinese_address("123 Nathan Road 尖沙咀") is False

records = load_address_dataset("data/address.xlsx")
assert len(records) == 3
tasks = list(iter_fetch_tasks(records))
assert len(tasks) == 6

config = {
    "comparison": {
        "criteria": "coordinates",
        "coordinate_tolerance": 1.0,
        "coordinate_fields": {"easting": "easting", "northing": "northing"},
    }
}
settings = get_comparison_settings(config)
pair = CoordinatePair(836123.4, 819456.7)
payload = build_comparison_payload(
    criteria="coordinates",
    coordinates=pair,
    building_csuid="CSUID-001",
)
assert coordinates_match(pair, 836123.4, 819456.7, 1.0) is True

db = Database("data/test_schema.db")
run1 = db.create_run("routine", endpoint_name="our_address_api", comparison_criteria="coordinates")
db.save_validation_result(
    run1,
    row_id=1,
    address_type="EADDRESS",
    address="123 Nathan Road",
    endpoint="our_address_api",
    coordinates='{"easting": 836123.4, "northing": 819456.7}',
    building_csuid="CSUID-001",
    comparison_value='{"easting": 836123.4, "northing": 819456.7}',
    response_code=200,
    expected_easting=836123.4,
    expected_northing=819456.7,
    expected_building_csuid="CSUID-001",
    chinese_address=False,
)
run2 = db.create_run("routine", endpoint_name="our_address_api", comparison_criteria="coordinates")
db.save_validation_result(
    run2,
    row_id=1,
    address_type="EADDRESS",
    address="123 Nathan Road",
    endpoint="our_address_api",
    coordinates='{"easting": 836999.0, "northing": 819456.7}',
    building_csuid="CSUID-001",
    comparison_value='{"easting": 836999.0, "northing": 819456.7}',
    response_code=200,
    expected_easting=836123.4,
    expected_northing=819456.7,
    expected_building_csuid="CSUID-001",
    chinese_address=False,
)
comparison = RoutineComparator(db, settings).compare_runs(run2, run1)
assert comparison.has_differences is True
print("ok")
