"""
dpumesh-server - DPU-native WSGI server (Graph-driven version)

Single thread handles multiple concurrent requests.
No blocking - uses event-driven architecture.

Key design:
- Server is GENERIC - knows nothing about specific services
- Application registers egress_graph on ctx:
    List of groups, each group has ordered list of (method, url, headers, body)
- Server drives execution:
    1. Submit first request of each group
    2. On response: check graph, send next in group (or mark group done)
    3. All groups done → finalize
"""

import sys
import os
import time
import importlib
from io import BytesIO
from typing import Callable, Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum

from . import dpu_lib
from .dpu_lib import IngressRequest, IngressResponse, EgressResponse
from concurrent.futures import ThreadPoolExecutor


class RequestState(Enum):
    RECEIVED = 1
    WAITING_EXTERNAL = 3
    READY_TO_RESPOND = 4


@dataclass
class EgressGroup:
    """One group of sequential external service calls"""
    requests: List[Tuple[str, str, Dict, Optional[str]]]  # [(method, url, headers, body), ...]
    current: int = 0                # Index of currently in-flight request
    active_egress_id: int = -1      # egress_req_id of current in-flight request
    done: bool = False
    error: bool = False


@dataclass
class RequestContext:
    """Tracks state of a single client request"""
    req_id: int
    ingress_req: IngressRequest
    state: RequestState = RequestState.RECEIVED
    
    # Timing
    start_time: float = 0.0
    
    # Egress graph: list of groups, each with sequential requests
    # Populated by application (ExternalServiceExecutor)
    egress_graph: List[EgressGroup] = field(default_factory=list)
    
    # Egress tracking: egress_req_id → group_index
    egress_to_group: Dict[int, int] = field(default_factory=dict)
    
    # WSGI artifacts
    environ: Dict = field(default_factory=dict)
    response_status: int = 200
    response_headers: Dict[str, str] = field(default_factory=dict)
    response_body: str = ""


class ThreadingDPUServer:
    """
    Multi-threaded DPU Server for Blocking Applications.
    
    Main Loop: ONLY polls Ingress SQ.
    Workers: Execute WSGI app in a thread.
             Blocking logic (ExternalServiceExecutor) runs inside the thread.
             That logic handles its own Egress SQ/CQ interactions via dpumesh.requests.
    """
    
    def __init__(self, app: Callable, host: str = "0.0.0.0", port: int = 8080, workers: int = 50):
        self.app = app
        self.host = host
        self.port = port
        self.workers = workers
        self._running = False
        self.executor = None
    
    def run(self):
        self._running = True
        self.executor = ThreadPoolExecutor(max_workers=self.workers)
        
        print(f"[dpumesh-server] Starting on {self.host}:{self.port}")
        print(f"[dpumesh-server] Blocking/Threaded mode with {self.workers} workers")
        
        empty_cycles = 0
        POLL_LIMIT = 1000
        
        try:
            while self._running:
                # ONLY Poll Ingress. 
                # Egress polling is done by the worker threads (via dpumesh.requests.wait_egress_response)
                ingress_req = dpu_lib.poll_ingress_sq()
                
                if ingress_req:
                    # Offload to thread immediately
                    self.executor.submit(self._handle_client, ingress_req)
                    empty_cycles = 0
                else:
                    empty_cycles += 1
                    if empty_cycles > POLL_LIMIT:
                        dpu_lib.wait_interrupt() # Wait for interrupt
                        empty_cycles = 0
                        
        finally:
            print("[dpumesh-server] Shutting down executor...")
            if self.executor:
                self.executor.shutdown(wait=False)

    def _handle_client(self, req: IngressRequest):
        """Executed in a worker thread"""
        try:
            environ = self._build_environ(req)
            
            status_code = 200
            response_headers = {}
            body_parts = []
            
            def start_response(status: str, headers: list, exc_info=None):
                nonlocal status_code, response_headers
                try:
                    status_code = int(status.split()[0])
                except ValueError:
                    status_code = 500
                response_headers = dict(headers)
            
            # Execute Flask App (Blocking)
            result = self.app(environ, start_response)
            
            for chunk in result:
                if isinstance(chunk, bytes):
                    body_parts.append(chunk.decode('utf-8', errors='replace'))
                else:
                    body_parts.append(str(chunk))
            
            if hasattr(result, 'close'):
                result.close()
            
            response_body = ''.join(body_parts)
            
            # Send Response back to Ingress CQ
            response = IngressResponse(
                req_id=req.req_id,
                status_code=status_code,
                headers=response_headers,
                body=response_body
            )
            dpu_lib.write_ingress_cq(response)
            
        except Exception as e:
            print(f"[dpumesh-server] Error in worker thread: {e}")
            # Try to send error response
            try:
                err_resp = IngressResponse(
                    req_id=req.req_id,
                    status_code=500,
                    headers={"Content-Type": "application/json"},
                    body='{"message": "Internal Server Error"}'
                )
                dpu_lib.write_ingress_cq(err_resp)
            except:
                pass

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
            'wsgi.multithread': True,   # This is now True
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


