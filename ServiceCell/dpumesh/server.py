import sys
import os
import time
import importlib
import multiprocessing
import traceback
from io import BytesIO
from typing import Callable, Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from . import dpu_lib as dlib

# ========================================================================================
# 1. HELPER: APP LOADER
# ========================================================================================
def load_app_from_path(path):
    try:
        if '.' not in sys.path: sys.path.insert(0, '.')
        if '/app' not in sys.path: sys.path.insert(0, '/app')
        
        mod_path, attr = path.rsplit(':', 1) if ':' in path else (path, 'app')
        
        # Handle "ServiceCell." prefix if needed
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            if mod_path.startswith('ServiceCell.'):
                mod = importlib.import_module(mod_path.replace('ServiceCell.', ''))
            else: raise
        
        return getattr(mod, attr)
    except Exception as e:
        print(f"[Loader] Error loading {path}: {e}")
        traceback.print_exc()
        sys.exit(1)

# ========================================================================================
# 2. WORKER PROCESS TARGETS
# ========================================================================================

def _run_blocking_worker(idx, queue, shared_state, app_path, host, port):
    """
    Worker process for Blocking Mode.
    Simulates a Gunicorn worker (thread-based or sync) but uses DPU blocking API.
    """
    try:
        # 1. Setup Environment
        dlib.set_shared_state(shared_state)
        wid = f"block-{idx}-{os.getpid()}"
        dlib.register_worker_with_queue(wid, queue)
        
        # 2. Load App
        app = load_app_from_path(app_path)
        print(f"[Worker-{idx}] Ready (Blocking Mode)", flush=True)
        
        # 3. Main Loop
        while True:
            # Poll global ingress queue (Load Balancing via OS Lock)
            req = dlib.poll_ingress_sq()
            if not req:
                dlib.wait_interrupt()
                continue
                
            # Process Request
            _handle_wsgi(app, req, host, port)
            
    except KeyboardInterrupt: pass
    except Exception as e:
        print(f"[Worker-{idx}] Crash: {e}")
        traceback.print_exc()

def _run_graph_worker(idx, queue, shared_state, app_path, host, port):
    """
    Worker process for Non-Blocking (Graph) Mode.
    Runs an Event Loop (DPUmeshServer).
    """
    try:
        # 1. Setup Environment
        dlib.set_shared_state(shared_state)
        wid = f"graph-{idx}-{os.getpid()}"
        dlib.register_worker_with_queue(wid, queue)
        
        # 2. Load App
        app = load_app_from_path(app_path)
        
        # 3. Start Event Loop Server
        server = DPUmeshServer(app, host, port, worker_id=wid)
        set_server(server) # Register for context access
        
        print(f"[Worker-{idx}] Ready (Graph Mode)", flush=True)
        server.run()
        
    except KeyboardInterrupt: pass
    except Exception as e:
        print(f"[Worker-{idx}] Crash: {e}")
        traceback.print_exc()


def _handle_wsgi(app, req, host, port):
    """Common WSGI handler"""
    try:
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
            k_u = k.upper().replace('-', '_')
            if k_u not in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                environ[f'HTTP_{k_u}'] = v

        status_code = 200
        headers = {}
        def start_response(status, response_headers, exc_info=None):
            nonlocal status_code, headers
            status_code = int(status.split()[0])
            headers = dict(response_headers)

        result = app(environ, start_response)
        body = ''.join([c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in result])
        
        dlib.write_ingress_cq(dlib.IngressResponse(req.req_id, status_code, headers, body))
        
    except Exception as e:
        print(f"[WSGI] Error: {e}")


# ========================================================================================
# 3. SERVER CLASSES
# ========================================================================================

class ProcessDPUServer:
    """Multi-Process Server for BLOCKING applications"""
    def __init__(self, app_path, host, port, workers):
        self.app_path = app_path
        self.host = host
        self.port = port
        self.workers = workers
        
    def run(self):
        # Initialize Engine (Parent)
        dlib.get_instance()
        state = dlib.get_shared_state()
        
        # Create Queues
        manager = multiprocessing.Manager()
        queues = [manager.Queue() for _ in range(self.workers)]
        
        # Spawn Workers
        procs = []
        for i in range(self.workers):
            p = multiprocessing.Process(
                target=_run_blocking_worker,
                args=(i, queues[i], state, self.app_path, self.host, self.port)
            )
            p.start()
            procs.append(p)
            
        print(f"[Server] Started {self.workers} BLOCKING workers.")
        for p in procs: p.join()


class ProcessGraphServer:
    """Multi-Process Server for NON-BLOCKING (Graph) applications"""
    def __init__(self, app_path, host, port, workers):
        self.app_path = app_path
        self.host = host
        self.port = port
        self.workers = workers
        
    def run(self):
        # Initialize Engine (Parent)
        dlib.get_instance()
        state = dlib.get_shared_state()
        
        # Create Queues
        manager = multiprocessing.Manager()
        queues = [manager.Queue() for _ in range(self.workers)]
        
        # Spawn Workers
        procs = []
        for i in range(self.workers):
            p = multiprocessing.Process(
                target=_run_graph_worker,
                args=(i, queues[i], state, self.app_path, self.host, self.port)
            )
            p.start()
            procs.append(p)
            
        print(f"[Server] Started {self.workers} GRAPH workers.")
        for p in procs: p.join()


# ========================================================================================
# 4. DPUMESH SERVER (Event Loop Logic)
# ========================================================================================

class RequestState(Enum):
    RECEIVED = 1
    WAITING_EXTERNAL = 3
    READY_TO_RESPOND = 4

