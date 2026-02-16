"""
dpumesh.dpu_daemon - Sidecar (DPU)

역할:
- TCP Bridge: 외부 요청 수신 → dest_worker 설정
- HTTP Proxy: 외부 서비스 호출 (fallback only)
- 로컬 라우팅: 항상 Queue로 (단일 노드 모드)

source_worker: 요청을 보낸 Worker ("" = 외부)
dest_worker: 요청을 받을 Worker ("" = 외부)

| Type             | source      | dest        |
|------------------|-------------|-------------|
| TCP Bridge 요청  | ""          | "s0-xxx"    |
| TCP Bridge 응답  | "s0-xxx"    | ""          |
| Egress 요청      | "s0-xxx"    | "s1-yyy"    |
| Egress 응답      | "s1-yyy"    | "s0-xxx"    |
"""

import os
import glob
import socket
import threading
import json
import time
import uuid
import requests as http_requests
from typing import Dict, Optional, Set

from .common import (
    QueueEntry, OpType,
    SharedMemoryQueue, QueueManager,
    ENV_NODE_NAME, HTTP_PROXY_PORT
)


def _get_service_name(url: str) -> str:
    """URL에서 서비스 이름 추출"""
    try:
        parts = url.split('/')
        if len(parts) >= 3:
            host = parts[2]
            if ':' in host:
                host = host.split(':')[0]
            return host.split('.')[0]
    except:
        pass
    return 'unknown'


class WorkerRegistry:
    """Worker 목록 관리 (Round-robin용)"""
    
    def __init__(self):
        self._service_workers: Dict[str, list] = {}
        self._service_rr_idx: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._last_scan = 0
    
    def scan(self):
        """Worker Queue 스캔"""
        now = time.time()
        if now - self._last_scan < 5:
            return
        
        try:
            workers_by_service: Dict[str, Set[str]] = {}
            
            for f in os.listdir("/dev/shm"):
                if f.startswith("dpumesh_worker_") and f.endswith("_sq"):
                    worker_id = f.replace("dpumesh_worker_", "").replace("_sq", "")
                    service = worker_id.split('-')[0] if '-' in worker_id else 'unknown'
                    
                    if service not in workers_by_service:
                        workers_by_service[service] = set()
                    workers_by_service[service].add(worker_id)
            
            with self._lock:
                for service, workers in workers_by_service.items():
                    self._service_workers[service] = sorted(list(workers))
                self._last_scan = now
                
        except Exception as e:
            print(f"[WorkerRegistry] Scan error: {e}", flush=True)
    
    def get_worker(self, service: str) -> Optional[str]:
        """서비스에 대해 Round-robin으로 Worker 선택"""
        self.scan()
        
        with self._lock:
            workers = self._service_workers.get(service)
            if not workers:
                return None
            
            if service not in self._service_rr_idx:
                self._service_rr_idx[service] = 0
            
            idx = self._service_rr_idx[service] % len(workers)
            self._service_rr_idx[service] = idx + 1
            
            return workers[idx]


