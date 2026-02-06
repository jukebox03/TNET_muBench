"""
dpu_lib v2 - Hardware Abstraction Layer (Simulation Mode with TCP Bridge)

Updated architecture based on DPU sidecar design:
- Separated metadata (SQ/CQ) and data (Buffer Pool)
- DMA Manager as explicit component for Host ↔ DPU transfers
- Interrupt + busy polling hybrid notification mechanism

Architecture:
  Host Side:    Host SQ, Host CQ, Host Buffer Pool
  DPA:          DMA Manager (polls Host SQ / Sidecar SQ, transfers data)
  DPU Side:     Sidecar CQ, Sidecar SQ, Sidecar Buffer Pool, L7 Proxy
"""

import threading
import socket
import json
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from queue import Queue, Empty
from enum import Enum


# ===== Data Structures =====

class OpType(Enum):
    """Operation types for SQ/CQ entries"""
    REQUEST = 1
    RESPONSE = 2


@dataclass
class SQEntry:
    """Submission Queue Entry - metadata only (lightweight)"""
    req_id: int
    data_id: int          # Reference to Buffer Pool entry
    op_type: OpType
    # Additional metadata for routing
    method: str = ""
    path: str = ""
    url: str = ""         # For egress requests


@dataclass
class CQEntry:
    """Completion Queue Entry - metadata only"""
    req_id: int
    data_id: int
    op_type: OpType
    status_code: int = 200


@dataclass
class BufferEntry:
    """Buffer Pool Entry - actual request/response data"""
    data_id: int
    headers: Dict[str, str]
    body: Optional[str]
    query_string: str = ""
    remote_addr: str = ""


# ===== Legacy-compatible dataclasses (for server.py/requests.py interface) =====

@dataclass
class IngressRequest:
    req_id: int
    method: str
    path: str
    headers: Dict[str, str]
    body: Optional[str]
    query_string: str
    remote_addr: str


@dataclass
class IngressResponse:
    req_id: int
    status_code: int
    headers: Dict[str, str]
    body: str


@dataclass
class EgressRequest:
    req_id: int
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[str]


@dataclass
class EgressResponse:
    req_id: int
    status_code: int
    headers: Dict[str, str]
    body: str


# ===== Interrupt Mechanism =====

class InterruptController:
    """
    Simulates hardware interrupt + busy polling hybrid.
    
    In real DPU HW:
    - DMA completion triggers PCIe MSI-X interrupt
    - Target (App or Proxy) wakes from sleep/halt
    - Then busy-polls CQ for entries
    
    Simulation:
    - Uses threading.Event as interrupt line
    - wait_interrupt() blocks until signaled or timeout
    """
    
    def __init__(self, name: str):
        self.name = name
        self._event = threading.Event()
    
    def signal(self):
        """Raise interrupt (DMA transfer complete)"""
        self._event.set()
    
    def wait(self, timeout: float = 0.01) -> bool:
        """Wait for interrupt, returns True if signaled"""
        triggered = self._event.wait(timeout=timeout)
        self._event.clear()
        return triggered
    
    def poll(self) -> bool:
        """Non-blocking check (busy polling)"""
        if self._event.is_set():
            self._event.clear()
            return True
        return False


# ===== Buffer Pool =====

class BufferPool:
    """
    Shared memory buffer pool (simulated).
    
    In real HW: pinned DMA-capable memory region.
    Simulation: thread-safe dictionary.
    """
    
    def __init__(self, name: str):
        self.name = name
        self._buffers: Dict[int, BufferEntry] = {}
        self._lock = threading.Lock()
    
    def write(self, entry: BufferEntry):
        with self._lock:
            self._buffers[entry.data_id] = entry
    
    def read(self, data_id: int) -> Optional[BufferEntry]:
        with self._lock:
            return self._buffers.get(data_id)
    
    def remove(self, data_id: int):
        with self._lock:
            self._buffers.pop(data_id, None)


# ===== DMA Manager =====

