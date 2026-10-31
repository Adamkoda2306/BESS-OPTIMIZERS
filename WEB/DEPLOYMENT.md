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

**This configuration does not provide shared artifact storage.** Vercel's scratch
space and the session registry are instance-local and ephemeral. The subsequent
PNG/GIF/download requests may reach a different instance, or an instance may be
recycled, so generated charts and downloads are not guaranteed to remain
available, even before the session expires.

For reliable production results, add shared object storage for artifacts and
shared session metadata, or host the existing Python server on a persistent
backend and proxy its API from Vercel. A `vercel.json` alone cannot provide this
persistence. Neither storage provisioning nor a live deployment is included here.

## Verification after deployment

1. Load `/` and `/index.html`; confirm CSS and both JavaScript files load.
2. Check `/api/health`, `/api/datasets`, and dataset date selection.
3. Run Grid BESS for one day and Solarnex in Single Day mode; verify both PNGs
   and the hourly table. Repeat using multiple browser sessions to exercise
   separate function instances once shared storage is configured.
4. Check build/runtime logs for dependency installation, bundled CBC solver
   availability on Linux, bundle size, and simulation timeouts. Local macOS
   tests cannot validate Vercel's Linux build or instance lifecycle.

References:
- [Vercel Python file-based functions](https://vercel.com/docs/functions/runtimes/python/api-directory)
- [Vercel runtime filesystem](https://vercel.com/docs/functions/runtimes)
- [Temporary file persistence](https://github.com/vercel/community/discussions/6312)
