# Contributing

Contributions are welcome through issues and pull requests, subject to the repository owner's policies.

## Development notes

- The desktop GUI uses Python's standard-library `tkinter` and can be syntax-tested outside ArcGIS Pro.
- Geoprocessing workers require ArcGIS Pro and `arcpy`.
- Do not commit downloaded DEMs, LAS/LAZ files, file geodatabases, or generated contour outputs.
- Preserve raw contour geometry; do not introduce smoothing into the authoritative output workflow without an explicit option and documentation.
- Adjacent DEMs should be mosaicked before contouring to preserve seam continuity.

## Tests

Run the lightweight tests with:

```bash
python test_core.py
```

ArcGIS-dependent production tests must be run from an ArcGIS Pro Python environment on Windows.