class DMAManager:
    """
    Simulates DPA (Data Path Accelerator) DMA Manager.
    
    Runs as separate thread, continuously polls:
    1. Host SQ → transfers data to Sidecar Buffer Pool → writes Sidecar CQ → interrupts Proxy
    2. Sidecar SQ → transfers data to Host Buffer Pool → writes Host CQ → interrupts App
    """
    
    def __init__(self,
                 host_sq: Queue, host_cq: Queue,
                 host_bp: BufferPool,
                 sidecar_cq: Queue, sidecar_sq: Queue,
                 sidecar_bp: BufferPool,
                 proxy_interrupt: InterruptController,
                 app_interrupt: InterruptController):
        
        self._host_sq = host_sq
        self._host_cq = host_cq
        self._host_bp = host_bp
        self._sidecar_cq = sidecar_cq
        self._sidecar_sq = sidecar_sq
        self._sidecar_bp = sidecar_bp
        self._proxy_interrupt = proxy_interrupt
        self._app_interrupt = app_interrupt
        
        self._running = False
        self._thread = None
    
    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="DMA-Manager")
        self._thread.start()
    
    def _run(self):
        """DMA Manager main loop - polls both directions"""
        # print("[DMA] DMA Manager started", flush=True)
        
        while self._running:
            work_done = False
            
            # === Host → DPU (Outbound requests) ===
            try:
                sq_entry = self._host_sq.get_nowait()
                work_done = True
                self._transfer_host_to_sidecar(sq_entry)
            except Empty:
                pass
            
            # === DPU → Host (Inbound responses) ===
            try:
                sq_entry = self._sidecar_sq.get_nowait()
                work_done = True
                self._transfer_sidecar_to_host(sq_entry)
            except Empty:
                pass
            
            if not work_done:
                time.sleep(0.0001)  # Avoid busy-wait in simulation
    
    def _transfer_host_to_sidecar(self, sq_entry: SQEntry):
        """
        DMA transfer: Host → Sidecar
        1. Read request data from Host Buffer Pool
        2. Write to Sidecar Buffer Pool
        3. Write CQ entry to Sidecar CQ
        4. Interrupt Proxy
        """
        # DMA Read from Host Buffer Pool
        buf = self._host_bp.read(sq_entry.data_id)
        if buf is None:
            # print(f"[DMA] Warning: no buffer for data_id {sq_entry.data_id}", flush=True)
            return
        
        # DMA Write to Sidecar Buffer Pool (simulate copy)
        sidecar_buf = BufferEntry(
            data_id=sq_entry.data_id,
            headers=buf.headers.copy(),
            body=buf.body,
            query_string=buf.query_string,
            remote_addr=buf.remote_addr
        )
        self._sidecar_bp.write(sidecar_buf)
        
        # Write metadata to Sidecar CQ
        cq_entry = CQEntry(
            req_id=sq_entry.req_id,
            data_id=sq_entry.data_id,
            op_type=sq_entry.op_type
        )
        self._sidecar_cq.put(cq_entry)
        
        # Interrupt Proxy: "new request ready"
        self._proxy_interrupt.signal()
        
        # Cleanup Host Buffer (data transferred)
        self._host_bp.remove(sq_entry.data_id)
    
    def _transfer_sidecar_to_host(self, sq_entry: SQEntry):
        """
        DMA transfer: Sidecar → Host
        1. Read response data from Sidecar Buffer Pool
        2. Write to Host Buffer Pool
        3. Write CQ entry to Host CQ
        4. Interrupt App
        """
        # DMA Read from Sidecar Buffer Pool
        buf = self._sidecar_bp.read(sq_entry.data_id)
        if buf is None:
            return
        
        # DMA Write to Host Buffer Pool
        host_buf = BufferEntry(
            data_id=sq_entry.data_id,
            headers=buf.headers.copy(),
            body=buf.body,
            query_string=buf.query_string,
            remote_addr=buf.remote_addr
        )
        self._host_bp.write(host_buf)
        
        # Write metadata to Host CQ
        cq_entry = CQEntry(
            req_id=sq_entry.req_id,
            data_id=sq_entry.data_id,
            op_type=sq_entry.op_type,
            status_code=sq_entry.req_id  # Will be overwritten
        )
        self._host_cq.put(cq_entry)
        
        # Interrupt App: "response ready"
        self._app_interrupt.signal()
        
        # Cleanup Sidecar Buffer
        self._sidecar_bp.remove(sq_entry.data_id)
    
    def stop(self):
        self._running = False


