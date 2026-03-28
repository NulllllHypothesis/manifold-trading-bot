"""
Manifold Markets API Client
Handles authentication and API calls.
"""

import requests
import json
from typing import Dict, List, Optional, Any
from .config import MANIFOLD_API_KEY, MANIFOLD_API_BASE, ENDPOINTS


class ManifoldAPI:
    """Client for Manifold Markets API"""
    
    def __init__(self, api_key: str = MANIFOLD_API_KEY):
        self.api_key = api_key
        self.base_url = MANIFOLD_API_BASE
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json"
        })
    
    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Make HTTP request to Manifold API"""
        url = f"{self.base_url}{endpoint}"
        
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response: {e.response.text}")
            raise
    
    def get_markets(self, limit: int = 100, before: Optional[str] = None) -> List[Dict]:
        """Get list of markets"""
        params = {"limit": limit}
        if before:
            params["before"] = before
        
        endpoint = ENDPOINTS["markets"]
        return self._make_request("GET", endpoint, params=params)
    
    def get_market(self, market_id: str) -> Dict:
        """Get specific market by ID"""
        endpoint = ENDPOINTS["market"].format(marketId=market_id)
        return self._make_request("GET", endpoint)
    
    def search_markets(self, term: str, limit: int = 20) -> List[Dict]:
        """Search markets by term"""
        endpoint = ENDPOINTS["search_markets"]
        params = {"term": term, "limit": limit}
        return self._make_request("GET", endpoint, params=params)
    
    def get_user_info(self) -> Dict:
        """Get current user information"""
        return self._make_request("GET", ENDPOINTS["me"])
    
    def place_bet(self, amount: int, contract_id: str, outcome: str, 
                  prob_before: Optional[float] = None) -> Dict:
        """Place a bet on a market"""
        data = {
            "amount": amount,
            "contractId": contract_id,
            "outcome": outcome
        }
        
        if prob_before is not None:
            data["probBefore"] = prob_before
        
        return self._make_request("POST", ENDPOINTS["bet"], json=data)
    
    def get_user_bets(self, username: Optional[str] = None, 
                     market_id: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get bets for a user or market"""
        params = {"limit": limit}
        if username:
            params["username"] = username
        if market_id:
            params["contractId"] = market_id
        
        return self._make_request("GET", ENDPOINTS["bets"], params=params)
    
    def create_market(self, question: str, description: str, 
                     outcome_type: str = "BINARY", close_time: Optional[int] = None,
                     tags: List[str] = None) -> Dict:
        """Create a new market"""
        data = {
            "question": question,
            "description": description,
            "outcomeType": outcome_type,
            "visibility": "public"
        }
        
        if close_time:
            data["closeTime"] = close_time
        if tags:
            data["tags"] = tags
        
        return self._make_request("POST", ENDPOINTS["markets"], json=data)


# Singleton instance
api_client = ManifoldAPI()