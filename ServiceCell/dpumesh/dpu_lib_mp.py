"""
dpu_lib_mp - Multi-Process Safe Hardware Abstraction Layer
For Blocking Applications (s0-dpu-blocking) only.

Architecture IDENTICAL to dpu_lib.py:
- Separated metadata (SQ/CQ) and data (Buffer Pool)
- DMA Manager as explicit component for Host <-> DPU transfers
- Interrupt + busy polling hybrid notification mechanism
- L7 Proxy with 50ms network latency simulation

Only difference from dpu_lib.py:
- Ingress/Egress API uses multiprocessing.Queue/dict for IPC
  (so child processes can communicate with main process)
- TCP Bridge + DMA + Proxy run in main process threads
"""

import multiprocessing
import threading
import socket
import json
import time
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from queue import Queue, Empty
from enum import Enum

# Re-use dataclasses from dpu_lib
from .dpu_lib import (
    OpType, SQEntry, CQEntry, BufferEntry,
    IngressRequest, IngressResponse, EgressResponse,
    InterruptController, BufferPool, DMAManager
)


# ===== Multiprocessing shared objects =====

_manager = None
_ingress_q = None        # MP Queue: TCP Bridge → server event loop
_egress_req_q = None     # MP Queue: child process → main (egress requests)
_egress_resp_map = None  # MP dict:  main → child process (egress responses)
_bridge_tx_q = None      # MP Queue: child process → TCP Bridge (ingress responses)
_bridge_running = False

def _init_shared_objects():
    """Initialize multiprocessing shared objects (Main Process only)"""
    global _manager, _ingress_q, _egress_req_q, _egress_resp_map, _bridge_tx_q
    if _manager is None:
        _manager = multiprocessing.Manager()
        _ingress_q = _manager.Queue()
        _egress_req_q = _manager.Queue()
        _egress_resp_map = _manager.dict()
        _bridge_tx_q = _manager.Queue()

def set_shared_state(state: Dict[str, Any]):
    """Called by Child Process to inherit parent's shared objects"""
    global _ingress_q, _egress_req_q, _egress_resp_map, _bridge_tx_q
    _ingress_q = state['ingress_q']
    _egress_req_q = state['egress_req_q']
    _egress_resp_map = state['egress_resp_map']
    _bridge_tx_q = state['bridge_tx_q']

def get_shared_state() -> Dict[str, Any]:
    """Called by Main Process to package objects for children"""
    _init_shared_objects()
    return {
        'ingress_q': _ingress_q,
        'egress_req_q': _egress_req_q,
        'egress_resp_map': _egress_resp_map,
        'bridge_tx_q': _bridge_tx_q,
    }


