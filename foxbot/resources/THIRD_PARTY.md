# Third-party data

## items.json

Built by `tools/build_item_catalog.py` from:

- foxhole-stockpiles `data/catalog.json` by Jorge Garcia (https://github.com/xurxogr/foxhole-stockpiles), MIT License.
- FIR (Foxhole Inventory Report) catalog by GICodeWarrior (https://github.com/GICodeWarrior/fir), MIT License.

Item names and data are from Foxhole by Siege Camp.

## locations.json

Snapshot of hex and town names from the official Foxhole War API (https://github.com/clapfoot/warapi), refreshed at runtime.

## icons/

Item and vehicle icons downloaded by `tools/build_item_icons.py` from the FIR project (https://github.com/GICodeWarrior/fir, `foxhole/<version>/icons`). The artwork belongs to Siege Camp (Foxhole); it is used here for a fan-made regiment tool.

## maps/

- Hex map images from the official Foxhole War API repository (https://github.com/clapfoot/warapi, `Images/Maps`), artwork by Siege Camp, converted to WebP by `tools/build_maps.py`.
- World layout (axial hex coordinates for all 53 regions) from foxhole-hexes by Gudjon Magnusson (https://github.com/notbadjon/foxhole-hexes), MIT License. `world.webp` and `layout.json` are built from these two sources.
