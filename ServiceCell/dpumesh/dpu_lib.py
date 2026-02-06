"""
dpu_lib v3 - Unified Multi-Process Hardware Abstraction Layer

This library now supports both Single-Process (Threaded) and Multi-Process (MP) modes transparently.
It simulates the DPU hardware in the Main Process, while allowing multiple Worker Processes
to submit requests and receive responses via dedicated IPC queues.

Architecture:
  [Worker Process N]      [Main Process / DPU Simulation]
      |                       |
      | register_worker(N) -> | Creates Queue[N]
      |                       |
      | submit_egress() ----> | _egress_req_q (Global)
      |                       |       |
      |                       |   [Egress Dispatcher] -> Host SQ -> DMA -> Proxy
      |                       |                                        |
      |                       |   [Egress Collector]  <- Host CQ <- DMA <- Proxy
      |                       |       |
      | <---- Queue[N] <----- | (Routes response to correct Worker Queue)
      |                       |

"""

import threading
import multiprocessing
import socket
import json
import time
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from queue import Queue, Empty
from enum import Enum


# ========================================================================================
# 1. SHARED DATA STRUCTURES (Metadata)
# ========================================================================================

class OpType(Enum):
    REQUEST = 1
    RESPONSE = 2

@dataclass
class SQEntry:
    req_id: str  # Changed to string to support UUIDs
    data_id: int
    op_type: OpType
    method: str = ""
    path: str = ""
    url: str = ""

@dataclass
class CQEntry:
    req_id: str
    data_id: int
    op_type: OpType
    status_code: int = 200

@dataclass
class BufferEntry:
    data_id: int
    headers: Dict[str, str]
    body: Optional[str]
    query_string: str = ""
    remote_addr: str = ""

# Legacy-compatible dataclasses
@dataclass
class IngressRequest:
    req_id: str
    method: str
    path: str
    headers: Dict[str, str]
    body: Optional[str]
    query_string: str
    remote_addr: str

@dataclass
class IngressResponse:
    req_id: str
    status_code: int
    headers: Dict[str, str]
    body: str

@dataclass
class EgressResponse:
    req_id: str
    status_code: int
    headers: Dict[str, str]
    body: str


# ========================================================================================
# 2. INTERNAL COMPONENTS (Run in Main Process / DPU Side)
# ========================================================================================

class InterruptController:
    """Simulates hardware interrupt (Threading based, used inside DPU simulation)"""
    def __init__(self, name: str):
        self._event = threading.Event()
    def signal(self): self._event.set()
    def wait(self, timeout: float = 0.01) -> bool:
        ret = self._event.wait(timeout=timeout)
        self._event.clear()
        return ret

class BufferPool:
    """Shared memory buffer pool (Thread-safe dict)"""
    def __init__(self, name: str):
        self.name = name
        self._buffers: Dict[int, BufferEntry] = {}
        self._lock = threading.Lock()
    def write(self, entry: BufferEntry):
        with self._lock: self._buffers[entry.data_id] = entry
    def read(self, data_id: int) -> Optional[BufferEntry]:
        with self._lock: return self._buffers.get(data_id)
    def remove(self, data_id: int):
        with self._lock: self._buffers.pop(data_id, None)

class DMAManager:
    """Transfers data between Host BP and Sidecar BP"""
    def __init__(self, host_sq, host_cq, host_bp, sidecar_cq, sidecar_sq, sidecar_bp, proxy_int, app_int):
        self.host_sq = host_sq
        self.host_cq = host_cq
        self.host_bp = host_bp
        self.sidecar_cq = sidecar_cq
        self.sidecar_sq = sidecar_sq
        self.sidecar_bp = sidecar_bp
        self.proxy_int = proxy_int
        self.app_int = app_int
        self._running = True
        threading.Thread(target=self._run, daemon=True, name="DMA-Manager").start()

    def _run(self):
        while self._running:
            work = False
            # Host -> Sidecar
            try:
                e = self.host_sq.get_nowait()
                self._transfer_h2s(e)
                work = True
            except Empty: pass
            
            # Sidecar -> Host
            try:
                e = self.sidecar_sq.get_nowait()
                self._transfer_s2h(e)
                work = True
            except Empty: pass
            
            if not work: time.sleep(0.0001)

    def _transfer_h2s(self, sq: SQEntry):
        buf = self.host_bp.read(sq.data_id)
        if not buf: return
        self.sidecar_bp.write(BufferEntry(sq.data_id, buf.headers, buf.body, buf.query_string, buf.remote_addr))
        self.sidecar_cq.put(CQEntry(sq.req_id, sq.data_id, sq.op_type))
        self.proxy_int.signal()
        self.host_bp.remove(sq.data_id)

    def _transfer_s2h(self, sq: SQEntry):
        buf = self.sidecar_bp.read(sq.data_id)
        if not buf: return
        self.host_bp.write(BufferEntry(sq.data_id, buf.headers, buf.body, buf.query_string, buf.remote_addr))
        self.host_cq.put(CQEntry(sq.req_id, sq.data_id, sq.op_type))
        self.app_int.signal()
        self.sidecar_bp.remove(sq.data_id)