class SidecarDaemon:
    """Sidecar - 단일 노드 모드"""
    
    def __init__(self):
        self.node_name = os.environ.get(ENV_NODE_NAME, 'unknown')
        self.namespace = os.environ.get('NAMESPACE', 'mubench-dpu')
        
        self.qm = QueueManager.get_instance()
        
        self._sidecar_sq: Optional[SharedMemoryQueue] = None
        self._sidecar_cq: Optional[SharedMemoryQueue] = None
        
        self._worker_registry = WorkerRegistry()
        self._running = False
        
        # Bridge 응답 대기
        self._bridge_lock = threading.Lock()
        self._bridge_responses: Dict[str, Optional[QueueEntry]] = {}
        self._bridge_events: Dict[str, threading.Event] = {}
    
    def run(self):
        self._running = True
        
        print(f"[Sidecar] Starting on node: {self.node_name} (single-node mode)", flush=True)
        
        # 이전 배포 잔여 SHM 정리
        cleaned = 0
        for f in glob.glob("/dev/shm/dpumesh_*"):
            try:
                os.remove(f)
                cleaned += 1
            except Exception:
                pass
        if cleaned:
            print(f"[Sidecar] Cleaned {cleaned} stale SHM files", flush=True)
        
        self._sidecar_sq, self._sidecar_cq = self.qm.create_sidecar_queues()
        print(f"[Sidecar] Sidecar queues created", flush=True)
        
        threads = [
            threading.Thread(target=self._run_bridge_server, daemon=True, name="TCP-Bridge"),
            threading.Thread(target=self._process_sq, daemon=True, name="SQ-Processor"),
        ]
        
        for t in threads:
            t.start()
        
        print(f"[Sidecar] TCP Bridge listening on port {HTTP_PROXY_PORT}", flush=True)
        print(f"[Sidecar] All components started", flush=True)
        
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("[Sidecar] Shutting down...", flush=True)
            self._running = False
    
    def _process_sq(self):
        """Sidecar SQ 처리 (DMA Manager → Sidecar)"""
        while self._running:
            try:
                entry = self._sidecar_sq.get()
                
                if entry:
                    if entry.op_type == OpType.REQUEST:
                        self._handle_egress_request(entry)
                    elif entry.op_type == OpType.RESPONSE:
                        self._handle_bridge_response(entry)
                else:
                    time.sleep(0.001)
                    
            except Exception as e:
                print(f"[Sidecar] Process SQ error: {e}", flush=True)
                time.sleep(0.01)
    
    def _handle_egress_request(self, entry: QueueEntry):
        """
        Egress 요청 처리 - 단일 노드 모드: 항상 로컬 라우팅 시도
        Worker가 없을 때만 HTTP fallback
        """
        url = entry.url
        service = _get_service_name(url)
        dest_worker = self._worker_registry.get_worker(service)
        
        if dest_worker:
            # 로컬 Worker가 있으면 Queue로 라우팅
            entry.dest_worker = dest_worker
            self._sidecar_cq.put(entry)
            # print(f"[Sidecar] Local routing {entry.req_id[:8]} → {dest_worker}", flush=True)
        else:
            # Worker가 없으면 HTTP fallback
            print(f"[Sidecar] No local worker for {service}, HTTP fallback {entry.req_id[:8]}", flush=True)
            status_code, resp_headers, resp_body = self._send_http(entry)
            
            response = QueueEntry(
                req_id=entry.req_id,
                op_type=OpType.RESPONSE,
                method=entry.method,
                url=entry.url,
                path=entry.path,
                headers=resp_headers,
                body=resp_body,
                status_code=status_code,
                source_worker="",
                dest_worker=entry.source_worker  # 원래 요청자에게 응답
            )
            self._sidecar_cq.put(response)
    
    def _handle_bridge_response(self, entry: QueueEntry):
        """TCP Bridge 응답 처리 (Worker → 외부 클라이언트 또는 Loopback)"""
        req_id = entry.req_id
        
        # 1. 외부 클라이언트(Bridge)가 기다리는 중인지 확인
        with self._bridge_lock:
            if req_id in self._bridge_events:
                self._bridge_responses[req_id] = entry
                self._bridge_events[req_id].set()
                return
        
        # 2. 브릿지가 없으면 내부 루프백 호출의 응답
        if entry.dest_worker:
            self._sidecar_cq.put(entry)
        else:
            print(f"[Sidecar] Unknown response {req_id[:8]} without dest_worker - ignoring", flush=True)
    
    def _send_http(self, entry: QueueEntry) -> tuple:
        """HTTP 호출 (fallback용)"""
        try:
            url = entry.url
            method = entry.method.upper()
            headers = entry.headers or {}
            body = entry.body
            
            clean_headers = {k: v for k, v in headers.items() 
                          if k.lower() not in ('host', 'content-length')}
            
            if method == "GET":
                resp = http_requests.get(url, headers=clean_headers, timeout=30)
            elif method == "POST":
                resp = http_requests.post(url, headers=clean_headers, data=body, timeout=30)
            elif method == "PUT":
                resp = http_requests.put(url, headers=clean_headers, data=body, timeout=30)
            elif method == "DELETE":
                resp = http_requests.delete(url, headers=clean_headers, timeout=30)
            else:
                resp = http_requests.request(method, url, headers=clean_headers, data=body, timeout=30)
            
            return (resp.status_code, dict(resp.headers), resp.text)
            
        except http_requests.exceptions.Timeout:
            return (504, {}, json.dumps({"error": "Gateway Timeout"}))
        except http_requests.exceptions.ConnectionError as e:
            return (503, {}, json.dumps({"error": f"Connection Error: {str(e)}"}))
        except Exception as e:
            return (500, {}, json.dumps({"error": str(e)}))
    
    # ==================== TCP Bridge ====================
    
    def _run_bridge_server(self):
        """TCP Bridge 서버"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('0.0.0.0', HTTP_PROXY_PORT))
        server.listen(128)
        
        while self._running:
            try:
                server.settimeout(1.0)
                try:
                    conn, addr = server.accept()
                except socket.timeout:
                    continue
                
                threading.Thread(
                    target=self._handle_bridge_client,
                    args=(conn, addr),
                    daemon=True
                ).start()
            except Exception as e:
                if self._running:
                    print(f"[Sidecar] Bridge server error: {e}", flush=True)
    
    def _handle_bridge_client(self, conn: socket.socket, addr: tuple):
        """외부 클라이언트 요청 처리"""
        buffer = b""
        req_id = None
        
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                
                buffer += data
                
                try:
                    p = json.loads(buffer.decode())
                    req_id = str(uuid.uuid4())
                    
                    event = threading.Event()
                    with self._bridge_lock:
                        self._bridge_events[req_id] = event
                        self._bridge_responses[req_id] = None
                    
                    # 목적지 URL/서비스 결정
                    target_url = p.get('url', '')
                    if not target_url:
                        target_service = p.get('target_service', '')
                        
                        if not target_service:
                            path = p.get('path', '/')
                            path_parts = path.strip('/').split('/')
                            if path_parts and path_parts[0] in ('s0', 's1', 's2'):
                                target_service = path_parts[0]
                            else:
                                target_service = 's0'
                        
                        path = p.get('path', '/')
                        target_url = f"http://{target_service}.{self.namespace}.svc.cluster.local{path}"
                    
                    # dest_worker 결정
                    service = _get_service_name(target_url)
                    dest_worker = self._worker_registry.get_worker(service)
                    
                    if not dest_worker:
                        self._send_bridge_error(conn, req_id, 503, f"No worker for service {service}")
                        break
                    
                    # Ingress 요청: source="" (외부), dest=worker
                    entry = QueueEntry(
                        req_id=req_id,
                        op_type=OpType.REQUEST,
                        method=p.get('method', 'GET'),
                        url=target_url,
                        path=p.get('path', '/'),
                        headers=p.get('headers', {}),
                        body=p.get('body', ''),
                        query_string=p.get('query_string', ''),
                        remote_addr=addr[0] if addr else '127.0.0.1',
                        source_worker="",  # 외부 클라이언트
                        dest_worker=dest_worker
                    )
                    
                    self._sidecar_cq.put(entry)
                    print(f"[Sidecar] Bridge request {req_id[:8]} → {dest_worker}", flush=True)
                    
                    if event.wait(timeout=30):
                        with self._bridge_lock:
                            response = self._bridge_responses.pop(req_id, None)
                            self._bridge_events.pop(req_id, None)
                        
                        if response:
                            self._send_bridge_response(conn, response)
                        else:
                            self._send_bridge_error(conn, req_id, 500, "No response")
                    else:
                        with self._bridge_lock:
                            self._bridge_responses.pop(req_id, None)
                            self._bridge_events.pop(req_id, None)
                        self._send_bridge_error(conn, req_id, 504, "Gateway Timeout")
                    
                    break
                    
                except json.JSONDecodeError:
                    continue
                    
        except Exception as e:
            print(f"[Sidecar] Bridge client error: {e}", flush=True)
        
        finally:
            try:
                conn.close()
            except:
                pass
    
    def _send_bridge_response(self, conn: socket.socket, entry: QueueEntry):
        try:
            response = {
                'req_id': entry.req_id,
                'status': entry.status_code,
                'headers': entry.headers,
                'body': entry.body
            }
            conn.sendall(json.dumps(response).encode())
        except Exception as e:
            print(f"[Sidecar] Send response error: {e}", flush=True)
    
    def _send_bridge_error(self, conn: socket.socket, req_id: str, status: int, message: str):
        try:
            response = {
                'req_id': req_id,
                'status': status,
                'body': json.dumps({'error': message})
            }
            conn.sendall(json.dumps(response).encode())
        except:
            pass


def main():
    daemon = SidecarDaemon()
    daemon.run()


if __name__ == '__main__':
    main()