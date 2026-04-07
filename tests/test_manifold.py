#!/usr/bin/env python3
"""
Unit tests for manifold_bot/manifold_api.py

All tests are fully mocked — no network calls, no API key required.
Run these in CI or pre-push without any external dependencies.

For live smoke testing against the real API, use:
    python3 tests/test_manifold.py --live
"""

import sys
import os
import argparse
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manifold_bot.manifold_api import ManifoldAPI


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — fully mocked
# ─────────────────────────────────────────────────────────────────────────────

class TestManifoldAPIUnit(unittest.TestCase):

    def setUp(self):
        self.api = ManifoldAPI(api_key='test-key')
        self._session_patcher = patch.object(self.api, 'session')
        self.mock_session = self._session_patcher.start()

    def tearDown(self):
        self._session_patcher.stop()

    def _mock_response(self, json_data, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data
        resp.raise_for_status = MagicMock()
        return resp

    def test_get_markets_returns_list(self):
        self.mock_session.request.return_value = self._mock_response([
            {'id': 'mkt1', 'question': 'Will X happen?', 'probability': 0.6},
            {'id': 'mkt2', 'question': 'Will Y happen?', 'probability': 0.3},
        ])
        markets = self.api.get_markets(limit=2)
        self.assertIsInstance(markets, list)
        self.assertEqual(len(markets), 2)
        self.assertEqual(markets[0]['id'], 'mkt1')

    def test_get_markets_passes_limit_param(self):
        self.mock_session.request.return_value = self._mock_response([])
        self.api.get_markets(limit=42)
        call_kwargs = self.mock_session.request.call_args
        self.assertEqual(call_kwargs[1]['params']['limit'], 42)

    def test_get_market_returns_dict(self):
        self.mock_session.request.return_value = self._mock_response(
            {'id': 'mkt1', 'probability': 0.72}
        )
        market = self.api.get_market('mkt1')
        self.assertIsInstance(market, dict)
        self.assertEqual(market['id'], 'mkt1')

    def test_get_user_info_returns_dict(self):
        self.mock_session.request.return_value = self._mock_response(
            {'id': 'user1', 'name': 'Test User', 'balance': 500.0}
        )
        info = self.api.get_user_info()
        self.assertIsInstance(info, dict)
        self.assertIn('name', info)

    def test_search_markets_passes_term(self):
        self.mock_session.request.return_value = self._mock_response([])
        self.api.search_markets(term='bitcoin', limit=5)
        call_kwargs = self.mock_session.request.call_args
        self.assertEqual(call_kwargs[1]['params']['term'], 'bitcoin')

    def test_authorization_header_set(self):
        """API key must be sent as 'Key <token>' in Authorization header."""
        api = ManifoldAPI(api_key='my-secret-key')
        auth = api.session.headers.get('Authorization', '')
        self.assertEqual(auth, 'Key my-secret-key')

    def test_http_error_raises(self):
        import requests
        err_resp = MagicMock()
        err_resp.raise_for_status.side_effect = requests.exceptions.HTTPError('403')
        self.mock_session.request.return_value = err_resp
        with self.assertRaises(requests.exceptions.HTTPError):
            self.api.get_markets()


# ─────────────────────────────────────────────────────────────────────────────
# Live smoke test — only runs with --live flag
# ─────────────────────────────────────────────────────────────────────────────

def run_live_smoke_test():
    """
    Smoke test against the real Manifold API.
    Requires MANIFOLD_API_KEY in the environment or .env file.
    Run manually: python3 tests/test_manifold.py --live
    """
    from manifold_bot.manifold_api import api_client

    print("Testing Manifold API setup...")
    print("=" * 50)

    user_info = api_client.get_user_info()
    print(f"1. API connection: ✅  Connected as {user_info.get('name')} "
          f"(balance ${user_info.get('balance', 0):.2f})")

    markets = api_client.get_markets(limit=3)
    assert len(markets) > 0, "get_markets returned empty list"
    print(f"2. Fetch markets:  ✅  Got {len(markets)} markets")

    results = api_client.search_markets(term='bitcoin', limit=3)
    print(f"3. Search markets: ✅  Found {len(results)} Bitcoin markets")

    print("=" * 50)
    print("✅ All smoke tests passed!")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true',
                        help='Run live smoke test against real Manifold API')
    args, remaining = parser.parse_known_args()

    if args.live:
        run_live_smoke_test()
    else:
        sys.argv = [sys.argv[0]] + remaining
        unittest.main(verbosity=2)