class DPUmeshServer:
    """
    Non-blocking WSGI Server with graph-driven egress chaining.
    
    Generic - no knowledge of specific services.
    Application populates ctx.egress_graph, server drives execution.
    """
    
    def __init__(self, app: Callable, host: str = "0.0.0.0", port: int = 8080):
        self.app = app
        self.host = host
        self.port = port
        self._running = False
        self.active_requests: Dict[int, RequestContext] = {}
    
    def run(self):
        """Main event loop"""
        self._running = True
        
        print(f"[dpumesh-server] Starting on {self.host}:{self.port}")
        print(f"[dpumesh-server] Graph-driven non-blocking mode")
        
        empty_cycles = 0
        POLL_LIMIT = 1000
        
        while self._running:
            work_done = False
            
            # Phase 1: New client requests
            ingress_req = dpu_lib.poll_ingress_sq()
            if ingress_req:
                work_done = True
                self._handle_new_request(ingress_req)
            
            # Phase 2: External service responses
            egress_resp = dpu_lib.poll_egress_cq()
            if egress_resp:
                work_done = True
                self._handle_egress_response(egress_resp)
            
            # Phase 3: Finalize completed requests
            self._finalize_ready_requests()
            
            # Idle strategy
            if work_done:
                empty_cycles = 0
            else:
                empty_cycles += 1
                if empty_cycles > POLL_LIMIT:
                    dpu_lib.wait_interrupt()
                    empty_cycles = 0
    
    def _handle_new_request(self, ingress_req: IngressRequest):
        """New request from Ingress SQ → run WSGI app → start egress graph"""
        ctx = RequestContext(
            req_id=ingress_req.req_id,
            ingress_req=ingress_req,
            start_time=time.time()
        )
        ctx.environ = self._build_environ(ingress_req)
        self.active_requests[ctx.req_id] = ctx
        
        # Run WSGI app (populates ctx.egress_graph)
        self._process_wsgi_app(ctx)
        
        # After WSGI returns, start the egress graph
        if ctx.egress_graph:
            self._start_egress_graph(ctx)
            ctx.state = RequestState.WAITING_EXTERNAL
        else:
            ctx.state = RequestState.READY_TO_RESPOND
    
    def _process_wsgi_app(self, ctx: RequestContext):
        """Call WSGI app. App populates ctx.egress_graph during execution."""
        try:
            _set_current_context(ctx)
            
            status_code = 200
            response_headers = {}
            body_parts = []
            
            def start_response(status: str, headers: list, exc_info=None):
                nonlocal status_code, response_headers
                status_code = int(status.split()[0])
                response_headers = dict(headers)
            
            result = self.app(ctx.environ, start_response)
            
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
            
        except Exception as e:
            print(f"[dpumesh-server] Error processing request {ctx.req_id}: {e}")
            ctx.response_status = 500
            ctx.response_body = '{"message": "Internal Server Error"}'
            ctx.response_headers = {"Content-Type": "application/json"}
            ctx.state = RequestState.READY_TO_RESPOND
        finally:
            _clear_current_context()
    
    def _start_egress_graph(self, ctx: RequestContext):
        """Submit first request of each group in parallel"""
        for group_idx, group in enumerate(ctx.egress_graph):
            if not group.requests:
                group.done = True
                continue
            self._submit_group_current(ctx, group_idx)
    
    def _submit_group_current(self, ctx: RequestContext, group_idx: int):
        """Submit the current request for a group"""
        group = ctx.egress_graph[group_idx]
        method, url, headers, body = group.requests[group.current]
        
        egress_req_id = dpu_lib.submit_egress_request(
            method=method,
            url=url,
            headers=headers or {},
            body=body
        )
        
        group.active_egress_id = egress_req_id
        ctx.egress_to_group[egress_req_id] = group_idx
    
    def _handle_egress_response(self, egress_resp: EgressResponse):
        """
        Response arrived. Check graph:
        - Next service in group? → submit it
        - Group done? → mark done
        - All groups done? → finalize
        """
        egress_req_id = egress_resp.req_id
        
        # Find parent request
        ctx = None
        for req_id, c in self.active_requests.items():
            if egress_req_id in c.egress_to_group:
                ctx = c
                break
        
        if ctx is None:
            return
        
        # Find which group this response belongs to
        group_idx = ctx.egress_to_group.pop(egress_req_id, None)
        if group_idx is None:
            return
        
        group = ctx.egress_graph[group_idx]
        
        # Check response status
        if egress_resp.status_code != 200:
            group.done = True
            group.error = True
        else:
            # Advance to next service in group
            group.current += 1
            
            if group.current < len(group.requests):
                # More services → submit next
                self._submit_group_current(ctx, group_idx)
            else:
                # Group complete
                group.done = True
        
        # Check if ALL groups are done
        if all(g.done for g in ctx.egress_graph):
            # Check for errors
            if any(g.error for g in ctx.egress_graph):
                ctx.response_status = 500
                ctx.response_body = '{"message": "Error in external services request"}'
                ctx.response_headers = {"Content-Type": "application/json"}
            ctx.state = RequestState.READY_TO_RESPOND
    
    def _finalize_ready_requests(self):
        """Write responses to Ingress CQ for completed requests."""
        completed = []
        
        for req_id, ctx in self.active_requests.items():
            if ctx.state == RequestState.READY_TO_RESPOND:
                response = IngressResponse(
                    req_id=ctx.req_id,
                    status_code=ctx.response_status,
                    headers=ctx.response_headers,
                    body=ctx.response_body
                )
                dpu_lib.write_ingress_cq(response)
                completed.append(req_id)
        
        for req_id in completed:
            del self.active_requests[req_id]
    
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
            'wsgi.multithread': False,
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


# ===== Thread-local context =====
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
    mode = os.environ.get('DPUMESH_MODE', 'graph').lower()
    
    print(f"[dpumesh-server] Loading app from {app_path}")
    app = load_app(app_path)
    
    if mode == 'blocking':
        # Blocking mode: Use ThreadingDPUServer
        # Use a reasonable number of workers (e.g., 50 or env var)
        workers = int(os.environ.get('DPUMESH_WORKERS', '50'))
        server = ThreadingDPUServer(app, host=host, port=port, workers=workers)
    else:
        # Graph mode: Use DPUmeshServer
        server = DPUmeshServer(app, host=host, port=port)
        set_server(server)
    
    try:
        server.run()
    except KeyboardInterrupt:
        print("\n[dpumesh-server] Shutting down...")
        server.stop()


if __name__ == '__main__':
    main()