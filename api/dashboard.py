"""Vercel entry point for the existing dashboard HTTP handler.

Vercel manages the server; do not launch WEB.app or a background cleanup thread.
"""

from WEB.file_manager import default_file_manager
from WEB.server import BESSDashboardHandler


class handler(BESSDashboardHandler):
    """Reuse all dashboard routes, cleaning stale scratch files per invocation."""

    def handle(self):
        default_file_manager.purge_expired_sessions()
        super().handle()
