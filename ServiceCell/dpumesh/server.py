"""
dpumesh.server - Worker Server

source_worker: 요청을 보낸 Worker ("" = 외부)
dest_worker: 요청을 받을 Worker ("" = 외부)

| Type             | source      | dest        |
|------------------|-------------|-------------|
| TCP Bridge 요청  | ""          | "s0-xxx"    |
| TCP Bridge 응답  | "s0-xxx"    | ""          |
| Egress 요청      | "s0-xxx"    | "s1-yyy"    |
| Egress 응답      | "s1-yyy"    | "s0-xxx"    |
"""

import sys
import os
import time
import uuid
import importlib
import multiprocessing
import traceback
from io import BytesIO
from typing import Callable, Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from . import host_lib as dlib
from .common import IngressResponse, OpType


# ========================================================================================
# 1. HELPER: APP LOADER
# ========================================================================================
def load_app_from_path(path):
    try:
        if '.' not in sys.path: sys.path.insert(0, '.')
        if '/app' not in sys.path: sys.path.insert(0, '/app')
        
        mod_path, attr = path.rsplit(':', 1) if ':' in path else (path, 'app')
        
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
    """Blocking Mode Worker - polling 스레드 사용"""
    try:
        wid = f"block-{idx}-{os.getpid()}"
        dlib.init_worker(wid, start_poller=True)  # Blocking: poller ON
        
        app = load_app_from_path(app_path)
        print(f"[Worker-{idx}] Ready (Blocking Mode)", flush=True)
        
        while True:
            req = dlib.poll_ingress_sq()
            if not req:
                dlib.wait_interrupt()
                continue
            
            _handle_wsgi(app, req, host, port)
            
    except KeyboardInterrupt: pass
    except Exception as e:
        print(f"[Worker-{idx}] Crash: {e}")
        traceback.print_exc()


def _run_graph_worker(idx, queue, shared_state, app_path, host, port):
    """Graph Mode Worker - SHM 직접 읽기"""
    try:
        wid = f"graph-{idx}-{os.getpid()}"
        dlib.init_worker(wid, start_poller=False)  # Graph: poller OFF
        
        app = load_app_from_path(app_path)
        
        server = DPUmeshServer(app, host, port, worker_id=wid)
        set_server(server)
        
        print(f"[Worker-{idx}] Ready (Graph Mode)", flush=True)
        server.run()
        
    except KeyboardInterrupt: pass
    except Exception as e:
        print(f"[Worker-{idx}] Crash: {e}")
        traceback.print_exc()


