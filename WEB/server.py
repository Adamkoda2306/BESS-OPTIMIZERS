"""HTTP server and REST API router for the BESS Web Dashboard."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from pathlib import Path
import urllib.parse

try:
    from WEB.file_manager import default_file_manager
    from WEB.services import (
        get_available_datasets,
        get_dataset_date_range,
        run_grid_bess_service,
        run_solarnex_bess_service,
    )
except ImportError:
    try:
        from .file_manager import default_file_manager
        from .services import (
            get_available_datasets,
            get_dataset_date_range,
            run_grid_bess_service,
            run_solarnex_bess_service,
        )
    except ImportError:
        from file_manager import default_file_manager
        from services import (
            get_available_datasets,
            get_dataset_date_range,
            run_grid_bess_service,
            run_solarnex_bess_service,
        )

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / 'templates'
STATIC_DIR = WEB_DIR / 'static'


class BESSDashboardHandler(SimpleHTTPRequestHandler):
    """Handles static web requests and REST API routing for BESS simulations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _send_json(self, data, status_code=200):
        """Helper to send a JSON response."""
        encoded = json.dumps(data, default=str).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error_json(self, message, status_code=400):
        """Helper to send a structured error response."""
        logger.warning("API error [%s]: %s", status_code, message)
        self._send_json({'status': 'error', 'message': str(message)}, status_code=status_code)

    def do_OPTIONS(self):
        """Handle CORS pre-flight."""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        """Route GET requests for UI and APIs."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        # 1. Main Dashboard UI
        if path in ('/', '/index.html'):
            html_file = TEMPLATES_DIR / 'index.html'
            if html_file.is_file():
                content = html_file.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_error(404, "Dashboard template not found")
                return

        # 2. Static Assets (/static/...)
        if path.startswith('/static/'):
            rel_path = path[len('/static/'):]
            target_file = (STATIC_DIR / rel_path).resolve()
            # Prevent directory traversal
            if str(target_file).startswith(str(STATIC_DIR)) and target_file.is_file():
                mime, _ = mimetypes.guess_type(target_file)
                mime = mime or 'application/octet-stream'
                content = target_file.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_error(404, "Static file not found")
                return

        # 3. API: List available datasets
        if path == '/api/datasets':
            try:
                datasets = get_available_datasets()
                self._send_json({'status': 'success', 'datasets': datasets})
            except Exception as exc:
                self._send_error_json(f"Failed to list datasets: {exc}", 500)
            return

        # 4. API: Get dates for a dataset
        if path == '/api/dataset-dates':
            dataset = query.get('dataset', [None])[0]
            if not dataset:
                self._send_error_json("Missing 'dataset' parameter", 400)
                return
            try:
                info = get_dataset_date_range(dataset)
                self._send_json({'status': 'success', 'data': info})
            except Exception as exc:
                self._send_error_json(f"Failed to read dataset dates: {exc}", 400)
            return

        # 5. API: Session TTL countdown
        if path == '/api/session-ttl':
            session_id = query.get('session_id', [None])[0]
            if not session_id:
                self._send_error_json("Missing 'session_id'", 400)
                return
            ttl = default_file_manager.get_session_ttl(session_id)
            self._send_json({
                'session_id': session_id,
                'ttl_seconds': ttl,
                'is_expired': ttl <= 0,
            })
            return

        # 6. API: Download or view generated artifacts from WEB/tmp
        if path in ('/api/files/download', '/api/files/view'):
            session_id = query.get('session_id', [None])[0]
            filename = query.get('file', [None])[0]
            if not session_id or not filename:
                self._send_error_json("Missing 'session_id' or 'file' query parameter", 400)
                return

            sdir = default_file_manager.get_session_dir(session_id)
            if not sdir or not sdir.is_dir():
                self._send_error_json("Session not found or expired (>30 min)", 404)
                return

            target_file = (sdir / filename).resolve()
            if not str(target_file).startswith(str(sdir)) or not target_file.is_file():
                self._send_error_json("Requested file not found in session", 404)
                return

            mime, _ = mimetypes.guess_type(target_file)
            mime = mime or 'application/octet-stream'
            content = target_file.read_bytes()
            disposition = 'attachment' if path == '/api/files/download' else 'inline'

            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Content-Disposition', f'{disposition}; filename="{target_file.name}"')
            self.end_headers()
            self.wfile.write(content)
            return

        # 7. Health check
        if path == '/api/health':
            self._send_json({'status': 'ok', 'app': 'BESS Dashboard'})
            return

        # Default 404
        self.send_error(404, "Endpoint not found")

    def do_POST(self):
        """Route POST requests for running optimizations."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # Parse JSON body
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode('utf-8'))
            except Exception as exc:
                self._send_error_json(f"Invalid JSON payload: {exc}", 400)
                return
        else:
            payload = {}

        # 1. Run Grid BESS Optimization
        if path == '/api/grid-bess/run':
            try:
                dataset = payload.get('dataset')
                date_val = payload.get('date')
                capacity = float(payload.get('capacity_mwh', 2.0))
                power = float(payload.get('max_power_mw', 1.0))
                efficiency = float(payload.get('efficiency', 0.92))

                if not dataset or not date_val:
                    self._send_error_json("Missing 'dataset' or 'date'", 400)
                    return

                # Create dedicated session inside WEB/tmp
                session_id, session_dir = default_file_manager.create_session('grid_bess')

                result = run_grid_bess_service(
                    dataset_path=dataset,
                    selected_date_str=date_val,
                    capacity_mwh=capacity,
                    max_power_mw=power,
                    efficiency=efficiency,
                    output_dir=session_dir,
                )

                result['session_id'] = session_id
                result['ttl_seconds'] = default_file_manager.expiration_seconds
                result['session_path'] = str(session_dir)

                self._send_json(result)
            except Exception as exc:
                logger.exception("Error executing Grid BESS: %s", exc)
                self._send_error_json(str(exc), 500)
            return

        # 2. Run Solarnex BESS Simulation
        if path == '/api/solarnex/run':
            try:
                dataset = payload.get('dataset')
                date_mode = payload.get('date_mode', 'full')
                date_val = payload.get('date')
                start_date = payload.get('start_date')
                end_date = payload.get('end_date')
                capacity = float(payload.get('capacity_mwh', 10.0))
                power = float(payload.get('max_power_mw', 2.5))
                rte = float(payload.get('rte', 0.90))
                horizon = int(payload.get('horizon_hours', 48))
                step = int(payload.get('step_hours', 24))
                spike_filter = bool(payload.get('spike_filter', True))
                animate = bool(payload.get('animate', False))
                animation_hours = payload.get('animation_hours')
                if animation_hours:
                    animation_hours = int(animation_hours)
                gif_duration = float(payload.get('gif_duration', 40.0))

                if not dataset:
                    self._send_error_json("Missing 'dataset'", 400)
                    return

                # Create dedicated session inside WEB/tmp
                session_id, session_dir = default_file_manager.create_session('solarnex')

                result = run_solarnex_bess_service(
                    dataset_path=dataset,
                    date_mode=date_mode,
                    selected_date_str=date_val,
                    start_date_str=start_date,
                    end_date_str=end_date,
                    capacity_mwh=capacity,
                    max_power_mw=power,
                    rte=rte,
                    horizon_hours=horizon,
                    step_hours=step,
                    spike_filter=spike_filter,
                    animate=animate,
                    animation_hours=animation_hours,
                    gif_duration=gif_duration,
                    output_dir=session_dir,
                )

                result['session_id'] = session_id
                result['ttl_seconds'] = default_file_manager.expiration_seconds
                result['session_path'] = str(session_dir)

                self._send_json(result)
            except Exception as exc:
                logger.exception("Error executing Solarnex BESS: %s", exc)
                self._send_error_json(str(exc), 500)
            return

        self.send_error(404, "Endpoint not found")


def run_server(host='127.0.0.1', port=8080, tmp_dir=None):
    """Start the ThreadingHTTPServer with background cleanup daemon."""
    if tmp_dir:
        default_file_manager.base_dir = Path(tmp_dir)
        default_file_manager._ensure_base_dir()

    default_file_manager.start_cleanup_worker(interval_seconds=30)
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, BESSDashboardHandler)

    print("\n" + "=" * 68)
    print(" 🚀 BESS Interactive Web Dashboard Server Running")
    print(f" 🌐 URL: http://{host}:{port}/")
    print(f" 📁 Temporary session artifacts: {default_file_manager.base_dir}")
    print(f" ⏱️ Auto-cleanup threshold: {default_file_manager.expiration_seconds // 60} minutes")
    print("=" * 68 + "\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        default_file_manager.stop_cleanup_worker()
        httpd.server_close()
        print("Dashboard server stopped cleanly.")