# ===== Main DPULib Class =====

class DPULib:
    """
    Hardware Abstraction Layer - Updated Architecture
    
    Components:
      Host Side:
        - host_sq: App writes outbound request metadata
        - host_cq: App reads inbound response metadata
        - host_bp: Shared buffer pool for request/response data
        
      DPA:
        - dma_manager: Polls SQs, transfers data via DMA
        
      DPU Side:
        - sidecar_cq: Proxy reads inbound request metadata (from DMA)
        - sidecar_sq: Proxy writes outbound response metadata
        - sidecar_bp: Sidecar buffer pool for request/response data
        - L7 Proxy: simulated egress handler
    
    Interrupts:
        - proxy_interrupt: DMA → Proxy (request arrived)
        - app_interrupt: DMA → App (response arrived)
    """
    
    def __init__(self):
        self._id_counter = 0
        self._lock = threading.Lock()
        
        # ----- Host Side -----
        self._host_sq = Queue()          # App → DMA (request metadata)
        self._host_cq = Queue()          # DMA → App (response metadata)
        self._host_bp = BufferPool("Host")
        
        # ----- DPU/Sidecar Side -----
        self._sidecar_cq = Queue()       # DMA → Proxy (request metadata)
        self._sidecar_sq = Queue()       # Proxy → DMA (response metadata)
        self._sidecar_bp = BufferPool("Sidecar")
        
        # ----- Interrupts -----
        self._proxy_interrupt = InterruptController("Proxy")
        self._app_interrupt = InterruptController("App")
        
        # ----- Egress tracking (for blocking wait mode) -----
        self._pending_egress: Dict[int, threading.Event] = {}
        self._egress_responses: Dict[int, EgressResponse] = {}
        
        # ----- TCP Bridge (external traffic injection) -----
        self._active_connections: Dict[int, socket.socket] = {}
        
        # ----- Start Components -----
        
        # 1. DMA Manager
        self._dma_manager = DMAManager(
            host_sq=self._host_sq,
            host_cq=self._host_cq,
            host_bp=self._host_bp,
            sidecar_cq=self._sidecar_cq,
            sidecar_sq=self._sidecar_sq,
            sidecar_bp=self._sidecar_bp,
            proxy_interrupt=self._proxy_interrupt,
            app_interrupt=self._app_interrupt
        )
        self._dma_manager.start()
        
        # 2. TCP Bridge (simulates external client traffic)
        self._bridge_thread = threading.Thread(
            target=self._run_tcp_bridge, daemon=True, name="TCP-Bridge"
        )
        self._bridge_thread.start()
        
        # 3. L7 Proxy Simulator (handles egress on sidecar side)
        self._proxy_thread = threading.Thread(
            target=self._run_proxy_simulator, daemon=True, name="L7-Proxy"
        )
        self._proxy_thread.start()
    
    def _next_id(self) -> int:
        with self._lock:
            self._id_counter += 1
            return self._id_counter
    
    # ================================================================
    # Public API (Application / Server facing) - Same interface as v1
    # ================================================================
    
    def poll_ingress_sq(self) -> Optional[IngressRequest]:
        """
        App polls for new client requests.
        
        In real HW: App busy-polls Host CQ after interrupt from DMA.
        Here we translate from the TCP bridge's injected ingress path.
        
        Note: For ingress (client → app), the TCP bridge puts requests
        directly into a compatibility queue. In the full DPU flow,
        ingress would also go through DMA, but the app-side interface
        stays the same.
        """
        try:
            return self._ingress_compat_queue.get_nowait()
        except Empty:
            return None
    
    def write_ingress_cq(self, response: IngressResponse):
        """App finished processing, send response back to client"""
        self._send_response_to_bridge(response)
    
    def submit_egress_request(self, method: str, url: str, headers: Dict, body: Optional[str]) -> int:
        """
        App submits outbound request to external service.
        
        Flow: 
        1. Write data to Host Buffer Pool
        2. Insert metadata to Host SQ
        3. DMA Manager picks it up → transfers to Sidecar
        4. L7 Proxy processes and forwards
        """
        req_id = self._next_id()
        data_id = self._next_id()
        
        # 1. Write request data to Host Buffer Pool
        buf_entry = BufferEntry(
            data_id=data_id,
            headers=headers or {},
            body=body
        )
        self._host_bp.write(buf_entry)
        
        # 2. Insert metadata to Host SQ
        sq_entry = SQEntry(
            req_id=req_id,
            data_id=data_id,
            op_type=OpType.REQUEST,
            method=method,
            url=url
        )
        self._host_sq.put(sq_entry)
        
        # Track for blocking wait mode
        self._pending_egress[req_id] = threading.Event()
        
        return req_id
    
    def wait_egress_response(self, req_id: int, timeout: float = 30.0) -> EgressResponse:
        """Blocking wait for egress response (used in standalone mode)"""
        event = self._pending_egress.get(req_id)
        if not event or not event.wait(timeout):
            raise TimeoutError(f"Timeout waiting for egress response {req_id}")
        resp = self._egress_responses.pop(req_id)
        del self._pending_egress[req_id]
        return resp
    
    def poll_egress_cq(self) -> Optional[EgressResponse]:
        """
        App polls for egress responses.
        
        In real HW: App busy-polls Host CQ after app_interrupt.
        Here: check the compatibility response queue.
        """
        try:
            return self._egress_response_queue.get_nowait()
        except Empty:
            return None
    
    def wait_interrupt(self):
        """
        App waits for interrupt (response ready).
        Uses hybrid: check interrupt flag, fallback to short sleep.
        """
        if not self._app_interrupt.wait(timeout=0.001):
            pass  # Timeout, will re-poll
    
    # ================================================================
    # Internal: TCP Bridge (External traffic injection)
    # ================================================================
    
    # Compatibility queue for ingress (TCP bridge → App)
    _ingress_compat_queue: Queue = None
    _egress_response_queue: Queue = None
    
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
    
    def _run_tcp_bridge(self):
        """Listens on port 5005 for external test scripts"""
        # Initialize compat queues
        self._ingress_compat_queue = Queue()
        self._egress_response_queue = Queue()
        
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind(('0.0.0.0', 5005))
            server_sock.listen(5)
            print("[DPU-LIB] TCP Bridge listening on port 5005...", flush=True)
            
            while True:
                conn, addr = server_sock.accept()
                threading.Thread(
                    target=self._handle_client_conn, args=(conn,), daemon=True
                ).start()
        except Exception as e:
            print(f"[DPU-LIB] Bridge Error: {e}", flush=True)
    
    def _handle_client_conn(self, conn):
        """Reads JSON request from socket → creates IngressRequest"""
        try:
            buffer = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buffer += data
                
                try:
                    payload = json.loads(buffer.decode())
                    req_id = self._next_id()
                    
                    self._active_connections[req_id] = conn
                    
                    req = IngressRequest(
                        req_id=req_id,
                        method=payload.get('method', 'GET'),
                        path=payload.get('path', '/'),
                        headers=payload.get('headers', {}),
                        body=payload.get('body', None),
                        query_string=payload.get('query_string', ''),
                        remote_addr=str(conn.getpeername())
                    )
                    
                    print(f"[DPU-LIB] Received external request ID {req_id}", flush=True)
                    self._ingress_compat_queue.put(req)
                    break
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            print(f"[DPU-LIB] Client Handler Error: {e}", flush=True)
    
    def _send_response_to_bridge(self, response: IngressResponse):
        """Writes response back to TCP socket"""
        conn = self._active_connections.pop(response.req_id, None)
        if conn:
            try:
                resp_data = {
                    'req_id': response.req_id,
                    'status': response.status_code,
                    'headers': response.headers,
                    'body': response.body
                }
                conn.sendall(json.dumps(resp_data).encode())
                conn.close()
                print(f"[DPU-LIB] Sent response for ID {response.req_id}", flush=True)
            except Exception as e:
                print(f"[DPU-LIB] Send Error: {e}", flush=True)
    
    # ================================================================
    # Internal: L7 Proxy Simulator (runs on DPU side)
    # ================================================================
    
    def _run_proxy_simulator(self):
        """
        Simulates L7 Proxy on DPU side.
        
        Flow:
        1. Wait for proxy_interrupt (DMA transferred request to Sidecar CQ)
        2. Read CQ entry from Sidecar CQ
        3. Read data from Sidecar Buffer Pool
        4. L7 Processing (routing, mTLS, etc.)
        5. Forward to network (simulated)
        6. Receive response
        7. Write response to Sidecar Buffer Pool
        8. Insert response metadata to Sidecar SQ
        9. DMA Manager transfers back to Host
        """
        # Wait for initialization
        time.sleep(0.1)
        
        while True:
            # Hybrid: interrupt + busy polling
            self._proxy_interrupt.wait(timeout=0.01)
            
            # Busy poll Sidecar CQ
            try:
                cq_entry = self._sidecar_cq.get_nowait()
            except Empty:
                continue
            
            # Read request data from Sidecar Buffer Pool
            buf = self._sidecar_bp.read(cq_entry.data_id)
            if buf is None:
                continue
            
            # Spawn thread for each request (simulate network I/O)
            threading.Thread(
                target=self._proxy_handle_request,
                args=(cq_entry, buf),
                daemon=True
            ).start()
    
    def _proxy_handle_request(self, cq_entry: CQEntry, buf: BufferEntry):
        """Handle a single egress request in the proxy"""
        try:
            # L7 Processing (routing, mTLS, header manipulation, etc.)
            # ... simulated ...
            
            # Forward to network and get response (simulated)
            time.sleep(0.05)  # Simulate network latency
            
            resp_body = json.dumps({"message": f"Hello from proxy"})
            resp_headers = {'Content-Type': 'application/json'}
            
            # Write response data to Sidecar Buffer Pool
            resp_data_id = self._next_id()
            resp_buf = BufferEntry(
                data_id=resp_data_id,
                headers=resp_headers,
                body=resp_body
            )
            self._sidecar_bp.write(resp_buf)
            
            # Insert response metadata to Sidecar SQ
            resp_sq = SQEntry(
                req_id=cq_entry.req_id,
                data_id=resp_data_id,
                op_type=OpType.RESPONSE
            )
            self._sidecar_sq.put(resp_sq)
            
            # DMA Manager will poll Sidecar SQ, transfer to Host,
            # and interrupt App
            
            # === Also notify via legacy egress path ===
            egress_resp = EgressResponse(
                req_id=cq_entry.req_id,
                status_code=200,
                headers=resp_headers,
                body=resp_body
            )
            
            if hasattr(self, '_egress_response_queue') and self._egress_response_queue:
                self._egress_response_queue.put(egress_resp)
            
            # Wake up blocking waiters
            with self._lock:
                event = self._pending_egress.get(cq_entry.req_id)
                if event:
                    self._egress_responses[cq_entry.req_id] = egress_resp
                    event.set()
                    
        except Exception as e:
            print(f"[L7-Proxy] Error handling request {cq_entry.req_id}: {e}", flush=True)


# ===== Singleton =====
_instance = None
_init_lock = threading.Lock()

def _get_instance():
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = DPULib()
    return _instance


# ===== API Exports (backward compatible) =====
def poll_ingress_sq():
    return _get_instance().poll_ingress_sq()

def write_ingress_cq(r):
    _get_instance().write_ingress_cq(r)

def submit_egress_request(method, url, headers, body):
    return _get_instance().submit_egress_request(method, url, headers, body)

def poll_egress_cq():
    return _get_instance().poll_egress_cq()

def wait_interrupt():
    _get_instance().wait_interrupt()

def wait_egress_response(rid, t=30):
    return _get_instance().wait_egress_response(rid, t)