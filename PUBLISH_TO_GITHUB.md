# Publish HGAC Contour Studio to GitHub

Recommended repository name: `hgac-contour-studio`

Recommended description:

> Windows GUI for downloading USGS Houston B24 bare-earth DEMs and generating seamless 2-ft and 5-ft contours with ArcGIS Pro.

## Option A — Git command line

Create an empty GitHub repository named `hgac-contour-studio` first. Do not initialize it with a README, `.gitignore`, or license because those files are already included here.

Open Command Prompt or PowerShell in this folder and run:

```bash
git init
git add .
git commit -m "Initial release: HGAC Contour Studio v1.4"
git branch -M main
git remote add origin https://github.com/djoshi1000/hgac-contour-studio.git
git push -u origin main
```

If Git asks you to authenticate, sign in using your normal GitHub authentication method.

## Option B — GitHub Desktop

1. Create an empty repository named `hgac-contour-studio` on GitHub.
2. Add this local folder as an existing repository in GitHub Desktop.
3. Commit all files with message `Initial release: HGAC Contour Studio v1.4`.
4. Publish/push the `main` branch.

## Public vs private

If this code is employer-owned or contains organization-specific implementation details, start with a **private** repository until publication and licensing are approved.

No open-source license is included in this package by design.