# ========================================================================================
# 3. GLOBAL SHARED OBJECTS (Multiprocessing IPC)
# ========================================================================================

_mp_manager = None
_global_ingress_q = None       # TCP Bridge -> Any Worker
_global_egress_req_q = None    # Any Worker -> DPU Host SQ
_global_bridge_tx_q = None     # Any Worker -> TCP Bridge (Response)
_worker_queues = {}            # WorkerID -> Response Queue (Local proxy in child, MP Queue in parent)
_bridge_running = False        # Flag to ensure bridge threads start only once

def _ensure_mp_init():
    """Initialize MP shared objects. Called ONCE by the Main Process."""
    global _mp_manager, _global_ingress_q, _global_egress_req_q, _global_bridge_tx_q
    if _mp_manager is None:
        _mp_manager = multiprocessing.Manager()
        _global_ingress_q = _mp_manager.Queue()
        _global_egress_req_q = _mp_manager.Queue()
        _global_bridge_tx_q = _mp_manager.Queue()

def get_shared_state():
    """Returns a dict of shared objects to pass to child processes"""
    _ensure_mp_init()
    # Note: _worker_registry is a Manager.dict(), so it is proxy-safe to pass
    return {
        'ingress_q': _global_ingress_q,
        'egress_req_q': _global_egress_req_q,
        'bridge_tx_q': _global_bridge_tx_q,
        'worker_registry': get_instance()._worker_registry
    }

def set_shared_state(state):
    """Called by child process to adopt shared objects"""
    global _global_ingress_q, _global_egress_req_q, _global_bridge_tx_q
    _global_ingress_q = state['ingress_q']
    _global_egress_req_q = state['egress_req_q']
    _global_bridge_tx_q = state['bridge_tx_q']
    # Adopt the registry proxy
    get_instance()._worker_registry = state['worker_registry']


# ========================================================================================
# 4. DPULib (The Main Engine)
# ========================================================================================

