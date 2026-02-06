"""
dpumesh.requests - DPU-backed HTTP client

For standalone/blocking use only.
In server mode, ExternalServiceExecutor registers egress_graph on ctx directly.
Server handles all non-blocking execution.

Session.get/post provides requests.Session compatible interface (blocking).
"""

import json
from typing import Optional, Dict
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


class Session:
    """
    requests.Session compatible interface (blocking).
    
    For standalone use outside dpumesh-server.
    """
    
    def __init__(self):
        self._default_headers = {}
    
    def get(self, url: str, headers: Optional[Dict] = None, **kwargs) -> Response:
        return self._request("GET", url, headers=headers, **kwargs)
    
    def post(self, url: str, data: Optional[str] = None,
             headers: Optional[Dict] = None, **kwargs) -> Response:
        return self._request("POST", url, data=data, headers=headers, **kwargs)
    
    def _request(self, method: str, url: str, data: Optional[str] = None,
                 headers: Optional[Dict] = None, **kwargs) -> Response:
        req_headers = self._default_headers.copy()
        if headers:
            req_headers.update(headers)
        
        req_id = dpu_lib.submit_egress_request(
            method=method,
            url=url,
            headers=req_headers,
            body=data
        )
        
        resp = dpu_lib.wait_egress_response(req_id)
        return Response(
            status_code=resp.status_code,
            text=resp.body,
            headers=resp.headers,
            content=resp.body.encode() if isinstance(resp.body, str) else resp.body
        )


# Module-level convenience (blocking)
_default_session = Session()

def get(url: str, headers: Optional[Dict] = None, **kwargs) -> Response:
    return _default_session.get(url, headers=headers, **kwargs)

def post(url: str, data: Optional[str] = None,
         headers: Optional[Dict] = None, **kwargs) -> Response:
    return _default_session.post(url, data=data, headers=headers, **kwargs)