"""Regression checks for delivering charts without shared serverless scratch files."""

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from WEB.server import BESSDashboardHandler, attach_chart_images


class ChartDeliveryTests(unittest.TestCase):
    def test_captures_both_pngs_before_session_is_deleted(self):
        contents = {
            'bess_dispatch_chart.png': b'\x89PNG\r\n\x1a\ndispatch',
            'market_prices_chart.png': b'\x89PNG\r\n\x1a\nprices',
        }
        result = {'status': 'success', 'summary': {'gross_arbitrage_inr': 123}}
        with tempfile.TemporaryDirectory() as directory:
            for name, data in contents.items():
                (Path(directory) / name).write_bytes(data)
            attach_chart_images(result, directory)
        # Simulate losing the entire producing instance's scratch directory.
        self.assertFalse(Path(directory).exists())
        self.assertEqual(result['summary'], {'gross_arbitrage_inr': 123})
        self.assertEqual(set(result['chart_images']), set(contents))
        for name, data in contents.items():
            self.assertEqual(base64.b64decode(result['chart_images'][name], validate=True), data)

    def test_missing_dispatch_cannot_return_partial_chart_success(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'market_prices_chart.png').write_bytes(b'prices')
            result = {'status': 'success'}
            with self.assertRaises(FileNotFoundError):
                attach_chart_images(result, directory)
            self.assertNotIn('chart_images', result)

    def test_missing_price_cannot_return_partial_chart_success(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'bess_dispatch_chart.png').write_bytes(b'dispatch')
            result = {'status': 'success'}
            with self.assertRaises(FileNotFoundError):
                attach_chart_images(result, directory)
            self.assertNotIn('chart_images', result)


class ResponseLimitTests(unittest.TestCase):
    def test_oversized_vercel_response_returns_an_actionable_error(self):
        handler = object.__new__(BESSDashboardHandler)
        handler._send_error_json = Mock()
        with patch.dict('os.environ', {'VERCEL': '1'}):
            handler._send_json({'chart_images': {'chart.png': 'A' * 4_000_000}})
        handler._send_error_json.assert_called_once()
        message, status = handler._send_error_json.call_args.args
        self.assertEqual(status, 413)
        self.assertIn('shorter date range', message)


if __name__ == '__main__':
    unittest.main()
