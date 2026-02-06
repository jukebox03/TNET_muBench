import sys
import os
import time
import importlib
from io import BytesIO
from typing import Callable, Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

# 1. Choose the correct engine EARLY
if os.environ.get('DPUMESH_MP_MODE') == '1':
    from . import dpu_lib_mp as dlib
else:
    from . import dpu_lib as dlib

# Helper function must be top-level for pickling in multiprocessing
def _process_worker_handler(req_data, app_path, host, port, shared_state):
    try:
        import sys
        import os
        import importlib
        from io import BytesIO
        
        # Child process MUST use the MP engine
        os.environ['DPUMESH_MP_MODE'] = '1'
        from . import dpu_lib_mp as dlib_worker
        dlib_worker.set_shared_state(shared_state)
        
        # Load App
        module_attr = app_path.rsplit(':', 1)
        module_path = module_attr[0]
        attr_name = module_attr[1] if len(module_attr) > 1 else 'app'
        
        if '.' not in sys.path: sys.path.insert(0, '.')
        if '/app' not in sys.path: sys.path.insert(0, '/app')

        try:
            module = importlib.import_module(module_path)
        except ImportError:
            if module_path.startswith('ServiceCell.'):
                module = importlib.import_module(module_path.replace('ServiceCell.', ''))
            else: raise

        app = getattr(module, attr_name)
        req = req_data
        
        # Build WSGI Environ
        environ = {
            'REQUEST_METHOD': req.method,
            'PATH_INFO': req.path,
            'QUERY_STRING': req.query_string,
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'REMOTE_ADDR': req.remote_addr,
            'SERVER_NAME': host,
            'SERVER_PORT': str(port),
            'wsgi.input': BytesIO(req.body.encode() if req.body else b''),
            'wsgi.errors': sys.stderr,
            'CONTENT_LENGTH': str(len(req.body.encode() if req.body else b'')),
            'CONTENT_TYPE': req.headers.get('Content-Type', ''),
            'wsgi.multithread': False,
            'wsgi.multiprocess': True,
            'wsgi.run_once': False,
            'wsgi.url_scheme': 'http',
        }
        for k, v in req.headers.items():
            k_upper = k.upper().replace('-', '_')
            if k_upper not in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                environ[f'HTTP_{k_upper}'] = v

        status_code = 200
        headers = {}
        def start_response(status, response_headers, exc_info=None):
            nonlocal status_code, headers
            status_code = int(status.split()[0])
            headers = dict(response_headers)

        result = app(environ, start_response)
        body = ''.join([c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in result])
        
        # Send Response back via shared queue
        dlib_worker.write_ingress_cq(dlib_worker.IngressResponse(req.req_id, status_code, headers, body))
        
    except Exception as e:
        import traceback
        print(f"[WORKER-ERROR] Req {req_data.req_id if req_data else '??'}: {e}")
        traceback.print_exc()


class ProcessDPUServer:
    def __init__(self, app_path: str, host: str, port: int, workers: int):
        self.app_path = app_path
        self.host = host
        self.port = port
        self.workers = workers
        self._running = False
        # Get shared state from the MP engine
        self.shared_state = dlib.get_shared_state()
    
    def run(self):
        self._running = True
        
        # FORCE INIT: Ensure Bridge Thread starts immediately
        print("[dpumesh-server] Initializing DPULibMP engine...", flush=True)
        if hasattr(dlib, 'get_instance'):
            dlib.get_instance()
        else:
            # Fallback for dpu_lib (threading) or if get_instance is not exposed
            dlib.poll_ingress_sq()
            
        print(f"[dpumesh-server] Starting ProcessDPUServer (MP) with {self.workers} workers", flush=True)
        
        with ProcessPoolExecutor(max_workers=self.workers) as executor:
            while self._running:
                req = dlib.poll_ingress_sq()
                if req:
                    executor.submit(_process_worker_handler, req, self.app_path, self.host, self.port, self.shared_state)
                else:
                    dlib.wait_interrupt()
    def stop(self): self._running = False


class RequestState(Enum):
    RECEIVED = 1
    WAITING_EXTERNAL = 3
    READY_TO_RESPOND = 4

@dataclass
class EgressGroup:
    requests: List[Tuple[str, str, Dict, Optional[str]]]
    current: int = 0
    active_egress_id: int = -1
    done: bool = False
    error: bool = False

@dataclass
class RequestContext:
    req_id: int
    ingress_req: Any
    state: RequestState = RequestState.RECEIVED
    start_time: float = 0.0
    egress_graph: List[EgressGroup] = field(default_factory=list)
    egress_to_group: Dict[int, int] = field(default_factory=dict)
    environ: Dict = field(default_factory=dict)
    response_status: int = 200
    response_headers: Dict[str, str] = field(default_factory=dict)
    response_body: str = ""

