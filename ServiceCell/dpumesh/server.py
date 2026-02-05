"""
dpumesh-server - DPU-native WSGI server (Non-blocking version)

Single thread handles multiple concurrent requests.
No blocking - uses event-driven architecture.

Key insight:
- When request A is waiting for external service response,
  we can process request B, C, D...
"""

import sys
import os
import time
import importlib
from io import BytesIO
from typing import Callable, Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum

from . import dpu_lib
from .dpu_lib import IngressRequest, IngressResponse, EgressResponse


class RequestState(Enum):
    """State machine for each request"""
    RECEIVED = 1          # Just received from Ingress SQ
    INTERNAL_DONE = 2     # Internal service completed
    WAITING_EXTERNAL = 3  # Waiting for external service responses
    READY_TO_RESPOND = 4  # All done, ready to write to Ingress CQ


@dataclass
class RequestContext:
    """Tracks state of a single client request"""
    req_id: int
    ingress_req: IngressRequest
    state: RequestState = RequestState.RECEIVED
    
    # Timing
    start_time: float = 0.0
    
    # Internal service result
    internal_body: str = ""
    
    # External service tracking
    pending_egress: Dict[int, str] = field(default_factory=dict)  # egress_req_id → service_name
    completed_responses: Dict[str, Any] = field(default_factory=dict)  # service_name → response
    external_errors: Dict[str, Exception] = field(default_factory=dict)
    
    # WSGI artifacts
    environ: Dict = field(default_factory=dict)
    response_status: int = 200
    response_headers: Dict[str, str] = field(default_factory=dict)
    response_body: str = ""


