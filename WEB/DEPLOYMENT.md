# Deploying the dashboard to Vercel

Import the repository with **Root Directory set to the repository root (`.`)**,
not `WEB/`. Select **Other** as the Framework Preset and leave Build Command and
Output Directory overrides unset. The root `vercel.json` routes requests to
`api/dashboard.py`, which reuses the existing dashboard handler. Python 3.12 and
the root `requirements.txt` define the Python environment.

The template, static assets, both optimization engines, and `datasets/converted/`
are bundled. Original datasets, local virtual environments, generated outputs,
and documentation are excluded. No long-running HTTP server is started on
Vercel. Stale session files are cleaned before each invocation rather than by a
background daemon. The function duration is set to 300 seconds; confirm the
project's compute settings allow this duration. Large simulations or animations
can still exceed the function's time, memory, response, or scratch-space limits.

## Temporary files and chart availability

Local runs still use `WEB/tmp/`. Vercel sets `BESS_TMP_DIR` to
`/tmp/bess_web_dashboard` because the deployed application directory is read-only.
Matplotlib's cache also uses `/tmp`.

Both PNG charts are included as base64 in the successful optimization response
(`chart_images`). The frontend turns them into browser-local Blob URLs, reusing
those URLs when changing tabs. Displaying or opening the PNGs at full resolution
therefore does not depend on subsequent requests finding the same Vercel instance.
The PNGs remain visible in the current page after the temporary session is purged.
A new successful run revokes that engine's previous Blob URLs to release memory.

The original PNG files, CSV/JSON downloads and optional GIF still use temporary
session directories. This configuration does not provide shared artifact storage;
separate download/GIF requests can fail if they reach another instance or the
session has expired. Persistent downloads/GIFs require shared object storage and
session metadata, or a persistent Python backend. Refreshing the page clears the
browser's in-memory results and requires another run.

Vercel responses over 4 MB are rejected with an actionable error before exceeding
the platform's 4.5 MB buffered response limit. Select a shorter simulation range
if this occurs. Neither storage provisioning nor a live deployment is included.

## Verification after deployment

1. Load `/` and `/index.html`; confirm CSS and both JavaScript files load.
2. Check `/api/health`, `/api/datasets`, and dataset date selection.
3. Run Grid BESS for one day and Solarnex in Single Day mode; verify both PNGs
   and the hourly table. Repeat using multiple browser sessions to exercise
   separate function instances; PNG display should not make file-view requests.
4. Check build/runtime logs for dependency installation, bundled CBC solver
   availability on Linux, bundle size, and simulation timeouts. Local macOS
   tests cannot validate Vercel's Linux build or instance lifecycle.

References:
- [Vercel Python file-based functions](https://vercel.com/docs/functions/runtimes/python/api-directory)
- [Vercel runtime filesystem](https://vercel.com/docs/functions/runtimes)
- [Temporary file persistence](https://github.com/vercel/community/discussions/6312)