class DPUmeshServer:
    def __init__(self, app: Callable, host: str = "0.0.0.0", port: int = 8080):
        self.app = app
        self.host = host
        self.port = port
        self._running = False
        self.active_requests: Dict[int, RequestContext] = {}
    
    def run(self):
        self._running = True
        print(f"[dpumesh-server] Starting DPUmeshServer (Graph) on {self.host}:{self.port}")
        while self._running:
            work_done = False
            req = dlib.poll_ingress_sq()
            if req:
                work_done = True
                self._handle_new_request(req)
            resp = dlib.poll_egress_cq()
            if resp:
                work_done = True
                self._handle_egress_response(resp)
            self._finalize_ready_requests()
            if not work_done: dlib.wait_interrupt()

    def _handle_new_request(self, req):
        ctx = RequestContext(req_id=req.req_id, ingress_req=req, start_time=time.time())
        ctx.environ = self._build_environ(req)
        self.active_requests[ctx.req_id] = ctx
        self._process_wsgi_app(ctx)
        if ctx.egress_graph:
            for i in range(len(ctx.egress_graph)): self._submit_group_current(ctx, i)
            ctx.state = RequestState.WAITING_EXTERNAL
        else: ctx.state = RequestState.READY_TO_RESPOND

    def _process_wsgi_app(self, ctx):
        _set_current_context(ctx)
        def start_response(status, headers, exc_info=None):
            ctx.response_status = int(status.split()[0])
            ctx.response_headers = dict(headers)
        result = self.app(ctx.environ, start_response)
        ctx.response_body = ''.join([c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in result])
        _clear_current_context()

    def _submit_group_current(self, ctx, g_idx):
        g = ctx.egress_graph[g_idx]
        m, u, h, b = g.requests[g.current]
        eid = dlib.submit_egress_request(m, u, h, b)
        g.active_egress_id = eid
        ctx.egress_to_group[eid] = g_idx

    def _handle_egress_response(self, resp):
        ctx = next((c for c in self.active_requests.values() if resp.req_id in c.egress_to_group), None)
        if not ctx: return
        g_idx = ctx.egress_to_group.pop(resp.req_id)
        g = ctx.egress_graph[g_idx]
        if resp.status_code != 200: g.done = g.error = True
        else:
            g.current += 1
            if g.current < len(g.requests): self._submit_group_current(ctx, g_idx)
            else: g.done = True
        if all(gr.done for gr in ctx.egress_graph):
            if any(gr.error for gr in ctx.egress_graph): ctx.response_status = 500
            ctx.state = RequestState.READY_TO_RESPOND

    def _finalize_ready_requests(self):
        done = [rid for rid, ctx in self.active_requests.items() if ctx.state == RequestState.READY_TO_RESPOND]
        for rid in done:
            ctx = self.active_requests.pop(rid)
            dlib.write_ingress_cq(dlib.IngressResponse(ctx.req_id, ctx.response_status, ctx.response_headers, ctx.response_body))

    def _build_environ(self, req):
        env = {'REQUEST_METHOD': req.method, 'PATH_INFO': req.path, 'QUERY_STRING': req.query_string,
               'SERVER_NAME': self.host, 'SERVER_PORT': str(self.port), 'SERVER_PROTOCOL': 'HTTP/1.1',
               'REMOTE_ADDR': req.remote_addr, 'wsgi.input': BytesIO(req.body.encode() if req.body else b''),
               'wsgi.errors': sys.stderr, 'wsgi.url_scheme': 'http'}
        for k, v in req.headers.items():
            k_u = k.upper().replace('-', '_')
            if k_u not in ('CONTENT_TYPE', 'CONTENT_LENGTH'): env[f'HTTP_{k_u}'] = v
        return env
    def stop(self): self._running = False

# Thread-local context
import threading
_context_local = threading.local()
def _set_current_context(ctx): _context_local.current_ctx = ctx
def _clear_current_context(): _context_local.current_ctx = None
def get_current_context(): return getattr(_context_local, 'current_ctx', None)
def set_server(s): _context_local.server = s

def load_app(path):
    mod_path, attr = path.rsplit(':', 1) if ':' in path else (path, 'app')
    if '.' not in sys.path: sys.path.insert(0, '.')
    return getattr(importlib.import_module(mod_path.replace('-', '_')), attr)

def main():
    if len(sys.argv) < 2: sys.exit(1)
    app_path = sys.argv[1]
    host = os.environ.get('DPUMESH_HOST', '0.0.0.0')
    port = int(os.environ.get('DPUMESH_PORT', '8080'))
    mode = os.environ.get('DPUMESH_MODE', 'graph').lower()
    
    if mode == 'blocking':
        server = ProcessDPUServer(app_path, host, port, int(os.environ.get('DPUMESH_WORKERS', '50')))
    else:
        server = DPUmeshServer(load_app(app_path), host, port)
        set_server(server)
    
    try: server.run()
    except KeyboardInterrupt: server.stop()

if __name__ == '__main__': main()