class DPUmeshServer:
    """
    Non-blocking WSGI Server
    
    Single thread handles multiple concurrent requests using event loop.
    """
    
    def __init__(self, app: Callable, host: str = "0.0.0.0", port: int = 8080):
        self.app = app
        self.host = host
        self.port = port
        self._running = False
        
        # Active requests: req_id → RequestContext
        self.active_requests: Dict[int, RequestContext] = {}
        
        # Egress tracking: egress_req_id → parent_req_id
        self.egress_to_parent: Dict[int, int] = {}
    
    def run(self):
        """Main event loop - single thread, no blocking"""
        self._running = True
        
        print(f"[dpumesh-server] Starting on {self.host}:{self.port}")
        print(f"[dpumesh-server] Non-blocking mode: single thread, multiple requests")
        
        empty_cycles = 0
        POLL_LIMIT = 1000
        
        while self._running:
            work_done = False
            
            # ===== Phase 1: Check for NEW client requests =====
            ingress_req = dpu_lib.poll_ingress_sq()
            if ingress_req:
                work_done = True
                self._handle_new_request(ingress_req)
            
            # ===== Phase 2: Check for external service responses =====
            egress_resp = dpu_lib.poll_egress_cq()
            if egress_resp:
                work_done = True
                self._handle_egress_response(egress_resp)
            
            # ===== Phase 3: Finalize completed requests =====
            self._finalize_ready_requests()
            
            # ===== Idle strategy =====
            if work_done:
                empty_cycles = 0
            else:
                empty_cycles += 1
                if empty_cycles > POLL_LIMIT:
                    dpu_lib.wait_interrupt()
                    empty_cycles = 0
    
    def _handle_new_request(self, ingress_req: IngressRequest):
        """
        New request arrived from Ingress SQ.
        Run internal service, submit external requests, return immediately.
        """
        ctx = RequestContext(
            req_id=ingress_req.req_id,
            ingress_req=ingress_req,
            start_time=time.time()
        )
        
        # Build WSGI environ
        ctx.environ = self._build_environ(ingress_req)
        
        # Store in active requests
        self.active_requests[ctx.req_id] = ctx
        
        # Call Flask app with special non-blocking handler
        self._process_wsgi_app(ctx)
    
    def _process_wsgi_app(self, ctx: RequestContext):
        """
        Call WSGI app.
        
        The app will call dpumesh.requests internally, which:
        1. Submits to Egress SQ (non-blocking)
        2. Returns a Future-like object
        
        But wait - current Flask code expects blocking requests.get()!
        
        Solution: We need to intercept the external calls differently.
        For now, we run the WSGI app and track egress submissions.
        """
        try:
            # Set current context for dpumesh.requests to find
            _set_current_context(ctx)
            
            response_started = False
            status_code = 200
            response_headers = {}
            body_parts = []
            
            def start_response(status: str, headers: list, exc_info=None):
                nonlocal response_started, status_code, response_headers
                response_started = True
                status_code = int(status.split()[0])
                response_headers = dict(headers)
            
            # Call WSGI app
            # During this call, dpumesh.requests will submit to Egress SQ
            # and register pending requests in ctx.pending_egress
            result = self.app(ctx.environ, start_response)
            
            # Collect body
            for chunk in result:
                if isinstance(chunk, bytes):
                    body_parts.append(chunk.decode('utf-8', errors='replace'))
                else:
                    body_parts.append(str(chunk))
            
            if hasattr(result, 'close'):
                result.close()
            
            ctx.response_body = ''.join(body_parts)
            ctx.response_status = status_code
            ctx.response_headers = response_headers
            
            # Check if we're waiting for external services
            if ctx.pending_egress:
                ctx.state = RequestState.WAITING_EXTERNAL
            else:
                ctx.state = RequestState.READY_TO_RESPOND
                
        except Exception as e:
            print(f"[dpumesh-server] Error processing request {ctx.req_id}: {e}")
            ctx.response_status = 500
            ctx.response_body = '{"message": "Internal Server Error"}'
            ctx.response_headers = {"Content-Type": "application/json"}
            ctx.state = RequestState.READY_TO_RESPOND
        finally:
            _clear_current_context()
    
    def _handle_egress_response(self, egress_resp: EgressResponse):
        """
        External service response arrived.
        Find parent request and update its state.
        """
        egress_req_id = egress_resp.req_id
        
        # Find parent request
        parent_req_id = self.egress_to_parent.get(egress_req_id)
        if parent_req_id is None:
            print(f"[dpumesh-server] Warning: orphaned egress response {egress_req_id}")
            return
        
        ctx = self.active_requests.get(parent_req_id)
        if ctx is None:
            print(f"[dpumesh-server] Warning: parent request {parent_req_id} not found")
            return
        
        # Get service name and remove from pending
        service_name = ctx.pending_egress.pop(egress_req_id, None)
        del self.egress_to_parent[egress_req_id]
        
        # Store response
        if egress_resp.status_code == 200:
            ctx.completed_responses[service_name] = egress_resp
        else:
            ctx.external_errors[service_name] = Exception(f"status: {egress_resp.status_code}")
        
        # Check if all external calls done
        if not ctx.pending_egress:
            ctx.state = RequestState.READY_TO_RESPOND
    
    def _finalize_ready_requests(self):
        """
        Write responses to Ingress CQ for completed requests.
        """
        completed = []
        
        for req_id, ctx in self.active_requests.items():
            if ctx.state == RequestState.READY_TO_RESPOND:
                # Write to Ingress CQ
                response = IngressResponse(
                    req_id=ctx.req_id,
                    status_code=ctx.response_status,
                    headers=ctx.response_headers,
                    body=ctx.response_body
                )
                dpu_lib.write_ingress_cq(response)
                completed.append(req_id)
                
                latency = (time.time() - ctx.start_time) * 1000
                # print(f"[dpumesh-server] Request {req_id} completed in {latency:.2f}ms")
        
        # Cleanup
        for req_id in completed:
            del self.active_requests[req_id]
    
    def register_egress_request(self, ctx: RequestContext, egress_req_id: int, service_name: str):
        """Called by dpumesh.requests when submitting external request"""
        ctx.pending_egress[egress_req_id] = service_name
        self.egress_to_parent[egress_req_id] = ctx.req_id
    
    def _build_environ(self, req: IngressRequest) -> Dict[str, Any]:
        """Convert IngressRequest to WSGI environ dict"""
        body_bytes = req.body.encode() if req.body else b''
        body_stream = BytesIO(body_bytes)
        
        environ = {
            'REQUEST_METHOD': req.method,
            'SCRIPT_NAME': '',
            'PATH_INFO': req.path,
            'QUERY_STRING': req.query_string,
            'SERVER_NAME': self.host,
            'SERVER_PORT': str(self.port),
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'REMOTE_ADDR': req.remote_addr,
            'wsgi.version': (1, 0),
            'wsgi.url_scheme': 'http',
            'wsgi.input': body_stream,
            'wsgi.errors': sys.stderr,
            'wsgi.multithread': False,  # Single thread!
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
            'CONTENT_LENGTH': str(len(body_bytes)),
            'CONTENT_TYPE': req.headers.get('Content-Type', ''),
        }
        
        for key, value in req.headers.items():
            key_upper = key.upper().replace('-', '_')
            if key_upper not in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                environ[f'HTTP_{key_upper}'] = value
        
        return environ
    
    def stop(self):
        self._running = False


# ===== Thread-local context for dpumesh.requests =====
import threading
_context_local = threading.local()

def _set_current_context(ctx: RequestContext):
    _context_local.current_ctx = ctx

def _clear_current_context():
    _context_local.current_ctx = None

def get_current_context() -> Optional[RequestContext]:
    return getattr(_context_local, 'current_ctx', None)

def get_server() -> Optional[DPUmeshServer]:
    return getattr(_context_local, 'server', None)

def set_server(server: DPUmeshServer):
    _context_local.server = server


# ===== CLI =====
def load_app(app_path: str) -> Callable:
    if ':' in app_path:
        module_path, attr_name = app_path.rsplit(':', 1)
    else:
        module_path = app_path
        attr_name = 'app'
    
    module_path = module_path.replace('-', '_')
    
    if '.' not in sys.path:
        sys.path.insert(0, '.')
    
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)


def main():
    if len(sys.argv) < 2:
        print("Usage: dpumesh-server <module:app>")
        sys.exit(1)
    
    app_path = sys.argv[1]
    host = os.environ.get('DPUMESH_HOST', '0.0.0.0')
    port = int(os.environ.get('DPUMESH_PORT', '8080'))
    
    print(f"[dpumesh-server] Loading app from {app_path}")
    app = load_app(app_path)
    
    server = DPUmeshServer(app, host=host, port=port)
    set_server(server)
    
    try:
        server.run()
    except KeyboardInterrupt:
        print("\n[dpumesh-server] Shutting down...")
        server.stop()


if __name__ == '__main__':
    main()