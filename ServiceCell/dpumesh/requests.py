"""
dpumesh.requests - DPU-backed HTTP client

For standalone/blocking use only.
In server mode, ExternalServiceExecutor registers egress_graph on ctx directly.

NOTE: Blocking mode (wait_egress_response) is not supported in HW-simulated mode.
      Use graph mode (ctx.call) instead.
"""

import json
from typing import Optional, Dict
from dataclasses import dataclass, field
from . import host_lib as dpu_lib


@dataclass
class Response:
    """Compatible with requests.Response"""
    status_code: int = 0
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    content: bytes = b""

    def json(self):
        return json.loads(self.text)


class Session:
    """
    requests.Session compatible interface (blocking).
    NOTE: Not supported in HW-simulated mode. Use graph mode.
    """

    def __init__(self):
        self._default_headers = {}

    def get(self, url, headers=None, **kwargs):
        return self._request("GET", url, headers=headers, **kwargs)

    def post(self, url, data=None, headers=None, **kwargs):
        return self._request("POST", url, data=data, headers=headers, **kwargs)

    def _request(self, method, url, data=None, headers=None, **kwargs):
        raise NotImplementedError(
            "Blocking HTTP not supported in HW-simulated mode. "
            "Use ctx.call() in graph mode."
        )


_default_session = Session()

def get(url, headers=None, **kwargs):
    return _default_session.get(url, headers=headers, **kwargs)

def post(url, data=None, headers=None, **kwargs):
    return _default_session.post(url, data=data, headers=headers, **kwargs)