from openpyxl import Workbook
from pathlib import Path

path = Path("data")
path.mkdir(exist_ok=True)
workbook = Workbook()
worksheet = workbook.active
worksheet.append(
  ["id", "EADDRESS", "CADDRESS", "EASTING", "NORTHING", "BUILDING_CSUID"]
)
rows = [
  (1, "123 Nathan Road, Tsim Sha Tsui", "九龍尖沙咀彌敦道123號", 836123.4, 819456.7, "CSUID-001"),
  (2, "88 Queensway, Admiralty", "香港金鐘道88號", 834210.2, 816890.5, "CSUID-002"),
  (3, "1 Harbour Road, Wanchai", "港灣道1號", 835001.0, 815120.3, "CSUID-003"),
]
for row in rows:
  worksheet.append(row)
workbook.save(path / "address.xlsx")
print("created data/address.xlsx")
