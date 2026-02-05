"""
dpumesh.requests - Non-blocking HTTP client

Two modes:
1. parallel() - Submit all, wait all (recommended)
2. Session.get/post - For compatibility, but registers with server context
"""

import json
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass

from . import dpu_lib


@dataclass
class Response:
    """Compatible with requests.Response"""
    status_code: int
    text: str
    headers: Dict[str, str]
    content: bytes
    
    def json(self):
        return json.loads(self.text)


def parallel(requests_list: List[Tuple[str, str, Optional[Dict], Optional[str]]]) -> List[Response]:
    """
    Parallel HTTP requests via DPU - TRUE non-blocking
    
    1. Submit ALL requests to Egress SQ (no waiting)
    2. Wait for ALL responses from Egress CQ
    
    Args:
        requests_list: List of (method, url, headers, body) tuples
    
    Returns:
        List of Response objects in same order
    """
    if not requests_list:
        return []
    
    # Get current context from server
    from .server import get_current_context, get_server
    ctx = get_current_context()
    server = get_server()
    
    # Phase 1: Submit ALL requests to Egress SQ
    req_ids = []
    for i, item in enumerate(requests_list):
        method, url, headers, body = item
        
        req_id = dpu_lib.submit_egress_request(
            method=method,
            url=url,
            headers=headers or {},
            body=body
        )
        req_ids.append(req_id)
        
        # Register with server for tracking
        if ctx and server:
            service_name = f"external_{i}_{url}"
            server.register_egress_request(ctx, req_id, service_name)
    
    # Phase 2: If we're in server context, DON'T block here
    # The server's main loop will handle responses
    if ctx and server:
        # Return placeholder responses - actual responses come via server
        # The WSGI app will complete, and server handles the rest
        return [Response(status_code=202, text="pending", headers={}, content=b"pending") 
                for _ in req_ids]
    
    # Phase 2b: If standalone (no server), block and wait
    responses = []
    for req_id in req_ids:
        resp = dpu_lib.wait_egress_response(req_id)
        responses.append(Response(
            status_code=resp.status_code,
            text=resp.body,
            headers=resp.headers,
            content=resp.body.encode() if isinstance(resp.body, str) else resp.body
        ))
    
    return responses


class Session:
    """
    requests.Session compatible interface
    
    When used inside dpumesh-server:
    - Submits to Egress SQ
    - Registers with server context
    - Returns immediately (non-blocking)
    
    When used standalone:
    - Falls back to blocking behavior
    """
    
    def __init__(self):
        self._default_headers = {}
    
    def get(self, url: str, headers: Optional[Dict] = None, **kwargs) -> Response:
        return self._request("GET", url, headers=headers, **kwargs)
    
    def post(self, url: str, data: Optional[str] = None, headers: Optional[Dict] = None, **kwargs) -> Response:
        return self._request("POST", url, data=data, headers=headers, **kwargs)
    
    def _request(self, method: str, url: str, data: Optional[str] = None,
                 headers: Optional[Dict] = None, **kwargs) -> Response:
        """Single request - uses parallel() internally"""
        req_headers = self._default_headers.copy()
        if headers:
            req_headers.update(headers)
        
        results = parallel([(method, url, req_headers, data)])
        return results[0]


# Module-level convenience functions
_default_session = Session()

def get(url: str, headers: Optional[Dict] = None, **kwargs) -> Response:
    return _default_session.get(url, headers=headers, **kwargs)

def post(url: str, data: Optional[str] = None, headers: Optional[Dict] = None, **kwargs) -> Response:
    return _default_session.post(url, data=data, headers=headers, **kwargs)