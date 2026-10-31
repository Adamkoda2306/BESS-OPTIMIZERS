"""Session and temporary file management for the BESS Web Dashboard.

Generates run directories under WEB/tmp/
and purges session artifacts after 30 minutes.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

logger = logging.getLogger(__name__)

SESSION_EXPIRATION_SECONDS = 10 * 60  # 10 minutes


class TempFileManager:
    """Manages temporary session directories with automatic 30-minute purging."""

    def __init__(self, base_dir=None, expiration_seconds=SESSION_EXPIRATION_SECONDS):
        # Vercel has a read-only application directory; local runs keep WEB/tmp.
        base_dir = base_dir or os.environ.get('BESS_TMP_DIR')
        if base_dir:
            self.base_dir = Path(base_dir).resolve()
        else:
            self.base_dir = Path(__file__).resolve().parent / 'tmp'
        self.expiration_seconds = expiration_seconds
        self._lock = threading.Lock()
        self._sessions = {}  # session_id -> {created_at, dir_path, meta}
        self._stop_event = threading.Event()
        self._cleanup_thread = None

        self._ensure_base_dir()

    def _ensure_base_dir(self):
        """Create the temporary root directory if it does not exist."""
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def start_cleanup_worker(self, interval_seconds=30):
        """Start the background thread that deletes files older than 30 minutes."""
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return

        self._stop_event.clear()

        def _worker():
            logger.info("Temporary file cleanup daemon started (TTL: %ss, check interval: %ss)",
                        self.expiration_seconds, interval_seconds)
            while not self._stop_event.is_set():
                try:
                    self.purge_expired_sessions()
                except Exception as exc:
                    logger.error("Error during temporary session cleanup: %s", exc)
                self._stop_event.wait(interval_seconds)

        self._cleanup_thread = threading.Thread(target=_worker, name="BESS-TempCleanup-Thread", daemon=True)
        self._cleanup_thread.start()

    def stop_cleanup_worker(self):
        """Stop the background cleanup thread."""
        self._stop_event.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=3)

    def create_session(self, module_name="run"):
        """Create a new temporary session directory."""
        self._ensure_base_dir()
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        session_id = f"{module_name}_{timestamp_str}_{token}"
        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        now = time.time()
        with self._lock:
            self._sessions[session_id] = {
                'created_at': now,
                'path': session_dir,
                'module': module_name,
            }

        # Write metadata into the session folder
        meta_file = session_dir / '.session_meta.json'
        try:
            meta_file.write_text(json.dumps({
                'session_id': session_id,
                'created_at': now,
                'created_at_iso': datetime.now(timezone.utc).isoformat(),
                'expires_at': now + self.expiration_seconds,
                'ttl_seconds': self.expiration_seconds,
                'module': module_name,
            }, indent=2))
        except Exception as exc:
            logger.warning("Could not write session metadata: %s", exc)

        return session_id, session_dir

    def get_session_dir(self, session_id):
        """Return the directory path for a valid session, or None."""
        with self._lock:
            info = self._sessions.get(session_id)
            if info and info['path'].is_dir():
                return info['path']

        # Fallback check on filesystem
        candidate = self.base_dir / session_id
        if candidate.is_dir():
            return candidate
        return None

    def get_session_ttl(self, session_id):
        """Return remaining seconds until auto-deletion for the given session."""
        with self._lock:
            info = self._sessions.get(session_id)
            if info:
                elapsed = time.time() - info['created_at']
                return max(0, int(self.expiration_seconds - elapsed))

        meta_file = self.base_dir / session_id / '.session_meta.json'
        if meta_file.is_file():
            try:
                data = json.loads(meta_file.read_text())
                elapsed = time.time() - data.get('created_at', 0)
                return max(0, int(self.expiration_seconds - elapsed))
            except Exception:
                pass
        return 0

    def purge_expired_sessions(self):
        """Scan and delete all session directories older than expiration_seconds (30 min)."""
        now = time.time()
        purged_count = 0

        # Purge tracked in-memory sessions
        with self._lock:
            expired_ids = [
                sid for sid, info in self._sessions.items()
                if (now - info['created_at']) >= self.expiration_seconds
            ]
            for sid in expired_ids:
                info = self._sessions.pop(sid)
                if info['path'].exists():
                    try:
                        shutil.rmtree(info['path'], ignore_errors=True)
                        purged_count += 1
                        logger.info("Purged expired session directory: %s", info['path'])
                    except Exception as err:
                        logger.warning("Failed to remove session %s: %s", sid, err)

        # Scan base_dir on disk for any untracked or orphaned directories
        if self.base_dir.is_dir():
            for entry in self.base_dir.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    mtime = entry.stat().st_mtime
                    age = now - mtime
                    if age >= self.expiration_seconds:
                        shutil.rmtree(entry, ignore_errors=True)
                        purged_count += 1
                        logger.info("Purged expired orphaned folder: %s (age %.1f min)",
                                    entry.name, age / 60)
                except Exception as err:
                    logger.warning("Could not inspect/remove %s: %s", entry, err)

        return purged_count

    def list_session_files(self, session_id):
        """List files available for download in a session directory."""
        sdir = self.get_session_dir(session_id)
        if not sdir or not sdir.is_dir():
            return []

        files = []
        for p in sorted(sdir.iterdir()):
            if p.is_file() and not p.name.startswith('.'):
                files.append({
                    'name': p.name,
                    'size_bytes': p.stat().st_size,
                    'extension': p.suffix.lower(),
                })
        return files


# Global singleton instance
default_file_manager = TempFileManager()