class DPULib:
    def __init__(self):
        _ensure_mp_init()
        
        # --- Host Side (Internal to DPU process) ---
        self._host_sq = Queue()
        self._host_cq = Queue()
        self._host_bp = BufferPool("Host")
        
        # --- Sidecar Side ---
        self._sidecar_cq = Queue()
        self._sidecar_sq = Queue()
        self._sidecar_bp = BufferPool("Sidecar")
        self._proxy_int = InterruptController("Proxy")
        self._app_int = InterruptController("App")
        
        # --- Routing Table (ReqID -> WorkerID) ---
        self._req_worker_map: Dict[str, str] = {}
        
        # --- Worker Queues (Managed by DPU) ---
        self._worker_registry = _mp_manager.dict()
        
        self._id_counter = 0
        self._lock = threading.Lock()
        
        # --- Start Components (ENSURE ONCE) ---
        global _bridge_running
        if not _bridge_running:
            _bridge_running = True
            
            self._dma = DMAManager(self._host_sq, self._host_cq, self._host_bp,
                                   self._sidecar_cq, self._sidecar_sq, self._sidecar_bp,
                                   self._proxy_int, self._app_int)
            
            threading.Thread(target=self._run_tcp_bridge, daemon=True, name="TCP-Bridge").start()
            threading.Thread(target=self._run_proxy_simulator, daemon=True, name="L7-Proxy").start()
            threading.Thread(target=self._run_egress_dispatcher, daemon=True, name="Egress-Dispatch").start()
            threading.Thread(target=self._run_egress_collector, daemon=True, name="Egress-Collect").start()
            threading.Thread(target=self._run_bridge_tx, daemon=True, name="Bridge-TX").start()

            print("[DPU-LIB] Engine Components Started (Bridge/Proxy/DMA)", flush=True)
        else:
             print("[DPU-LIB] Engine Components already running (Skipped)", flush=True)

        print("[DPU-LIB] Engine Initialized (Unified MP Mode)", flush=True)

    def _next_id(self):
        with self._lock: self._id_counter += 1; return self._id_counter

    # --- Egress Dispatcher (Worker -> Host SQ) ---
    def _run_egress_dispatcher(self):
        while True:
            try:
                # Get request from ANY worker
                # Item: (worker_id, req_id, method, url, headers, body)
                item = _global_egress_req_q.get()
                wid, rid, method, url, headers, body = item
                
                # Store routing info
                self._req_worker_map[rid] = wid
                
                # Write to Host BP/SQ
                did = self._next_id()
                self._host_bp.write(BufferEntry(did, headers or {}, body))
                self._host_sq.put(SQEntry(rid, did, OpType.REQUEST, method=method, url=url))
                
            except Exception as e:
                print(f"[Dispatch] Error: {e}", flush=True)

    # --- Egress Collector (Host CQ -> Worker Queue) ---
    def _run_egress_collector(self):
        while True:
            self._app_int.wait(0.01)
            while True:
                try:
                    cq = self._host_cq.get_nowait()
                    buf = self._host_bp.read(cq.data_id)
                    if not buf: continue
                    self._host_bp.remove(cq.data_id)
                    
                    # Find Worker
                    wid = self._req_worker_map.pop(cq.req_id, None)
                    if wid:
                        resp = EgressResponse(cq.req_id, 200, buf.headers, buf.body)
                        # Look up worker queue
                        if wid in self._worker_registry:
                            try:
                                self._worker_registry[wid].put(resp)
                            except Exception:
                                pass # Worker might have died
                except Empty: break

    # --- TCP Bridge ---
    def _run_tcp_bridge(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._active_conns = {}
        try:
            sock.bind(('0.0.0.0', 5005))
            sock.listen(100)
            while True:
                conn, _ = sock.accept()
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
        except Exception: pass
        
    def _handle_client(self, conn):
        buf = b""
        try:
            while True:
                d = conn.recv(4096)
                if not d: break
                buf += d
                try:
                    p = json.loads(buf.decode())
                    rid = str(uuid.uuid4())
                    self._active_conns[rid] = conn
                    req = IngressRequest(rid, p.get('method','GET'), p.get('path','/'),
                                         p.get('headers',{}), p.get('body',''), p.get('query_string',''), '127.0.0.1')
                    _global_ingress_q.put(req)
                    break
                except: continue
        except: pass

    def _run_bridge_tx(self):
        while True:
            try:
                resp = _global_bridge_tx_q.get()
                conn = self._active_conns.pop(resp.req_id, None)
                if conn:
                    conn.sendall(json.dumps({'req_id':resp.req_id, 'status':resp.status_code, 'body':resp.body}).encode())
                    conn.close()
            except: pass

    # --- Proxy Simulator ---
    def _run_proxy_simulator(self):
        time.sleep(0.1)
        while True:
            self._proxy_int.wait(0.01)
            try:
                cq = self._sidecar_cq.get_nowait()
                buf = self._sidecar_bp.read(cq.data_id)
                if buf:
                    threading.Thread(target=self._proxy_logic, args=(cq, buf), daemon=True).start()
            except Empty: pass

    def _proxy_logic(self, cq, buf):
        time.sleep(0.05) # Latency
        resp_body = json.dumps({"message": "Hello from Unified Proxy"})
        did = self._next_id()
        self._sidecar_bp.write(BufferEntry(did, {'Content-Type':'application/json'}, resp_body))
        self._sidecar_sq.put(SQEntry(cq.req_id, did, OpType.RESPONSE))


# ========================================================================================
# 5. CLIENT API (Used by Worker Processes)
# ========================================================================================

_local_worker_id = None
_local_worker_q = None

def register_worker(worker_id=None):
    """Call this at the start of a Worker Process"""
    global _local_worker_id, _local_worker_q
    
    # 1. Ensure we have shared objects (inherited or from manager)
    if _global_egress_req_q is None:
        # If we are the main process or purely threaded, this might fail if not init
        # But typically we call get_instance() first.
        pass

    if worker_id is None:
        worker_id = str(os.getpid())
    
    _local_worker_id = worker_id
    
    # Create a queue for this worker
    # Note: We must create it using the Manager if we are in a child process? 
    # Or just a normal Queue? Normal Queue created in child cannot be read by parent unless passed?
    # Actually, Manager.Queue() is best.
    # To get a Manager Queue here, we need the manager.
    # But passing the manager to children is tricky.
    # 
    # WORKAROUND: In MP mode, the parent creates N queues and passes them?
    # OR: We use the `_worker_registry` which is a Manager.dict().
    # We can put a `manager.Queue()` into it. But the child doesn't have the manager.
    # 
    # SIMPLIFICATION: We will assume the parent created the Queues OR
    # we use a helper to request a queue creation.
    #
    # Let's trust that `_mp_manager` is available if we are forked? No, managers are not shared like that.
    #
    # REVISED STRATEGY: 
    # The Child process creates a local Queue? No, IPC.
    #
    # Let's fallback to: Parent creates queues in `server.py` and passes them in `shared_state`.
    # `shared_state` will contain `worker_queues` dict (pre-populated) OR a `create_queue` lambda?
    #
    # ACTUALLY: The easiest way for this code is to assume `_worker_registry` is a proxy.
    # But creating a queue *inside* a child that is visible to parent is hard without the manager.
    #
    # SOLUTION for this file:
    # We will assume `set_shared_state` passed a `worker_registry` (DictProxy).
    # But we can't create a Queue to put in it.
    #
    # ALTERNATIVE: One global "Response Dispatcher Queue" (WorkerID, Resp)?
    # Then each worker filters? No, that's broadcasting.
    #
    # BEST APPROACH:
    # Use a "Callback Queue" created by the Manager in the Parent.
    # The parent (DPULib) exposes a method `create_worker_queue(wid)`.
    # But we can't call parent methods.
    #
    # OK, let's look at `server.py`. It spawns processes. It has the manager.
    # It should create the queue and pass it to the worker.
    # So `register_worker` just needs to accept the queue object.
    pass

def register_worker_with_queue(wid, queue):
    """
    Called by the worker process startup code.
    Registers the queue in the global registry (so DPU can see it).
    """
    global _local_worker_id, _local_worker_q
    _local_worker_id = wid
    _local_worker_q = queue
    
    # Register in the shared dictionary
    # Since _worker_registry is a Manager Dict Proxy, this works across processes
    get_instance()._worker_registry[wid] = queue
    print(f"[Worker] Registered {wid} with DPU", flush=True)

def poll_ingress_sq():
    """Get request from global load-balanced queue"""
    try:
        return _global_ingress_q.get_nowait()
    except: return None

def write_ingress_cq(r):
    _global_bridge_tx_q.put(r)

def submit_egress_request(method, url, headers, body):
    req_id = str(uuid.uuid4())
    # Send (WorkerID, ReqID, ...)
    _global_egress_req_q.put((_local_worker_id, req_id, method, url, headers, body))
    return req_id

def poll_egress_cq():
    """Non-blocking poll of LOCAL worker queue"""
    try:
        return _local_worker_q.get_nowait()
    except: return None

def wait_egress_response(req_id, timeout=30):
    """Blocking wait on LOCAL worker queue"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            # Wait for any response
            resp = _local_worker_q.get(timeout=0.1)
            
            if resp.req_id == req_id:
                return resp
            
            # If not ours, it means we have a pipeline issue or stray response.
            # In strict blocking mode, this is rare. Ideally we should buffer it.
            # For now, let's print a warning and drop it (or push back if possible?)
            # Queues don't support push back.
            # Warning: This response is now LOST.
            # To fix: Use a local buffer dict?
            # 
            # Implement simple local buffer for safety:
            if not hasattr(_local_worker_q, '_buffer'):
                _local_worker_q._buffer = {}
            _local_worker_q._buffer[resp.req_id] = resp
            
        except Empty:
            # Check buffer
            if hasattr(_local_worker_q, '_buffer') and req_id in _local_worker_q._buffer:
                 return _local_worker_q._buffer.pop(req_id)
            continue
            
    raise TimeoutError(f"Timeout {req_id}")

def wait_interrupt():
    time.sleep(0.001)

# ===== Singleton Access =====
_inst = None
def get_instance():
    global _inst
    if _inst is None:
        _inst = DPULib()
    return _inst

def get_worker_registry():
    return get_instance()._worker_registry
