"""
dpu_lib - Hardware Abstraction Layer (Simulation Mode with TCP Bridge)

This version includes a TCP server bridge (port 5005) to allow external scripts
to inject traffic and receive responses, simulating a real DPU/Network interface.
"""

import threading
import socket
import json
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from queue import Queue, Empty


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


class DPULib:
    def __init__(self):
        self._req_id_counter = 0
        self._lock = threading.Lock()
        
        # Internal Queues (Acting as HW Ring Buffers)
        self._ingress_sq = Queue()
        self._ingress_cq = Queue()
        self._egress_sq = Queue()
        self._egress_cq = Queue()
        
        self._pending_egress: Dict[int, threading.Event] = {}
        self._egress_responses: Dict[int, EgressResponse] = {}

        # Bridge: Map req_id -> socket connection (to send response back)
        self._active_connections: Dict[int, socket.socket] = {}
        
        # Start TCP Bridge Server
        self._bridge_thread = threading.Thread(target=self._run_tcp_bridge, daemon=True)
        self._bridge_thread.start()
        
        # Start Egress Simulator (Auto-responder for external calls)
        self._egress_sim_thread = threading.Thread(target=self._run_egress_simulator, daemon=True)
        self._egress_sim_thread.start()

    def _next_req_id(self) -> int:
        with self._lock:
            self._req_id_counter += 1
            return self._req_id_counter

    # --- Hardware Interface (Public API) ---

    def poll_ingress_sq(self) -> Optional[IngressRequest]:
        try:
            return self._ingress_sq.get_nowait()
        except Empty:
            return None

    def write_ingress_cq(self, response: IngressResponse):
        """When App finishes a request, send it back to the external client via TCP"""
        self._ingress_cq.put(response)
        self._send_response_to_bridge(response)

    def submit_egress_request(self, method: str, url: str, headers: Dict, body: Optional[str]) -> int:
        req_id = self._next_req_id()
        self._pending_egress[req_id] = threading.Event()
        req = EgressRequest(req_id, method, url, headers, body)
        self._egress_sq.put(req)
        return req_id

    def wait_egress_response(self, req_id: int, timeout: float = 30.0) -> EgressResponse:
        event = self._pending_egress.get(req_id)
        if not event or not event.wait(timeout):
             raise TimeoutError(f"Timeout waiting for egress response {req_id}")
        resp = self._egress_responses.pop(req_id)
        del self._pending_egress[req_id]
        return resp

    def poll_egress_cq(self) -> Optional[EgressResponse]:
        try:
            return self._egress_cq.get_nowait()
        except Empty:
            return None
            
    def wait_interrupt(self):
        time.sleep(0.001)

    # --- Simulation Backend (TCP Bridge & Egress Auto-responder) ---

    def _run_tcp_bridge(self):
        """Listens on port 5005 for external test scripts"""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind(('0.0.0.0', 5005))
            server_sock.listen(5)
            print("[DPU-LIB] TCP Bridge listening on port 5005...", flush=True)
            
            while True:
                conn, addr = server_sock.accept()
                threading.Thread(target=self._handle_client_conn, args=(conn,), daemon=True).start()
        except Exception as e:
            print(f"[DPU-LIB] Bridge Error: {e}", flush=True)

    def _handle_client_conn(self, conn):
        """Reads JSON request from socket -> Ingress SQ"""
        try:
            buffer = b""
            while True:
                data = conn.recv(4096)
                if not data: break
                buffer += data
                
                # Simple JSON framing (one json object per connection for simplicity, or newline)
                try:
                    payload = json.loads(buffer.decode())
                    req_id = self._next_req_id()
                    
                    # Store connection to reply later
                    self._active_connections[req_id] = conn
                    
                    # Create Ingress Request
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
                    self._ingress_sq.put(req)
                    
                    # Only handle one request per connection for this simple sim
                    # To keep connection alive, we'd need protocol framing (length-prefix)
                    break 
                except json.JSONDecodeError:
                    continue # wait for more data
        except Exception as e:
            print(f"[DPU-LIB] Client Handler Error: {e}", flush=True)

    def _send_response_to_bridge(self, response: IngressResponse):
        """Writes Ingress CQ result back to socket"""
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

    def _run_egress_simulator(self):
        """Automatically replies to Egress Requests (Fake External Services) - Parallel Version"""
        
        def process_req(req):
            try:
                # print(f"[DPU-LIB] Simulating network call to {req.url}", flush=True)
                time.sleep(0.05) # Simulate network latency
                
                resp = EgressResponse(
                    req_id=req.req_id,
                    status_code=200,
                    headers={'Content-Type': 'application/json'},
                    body=json.dumps({"message": f"Hello from {req.url}"})
                )
                self._egress_cq.put(resp)
                
                # Wake up any waiting threads (if blocking mode used)
                with self._lock:
                    event = self._pending_egress.get(req.req_id)
                    if event:
                        self._egress_responses[req.req_id] = resp
                        event.set()
            except Exception as e:
                print(f"[DPU-LIB] Egress Worker Error: {e}", flush=True)

        while True:
            try:
                req = self._egress_sq.get(timeout=1.0)
                # Spawn a thread for each egress request to simulate true parallelism
                threading.Thread(target=process_req, args=(req,), daemon=True).start()
            except Empty:
                pass
            except Exception as e:
                print(f"[DPU-LIB] Egress Sim Error: {e}", flush=True)


# Singleton
_instance = DPULib()

# API Exports
def poll_ingress_sq(): return _instance.poll_ingress_sq()
def write_ingress_cq(r): _instance.write_ingress_cq(r)
def submit_egress_request(method, url, headers, body): return _instance.submit_egress_request(method, url, headers, body)
def poll_egress_cq(): return _instance.poll_egress_cq()
def wait_interrupt(): _instance.wait_interrupt()
def wait_egress_response(rid, t=30): return _instance.wait_egress_response(rid, t)
