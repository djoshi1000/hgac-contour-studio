# Security and data handling

This project downloads public elevation data and can also search user-selected local folders for DEM files.

- Do not commit private local file paths if they reveal sensitive infrastructure or internal network locations.
- Do not commit downloaded DEMs, LAS/LAZ files, geodatabases, or customer deliverables.
- Review run manifests before sharing them publicly because they can contain local source paths.
- The application does not require API keys for the public USGS staged-product URLs used by the downloader.