def _handle_wsgi(app, req, host, port):
    """Common WSGI handler for blocking mode"""
    from .common import IngressResponse
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
        
        # 응답: source=현재Worker, dest=요청보낸곳 (source_worker)
        source = dlib.get_worker_id()
        dest = getattr(req, 'source_worker', '') or ''
        
        dlib.write_ingress_cq(
            IngressResponse(req.req_id, status_code, headers, body),
            source_worker=source,
            dest_worker=dest
        )
        
    except Exception as e:
        print(f"[WSGI] Error: {e}")
        traceback.print_exc()


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
        manager = multiprocessing.Manager()
        queues = [manager.Queue() for _ in range(self.workers)]
        
        procs = []
        for i in range(self.workers):
            p = multiprocessing.Process(
                target=_run_blocking_worker,
                args=(i, queues[i], None, self.app_path, self.host, self.port)
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
        manager = multiprocessing.Manager()
        queues = [manager.Queue() for _ in range(self.workers)]
        
        procs = []
        for i in range(self.workers):
            p = multiprocessing.Process(
                target=_run_graph_worker,
                args=(i, queues[i], None, self.app_path, self.host, self.port)
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
    """그룹 내 서비스들은 순차 실행, 그룹 간은 병렬 실행"""
    requests: List[Tuple[str, str, Dict, Optional[str]]]  # (method, url, headers, body)
    current: int = 0
    active_egress_id: str = ""
    done: bool = False
    error: bool = False


@dataclass
class RequestContext:
    """Ingress 요청 컨텍스트"""
    req_id: str
    ingress_req: Any
    state: RequestState = RequestState.RECEIVED
    start_time: float = 0.0
    egress_graph: List[EgressGroup] = field(default_factory=list)
    
    # egress_req_id → group_idx 매핑
    egress_to_group: Dict[str, int] = field(default_factory=dict)
    
    # 원본 요청자 (응답할 때 dest로)
    source_worker: str = ""
    
    environ: Dict = field(default_factory=dict)
    response_status: int = 200
    response_headers: Dict[str, str] = field(default_factory=dict)
    response_body: str = ""


class DPUmeshServer:
    """Single-Process Event Loop - worker_sq에서 직접 읽기"""
    
    def __init__(self, app, host, port, worker_id):
        self.app = app
        self.host = host
        self.port = port
        self.worker_id = worker_id
        self._running = False
        
        # worker_sq 직접 참조
        self._worker_sq = dlib.get_worker_sq()
        
        # ingress_req_id → RequestContext
        self.active_requests: Dict[str, RequestContext] = {}
        
        # egress_req_id → ingress_req_id
        self.egress_to_ingress: Dict[str, str] = {}
    
    def run(self):
        self._running = True
        while self._running:
            work_done = False
            
            # SHM에서 직접 읽기 — REQUEST/RESPONSE 구분
            entry = self._worker_sq.get()
            if entry:
                work_done = True
                if entry.op_type == OpType.REQUEST:
                    self._handle_new_request(entry)
                elif entry.op_type == OpType.RESPONSE:
                    self._handle_egress_response(entry)
            
            # 완료된 요청 응답
            self._finalize_ready_requests()
            
            if not work_done:
                dlib.wait_interrupt()

    def _handle_new_request(self, req):
        """새 Ingress 요청 처리"""
        ctx = RequestContext(
            req_id=req.req_id,
            ingress_req=req,
            start_time=time.time(),
            source_worker=getattr(req, 'source_worker', '') or ''
        )
        ctx.environ = self._build_environ(req)
        self.active_requests[ctx.req_id] = ctx
        
        # Flask 앱 실행
        self._process_wsgi_app(ctx)
        
        if ctx.egress_graph:
            for i in range(len(ctx.egress_graph)):
                self._submit_group_current(ctx, i)
            ctx.state = RequestState.WAITING_EXTERNAL
        else:
            ctx.state = RequestState.READY_TO_RESPOND

    def _process_wsgi_app(self, ctx):
        """WSGI 앱 실행"""
        _set_current_context(ctx)
        
        def start_response(status, headers, exc_info=None):
            ctx.response_status = int(status.split()[0])
            ctx.response_headers = dict(headers)
        
        result = self.app(ctx.environ, start_response)
        ctx.response_body = ''.join([
            c.decode('utf-8') if isinstance(c, bytes) else str(c) 
            for c in result
        ])
        
        _clear_current_context()

    def _submit_group_current(self, ctx: RequestContext, g_idx: int):
        """그룹의 현재 요청 제출"""
        g = ctx.egress_graph[g_idx]
        
        if g.current >= len(g.requests):
            g.done = True
            return
        
        method, url, headers, body = g.requests[g.current]
        
        # 새 egress_req_id 생성
        egress_req_id = str(uuid.uuid4())
        
        # 매핑 저장
        g.active_egress_id = egress_req_id
        ctx.egress_to_group[egress_req_id] = g_idx
        self.egress_to_ingress[egress_req_id] = ctx.req_id
        
        # 요청 전송 (source=현재Worker, dest=Sidecar가 결정)
        dlib.submit_egress_request(method, url, headers, body, egress_req_id)
        
        print(f"[Server] Egress {egress_req_id[:8]} for ingress {ctx.req_id[:8]} → {url}", flush=True)

    def _handle_egress_response(self, resp):
        """Egress 응답 처리"""
        egress_req_id = resp.req_id
        
        ingress_req_id = self.egress_to_ingress.pop(egress_req_id, None)
        if not ingress_req_id:
            print(f"[Server] Unknown egress response: {egress_req_id[:8]}", flush=True)
            return
        
        ctx = self.active_requests.get(ingress_req_id)
        if not ctx:
            print(f"[Server] No context for ingress: {ingress_req_id[:8]}", flush=True)
            return
        
        g_idx = ctx.egress_to_group.pop(egress_req_id, None)
        if g_idx is None:
            print(f"[Server] No group for egress: {egress_req_id[:8]}", flush=True)
            return
        
        g = ctx.egress_graph[g_idx]
        
        print(f"[Server] Egress response {egress_req_id[:8]} status={resp.status_code}", flush=True)
        
        if resp.status_code != 200:
            g.done = True
            g.error = True
        else:
            g.current += 1
            if g.current < len(g.requests):
                self._submit_group_current(ctx, g_idx)
            else:
                g.done = True
        
        if all(gr.done for gr in ctx.egress_graph):
            if any(gr.error for gr in ctx.egress_graph):
                ctx.response_status = 500
            ctx.state = RequestState.READY_TO_RESPOND

    def _finalize_ready_requests(self):
        """완료된 요청 응답 전송"""
        done_ids = [
            rid for rid, ctx in self.active_requests.items() 
            if ctx.state == RequestState.READY_TO_RESPOND
        ]
        
        for rid in done_ids:
            ctx = self.active_requests.pop(rid)
            
            # 남은 매핑 정리
            for egress_id in list(ctx.egress_to_group.keys()):
                self.egress_to_ingress.pop(egress_id, None)
            
            # 응답: source=현재Worker, dest=요청보낸곳
            dlib.write_ingress_cq(
                IngressResponse(
                    ctx.req_id, 
                    ctx.response_status, 
                    ctx.response_headers, 
                    ctx.response_body
                ),
                source_worker=self.worker_id,
                dest_worker=ctx.source_worker  # 원본 요청자 (""이면 외부)
            )
            
            elapsed = (time.time() - ctx.start_time) * 1000
            print(f"[Server] Completed {ctx.req_id[:8]} in {elapsed:.1f}ms", flush=True)

    def _build_environ(self, req):
        """WSGI environ 생성"""
        body_bytes = req.body.encode() if req.body else b''

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
            'wsgi.url_scheme': 'http',
            'wsgi.multithread': False,
            'wsgi.multiprocess': True,
            'wsgi.run_once': False,
            'CONTENT_LENGTH': str(len(body_bytes)),
            'CONTENT_TYPE': req.headers.get('Content-Type', ''),
        }
        
        for k, v in req.headers.items():
            k_u = k.upper().replace('-', '_')
            if k_u not in ('CONTENT_TYPE', 'CONTENT_LENGTH'): 
                env[f'HTTP_{k_u}'] = v
        
        return env


# ========================================================================================
# 5. CONTEXT & ENTRY POINT
# ========================================================================================

import threading
_context_local = threading.local()

def _set_current_context(ctx): 
    _context_local.current_ctx = ctx

def _clear_current_context(): 
    _context_local.current_ctx = None

def get_current_context(): 
    return getattr(_context_local, 'current_ctx', None)

def set_server(s): 
    _context_local.server = s


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
        server = ProcessGraphServer(app_path, host, port, workers)
    
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()