@dataclass
class EgressGroup:
    requests: List[Tuple[str, str, Dict, Optional[str]]]
    current: int = 0
    active_egress_id: str = ""
    done: bool = False
    error: bool = False

@dataclass
class RequestContext:
    req_id: str
    ingress_req: Any
    state: RequestState = RequestState.RECEIVED
    start_time: float = 0.0
    egress_graph: List[EgressGroup] = field(default_factory=list)
    egress_to_group: Dict[str, int] = field(default_factory=dict)
    environ: Dict = field(default_factory=dict)
    response_status: int = 200
    response_headers: Dict[str, str] = field(default_factory=dict)
    response_body: str = ""

class DPUmeshServer:
    """Single-Process Event Loop (Runs inside a Worker Process)"""
    def __init__(self, app, host, port, worker_id):
        self.app = app
        self.host = host
        self.port = port
        self.worker_id = worker_id
        self._running = False
        self.active_requests: Dict[str, RequestContext] = {}
    
    def run(self):
        self._running = True
        while self._running:
            work_done = False
            
            # 1. Ingress
            req = dlib.poll_ingress_sq()
            if req:
                work_done = True
                self._handle_new_request(req)
                
            # 2. Egress Responses
            resp = dlib.poll_egress_cq()
            if resp:
                work_done = True
                self._handle_egress_response(resp)
                
            # 3. Finalize
            self._finalize_ready_requests()
            
            if not work_done: dlib.wait_interrupt()

    def _handle_new_request(self, req):
        ctx = RequestContext(req_id=req.req_id, ingress_req=req, start_time=time.time())
        ctx.environ = self._build_environ(req)
        self.active_requests[ctx.req_id] = ctx
        self._process_wsgi_app(ctx)
        
        if ctx.egress_graph:
            # Start first request of each group
            for i in range(len(ctx.egress_graph)):
                self._submit_group_current(ctx, i)
            ctx.state = RequestState.WAITING_EXTERNAL
        else:
            ctx.state = RequestState.READY_TO_RESPOND

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
        if g.current >= len(g.requests):
            g.done = True
            return

        m, u, h, b = g.requests[g.current]
        eid = dlib.submit_egress_request(m, u, h, b)
        g.active_egress_id = eid
        ctx.egress_to_group[eid] = g_idx

    def _handle_egress_response(self, resp):
        # Find which ctx owns this response
        # In blocking, we just waited. In MP Graph, we look up.
        # But wait, resp.req_id matches the egress req_id.
        # We need to find the ctx that has this egress_id in its map.
        
        target_ctx = None
        target_g_idx = None
        
        for ctx in self.active_requests.values():
            if resp.req_id in ctx.egress_to_group:
                target_ctx = ctx
                target_g_idx = ctx.egress_to_group.pop(resp.req_id)
                break
        
        if not target_ctx: return

        g = target_ctx.egress_graph[target_g_idx]
        
        if resp.status_code != 200:
            g.done = True
            g.error = True
        else:
            g.current += 1
            if g.current < len(g.requests):
                self._submit_group_current(target_ctx, target_g_idx)
            else:
                g.done = True
        
        # Check if all groups are done
        if all(gr.done for gr in target_ctx.egress_graph):
            if any(gr.error for gr in target_ctx.egress_graph):
                target_ctx.response_status = 500
            target_ctx.state = RequestState.READY_TO_RESPOND

    def _finalize_ready_requests(self):
        done_ids = [rid for rid, ctx in self.active_requests.items() if ctx.state == RequestState.READY_TO_RESPOND]
        for rid in done_ids:
            ctx = self.active_requests.pop(rid)
            dlib.write_ingress_cq(dlib.IngressResponse(ctx.req_id, ctx.response_status, ctx.response_headers, ctx.response_body))

    def _build_environ(self, req):
        env = {
            'REQUEST_METHOD': req.method,
            'PATH_INFO': req.path,
            'QUERY_STRING': req.query_string,
            'SERVER_NAME': self.host, 
            'SERVER_PORT': str(self.port), 
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'REMOTE_ADDR': req.remote_addr, 
            'wsgi.input': BytesIO(req.body.encode() if req.body else b''),
            'wsgi.errors': sys.stderr, 
            'wsgi.url_scheme': 'http'
        }
        for k, v in req.headers.items():
            k_u = k.upper().replace('-', '_')
            if k_u not in ('CONTENT_TYPE', 'CONTENT_LENGTH'): 
                env[f'HTTP_{k_u}'] = v
        return env


# ========================================================================================
# 5. ENTRY POINT
# ========================================================================================

# Thread-local context
import threading
_context_local = threading.local()
def _set_current_context(ctx): _context_local.current_ctx = ctx
def _clear_current_context(): _context_local.current_ctx = None
def get_current_context(): return getattr(_context_local, 'current_ctx', None)
def set_server(s): _context_local.server = s

def main():
    if len(sys.argv) < 2:
        print("Usage: python -m dpumesh.server <app_path> [options]")
        sys.exit(1)
        
    app_path = sys.argv[1]
    host = os.environ.get('DPUMESH_HOST', '0.0.0.0')
    port = int(os.environ.get('DPUMESH_PORT', '8080'))
    workers = int(os.environ.get('DPUMESH_WORKERS', '50'))
    mode = os.environ.get('DPUMESH_MODE', 'graph').lower()
    
    if mode == 'blocking':
        server = ProcessDPUServer(app_path, host, port, workers)
    else:
        # Default: Multi-Process Graph Mode
        server = ProcessGraphServer(app_path, host, port, workers)
    
    try: server.run()
    except KeyboardInterrupt: pass

if __name__ == '__main__': main()