class DPULibMP:
    """
    Hardware Abstraction Layer - Multi-Process version
    
    Same architecture as DPULib (dpu_lib.py):
      Host Side:    Host SQ, Host CQ, Host Buffer Pool
      DPA:          DMA Manager
      DPU Side:     Sidecar CQ, Sidecar SQ, Sidecar Buffer Pool, L7 Proxy
      Interrupts:   proxy_interrupt, app_interrupt
    
    Additionally:
      MP Queues for cross-process IPC (ingress/egress)
    """
    
    def __init__(self):
        global _bridge_running
        
        if _ingress_q is None:
            _init_shared_objects()
        
        # ----- Host Side (thread-safe, in-process) -----
        self._host_sq = Queue()
        self._host_cq = Queue()
        self._host_bp = BufferPool("Host")
        
        # ----- DPU/Sidecar Side -----
        self._sidecar_cq = Queue()
        self._sidecar_sq = Queue()
        self._sidecar_bp = BufferPool("Sidecar")
        
        # ----- Interrupts -----
        self._proxy_interrupt = InterruptController("Proxy")
        self._app_interrupt = InterruptController("App")
        
        # ----- Internal state -----
        self._id_counter = 0
        self._lock = threading.Lock()
        self._pending_egress: Dict[int, threading.Event] = {}
        self._egress_responses: Dict[int, EgressResponse] = {}
        self._active_connections: Dict[int, socket.socket] = {}
        
        # ----- Start components (exactly once) -----
        if not _bridge_running:
            _bridge_running = True
            
            # 1. DMA Manager (same as dpu_lib.py)
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
            
            # 2. TCP Bridge
            threading.Thread(target=self._run_bridge, daemon=True, name="TCP-Bridge").start()
            
            # 3. L7 Proxy Simulator (same as dpu_lib.py)
            threading.Thread(target=self._run_proxy_simulator, daemon=True, name="L7-Proxy").start()
            
            # 4. TX thread (sends ingress responses back to TCP clients)
            threading.Thread(target=self._run_tx, daemon=True, name="TX-Bridge").start()
            
            # 5. Egress dispatcher (MP Queue → Host SQ/BP)
            threading.Thread(target=self._run_egress_dispatcher, daemon=True, name="Egress-Dispatch").start()
            
            # 6. Egress collector (Host CQ → MP dict for child processes)
            threading.Thread(target=self._run_egress_collector, daemon=True, name="Egress-Collect").start()

    def _next_id(self) -> int:
        with self._lock:
            self._id_counter += 1
            return self._id_counter

    # ================================================================
    # TCP Bridge (External traffic injection) - same as dpu_lib.py
    # ================================================================
    
    def _run_bridge(self):
        """Listens on port 5005 for external test scripts"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('0.0.0.0', 5005))
            sock.listen(100)
            print("[DPU-MP] TCP Bridge listening on 5005", flush=True)
            while True:
                conn, _ = sock.accept()
                threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
        except Exception as e:
            print(f"[DPU-MP] Bridge Error: {e}", flush=True)

    def _handle_conn(self, conn):
        """Reads JSON request from socket → creates IngressRequest → MP Queue"""
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data: break
                buf += data
                try:
                    payload = json.loads(buf.decode())
                    req_id = str(uuid.uuid4())
                    self._active_connections[req_id] = conn
                    
                    req = IngressRequest(
                        req_id, payload.get('method', 'GET'), payload.get('path', '/'),
                        payload.get('headers', {}), payload.get('body', ''),
                        payload.get('query_string', ''), '127.0.0.1'
                    )
                    _ingress_q.put(req)
                    break
                except json.JSONDecodeError: continue
        except: pass

    def _run_tx(self):
        """Reads ingress responses from MP Queue → sends back to TCP client"""
        while True:
            try:
                resp = _bridge_tx_q.get()
                conn = self._active_connections.pop(resp.req_id, None)
                if conn:
                    try:
                        data = {'req_id': resp.req_id, 'status': resp.status_code, 'body': resp.body}
                        conn.sendall(json.dumps(data).encode())
                        conn.close()
                    except: pass
            except: pass

    # ================================================================
    # L7 Proxy Simulator (runs on DPU side) - SAME as dpu_lib.py
    # ================================================================
    
    def _run_proxy_simulator(self):
        """
        Simulates L7 Proxy on DPU side.
        Identical logic to dpu_lib.py._run_proxy_simulator
        """
        time.sleep(0.1)  # Wait for initialization
        
        while True:
            # Hybrid: interrupt + busy polling
            self._proxy_interrupt.wait(timeout=0.01)
            
            try:
                cq_entry = self._sidecar_cq.get_nowait()
            except Empty:
                continue
            
            buf = self._sidecar_bp.read(cq_entry.data_id)
            if buf is None:
                continue
            
            threading.Thread(
                target=self._proxy_handle_request,
                args=(cq_entry, buf),
                daemon=True
            ).start()
    
    def _proxy_handle_request(self, cq_entry: CQEntry, buf: BufferEntry):
        """Handle a single egress request in the proxy - SAME as dpu_lib.py"""
        try:
            # L7 Processing (routing, mTLS, header manipulation, etc.)
            # ... simulated ...
            
            # Forward to network and get response (simulated)
            time.sleep(0.05)  # ★ Same 50ms network latency as dpu_lib.py
            
            resp_body = json.dumps({"message": "Hello from proxy"})
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
            
        except Exception as e:
            print(f"[L7-Proxy] Error handling request {cq_entry.req_id}: {e}", flush=True)

    # ================================================================
    # Egress Dispatcher: MP Queue → DPU pipeline (Host BP → Host SQ)
    # ================================================================
    
    def _run_egress_dispatcher(self):
        """
        Reads egress requests from MP Queue (child processes)
        and feeds them into the DPU pipeline (Host BP → Host SQ → DMA → Proxy)
        """
        while True:
            try:
                req_tuple = _egress_req_q.get()
                req_id, method, url, headers, body = req_tuple
                
                data_id = self._next_id()
                
                # Write request data to Host Buffer Pool
                buf_entry = BufferEntry(
                    data_id=data_id,
                    headers=headers or {},
                    body=body
                )
                self._host_bp.write(buf_entry)
                
                # Insert metadata to Host SQ
                sq_entry = SQEntry(
                    req_id=req_id,
                    data_id=data_id,
                    op_type=OpType.REQUEST,
                    method=method,
                    url=url
                )
                self._host_sq.put(sq_entry)
                
            except Exception as e:
                print(f"[Egress-Dispatch] Error: {e}", flush=True)

    # ================================================================
    # Egress Collector: DPU pipeline → MP dict (for child processes)
    # ================================================================
    
    def _run_egress_collector(self):
        """
        Polls Host CQ (via app_interrupt) for completed egress responses.
        Reads response data from Host Buffer Pool.
        Puts result into MP shared dict for child processes to pick up.
        """
        while True:
            # Wait for interrupt from DMA Manager
            self._app_interrupt.wait(timeout=0.01)
            
            while True:
                try:
                    cq_entry = self._host_cq.get_nowait()
                except Empty:
                    break
                
                # Read response data from Host Buffer Pool
                buf = self._host_bp.read(cq_entry.data_id)
                if buf is None:
                    continue
                self._host_bp.remove(cq_entry.data_id)
                
                # Build EgressResponse
                resp = EgressResponse(
                    req_id=cq_entry.req_id,
                    status_code=200,
                    headers=buf.headers,
                    body=buf.body
                )
                
                # Put into MP shared dict (child process will poll this)
                _egress_resp_map[cq_entry.req_id] = resp

    # ================================================================
    # Public API (called by child processes via MP Queue/dict)
    # ================================================================
    
    def poll_ingress_sq(self):
        try: return _ingress_q.get_nowait()
        except: return None

    def write_ingress_cq(self, r):
        _bridge_tx_q.put(r)

    def submit_egress_request(self, method, url, headers, body):
        req_id = str(uuid.uuid4())
        _egress_req_q.put((req_id, method, url, headers, body))
        return req_id

    def wait_egress_response(self, req_id, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            resp = _egress_resp_map.pop(req_id, None)
            if resp is not None:
                return resp
            time.sleep(0.001)
        raise TimeoutError(f"Timeout {req_id}")

    def wait_interrupt(self):
        time.sleep(0.01)


# ===== Global Instance =====
_inst = None
def get_instance():
    global _inst
    if _inst is None:
        _inst = DPULibMP()
    return _inst

# ===== Exported Functions (keyword-argument compatible) =====
def poll_ingress_sq():
    return get_instance().poll_ingress_sq()

def write_ingress_cq(r):
    get_instance().write_ingress_cq(r)

def submit_egress_request(method, url, headers, body):
    return get_instance().submit_egress_request(method, url, headers, body)

def wait_egress_response(req_id, timeout=30):
    return get_instance().wait_egress_response(req_id, timeout)

def wait_interrupt():
    get_instance().wait_interrupt()