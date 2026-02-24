"""
dpumesh.dpu_daemon - DPU Sidecar (ARM 시뮬레이션) v2

수정사항:
- PoolType enum 기반 pool resolve
- Bridge 응답 시 buffer 해제 보장
"""

import os
import socket
import threading
import json
import time
import uuid
from typing import Dict, Optional

from .common import (
    SwDescriptor, CaseFlag, OpFlag, PoolType,
    BufferPool, DescriptorRing, DpuResources,
    PodResources, PodRegistry, resolve_pool,
    deserialize_header, serialize_header,
    HTTP_PROXY_PORT,
)


def _get_service_name(url):
    try:
        parts = url.split('/')
        if len(parts) >= 3:
            host = parts[2]
            if ':' in host:
                host = host.split(':')[0]
            return host.split('.')[0]
    except Exception:
        pass
    return 'unknown'


class WorkerSelector:
    def __init__(self):
        self._rr_idx = {}
        self._lock = threading.Lock()

    def select(self, service):
        workers = PodRegistry.get_workers_by_service(service)
        if not workers:
            return None
        with self._lock:
            if service not in self._rr_idx:
                self._rr_idx[service] = 0
            idx = self._rr_idx[service] % len(workers)
            self._rr_idx[service] = idx + 1
            return workers[idx]


class SidecarDaemon:
    def __init__(self):
        self.node_name = os.environ.get('NODE_NAME', 'unknown')
        self.namespace = os.environ.get('NAMESPACE', 'mubench-dpu')
        self._dpu = None
        self._worker_selector = WorkerSelector()
        self._running = False
        self._bridge_lock = threading.Lock()
        self._bridge_responses = {}
        self._bridge_events = {}
        self._pod_cache = {}
        self._pod_cache_lock = threading.Lock()

    def _get_pod_resources(self, pod_id):
        with self._pod_cache_lock:
            if pod_id in self._pod_cache:
                return self._pod_cache[pod_id]
        try:
            res = PodResources(pod_id, create=False)
            with self._pod_cache_lock:
                self._pod_cache[pod_id] = res
            return res
        except Exception:
            return None

    def _resolve_pool(self, pool_type, pod_id):
        return resolve_pool(pool_type, pod_id)

    def run(self):
        self._running = True
        print(f"[ARM] Sidecar starting on node: {self.node_name}", flush=True)

        print(f"[ARM] Waiting for DPU resources...", flush=True)
        while self._running:
            try:
                self._dpu = DpuResources(create=False)
                break
            except Exception:
                time.sleep(0.5)
        print(f"[ARM] Connected to DPU resources", flush=True)

        threads = [
            threading.Thread(target=self._poll_sidecar_tx_sq, daemon=True, name="ARM-TX-Poll"),
            threading.Thread(target=self._run_bridge_server, daemon=True, name="TCP-Bridge"),
        ]
        for t in threads:
            t.start()

        print(f"[ARM] TCP Bridge on port {HTTP_PROXY_PORT}", flush=True)
        print(f"[ARM] All components started", flush=True)

        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self._running = False

    # ==================== Sidecar TX SQ → Routing → Sidecar RX SQ ====================

    def _poll_sidecar_tx_sq(self):
        while self._running:
            desc = self._dpu.sidecar_tx_sq.get()
            if desc is None or desc.valid != 1:
                time.sleep(0.0005)
                continue
            self._route_descriptor(desc)

    def _route_descriptor(self, desc):
        # Header 읽기 (DPU TX header pool)
        header_info = {}
        if desc.header_buf_slot >= 0 and desc.header_len > 0:
            header_data = self._dpu.tx_header_pool.read(
                desc.header_buf_slot, desc.header_len)
            try:
                header_info = deserialize_header(header_data)
            except Exception as e:
                print(f"[ARM] Header parse error: {e}", flush=True)

        if desc.is_response:
            self._route_response(desc, header_info)
        else:
            self._route_request(desc, header_info)

    def _route_request(self, desc, header_info):
        url = header_info.get('url', '')
        service = _get_service_name(url)
        worker_info = self._worker_selector.select(service)

        if not worker_info:
            print(f"[ARM] No worker for service: {service}", flush=True)
            self._cleanup_descriptor_buffers(desc)
            return

        worker_name, dst_pod_id = worker_info
        desc.dst_pod_id = dst_pod_id
        desc.flags = OpFlag.REQUEST | CaseFlag.CASE3_LOCAL
        desc.valid = 1

        if not self._dpu.sidecar_rx_sq.put(desc):
            print(f"[ARM] Sidecar RX SQ full", flush=True)
            self._cleanup_descriptor_buffers(desc)

    def _route_response(self, desc, header_info):
        dest_worker = header_info.get('dest_worker', '')
        req_id_str = header_info.get('req_id_str', '')

        if not dest_worker:
            self._deliver_bridge_response(desc, header_info)
            return

        dst_pod_id = PodRegistry.get_pod_id(dest_worker)
        if dst_pod_id < 0:
            print(f"[ARM] Unknown dest worker: {dest_worker}", flush=True)
            self._cleanup_descriptor_buffers(desc)
            return

        desc.dst_pod_id = dst_pod_id
        desc.flags = OpFlag.RESPONSE | CaseFlag.CASE3_LOCAL
        desc.valid = 1

        if not self._dpu.sidecar_rx_sq.put(desc):
            print(f"[ARM] Sidecar RX SQ full for response", flush=True)
            self._cleanup_descriptor_buffers(desc)

    def _deliver_bridge_response(self, desc, header_info):
        """Bridge 응답: body/header 읽고 해제 후 Event 통지"""
        req_id_str = header_info.get('req_id_str', '')
        status_code = header_info.get('status_code', 200)
        resp_headers = header_info.get('headers', {})

        body_text = ""
        if desc.src_body_buf_slot >= 0 and desc.body_len > 0:
            src_body_pool = self._resolve_pool(
                desc.src_body_pool_type, desc.src_body_pod_id)
            if src_body_pool:
                body_data = src_body_pool.read(desc.src_body_buf_slot, desc.body_len)
                body_text = body_data.decode('utf-8', errors='ignore')
                src_body_pool.free(desc.src_body_buf_slot)

        if desc.src_header_buf_slot >= 0:
            src_header_pool = self._resolve_pool(
                desc.src_header_pool_type, desc.src_header_pod_id)
            if src_header_pool:
                src_header_pool.free(desc.src_header_buf_slot)

        with self._bridge_lock:
            if req_id_str in self._bridge_events:
                self._bridge_responses[req_id_str] = {
                    'req_id': req_id_str,
                    'status': status_code,
                    'headers': resp_headers,
                    'body': body_text,
                }
                self._bridge_events[req_id_str].set()

    def _cleanup_descriptor_buffers(self, desc):
        """에러 경로: descriptor가 참조하는 buffer 모두 해제"""
        if desc.src_body_buf_slot >= 0:
            pool = self._resolve_pool(desc.src_body_pool_type, desc.src_body_pod_id)
            if pool:
                pool.free(desc.src_body_buf_slot)
        if desc.src_header_buf_slot >= 0:
            pool = self._resolve_pool(desc.src_header_pool_type, desc.src_header_pod_id)
            if pool:
                pool.free(desc.src_header_buf_slot)

    # ==================== TCP Bridge ====================

    def _run_bridge_server(self):
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
                    args=(conn, addr), daemon=True
                ).start()
            except Exception as e:
                if self._running:
                    print(f"[ARM] Bridge server error: {e}", flush=True)

    def _handle_bridge_client(self, conn, addr):
        buf = b""
        req_id_str = None

        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data

                try:
                    p = json.loads(buf.decode())
                except json.JSONDecodeError:
                    continue

                req_id_str = str(uuid.uuid4())

                event = threading.Event()
                with self._bridge_lock:
                    self._bridge_events[req_id_str] = event
                    self._bridge_responses[req_id_str] = None

                # 목적지 결정
                target_url = p.get('url', '')
                if not target_url:
                    target_service = p.get('target_service', '')
                    if not target_service:
                        path = p.get('path', '/')
                        path_parts = path.strip('/').split('/')
                        if path_parts and path_parts[0] in ('s0', 's1', 's2', 's3', 's4'):
                            target_service = path_parts[0]
                        else:
                            target_service = 's0'
                    path = p.get('path', '/')
                    target_url = (
                        f"http://{target_service}.{self.namespace}"
                        f".svc.cluster.local{path}"
                    )

                service = _get_service_name(target_url)
                worker_info = self._worker_selector.select(service)

                if not worker_info:
                    self._send_bridge_error(conn, req_id_str, 503,
                                            f"No worker for service {service}")
                    break

                worker_name, dst_pod_id = worker_info

                # Body → DPU RX body pool
                body_str = p.get('body', '')
                body_bytes = body_str.encode() if isinstance(body_str, str) else (body_str or b'')

                body_slot = self._dpu.rx_body_pool.alloc()
                if body_slot < 0:
                    self._send_bridge_error(conn, req_id_str, 503,
                                            "DPU RX body pool full")
                    break

                # Case 2: body에 header+body combined
                header_data = serialize_header(
                    method=p.get('method', 'GET'),
                    url=target_url,
                    path=p.get('path', '/'),
                    headers=p.get('headers', {}),
                    query_string=p.get('query_string', ''),
                    remote_addr=addr[0] if addr else '127.0.0.1',
                    req_id_str=req_id_str,
                    source_worker="",
                    dest_worker=worker_name,
                )

                combined = json.dumps({
                    'header': json.loads(header_data.decode()),
                    'body': body_str,
                }).encode()

                try:
                    body_len = self._dpu.rx_body_pool.write(body_slot, combined)
                except OverflowError:
                    self._dpu.rx_body_pool.free(body_slot)
                    self._send_bridge_error(conn, req_id_str, 413,
                                            "Request too large")
                    break

                desc = SwDescriptor(
                    header_buf_slot=-1,
                    header_len=0,
                    body_buf_slot=body_slot,
                    body_len=body_len,
                    req_id=0,
                    step_id=0,
                    dst_pod_id=dst_pod_id,
                    src_pod_id=0,
                    flags=OpFlag.REQUEST | CaseFlag.CASE2_INGRESS,
                    valid=1,
                    src_body_pool_type=PoolType.DPU_RX_BODY,
                    src_body_pod_id=0,
                    src_body_buf_slot=body_slot,
                )

                if not self._dpu.sidecar_rx_sq.put(desc):
                    self._dpu.rx_body_pool.free(body_slot)
                    self._send_bridge_error(conn, req_id_str, 503,
                                            "Sidecar RX SQ full")
                    break

                print(f"[ARM] Bridge request {req_id_str[:8]} -> {worker_name}", flush=True)

                if event.wait(timeout=30):
                    with self._bridge_lock:
                        response = self._bridge_responses.pop(req_id_str, None)
                        self._bridge_events.pop(req_id_str, None)
                    if response:
                        self._send_bridge_response(conn, response)
                    else:
                        self._send_bridge_error(conn, req_id_str, 500, "No response")
                else:
                    with self._bridge_lock:
                        self._bridge_responses.pop(req_id_str, None)
                        self._bridge_events.pop(req_id_str, None)
                    self._send_bridge_error(conn, req_id_str, 504, "Gateway Timeout")

                break

        except Exception as e:
            print(f"[ARM] Bridge client error: {e}", flush=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _send_bridge_response(self, conn, response):
        try:
            conn.sendall(json.dumps(response).encode())
        except Exception as e:
            print(f"[ARM] Send response error: {e}", flush=True)

    def _send_bridge_error(self, conn, req_id_str, status, message):
        try:
            conn.sendall(json.dumps({
                'req_id': req_id_str,
                'status': status,
                'body': json.dumps({'error': message})
            }).encode())
        except Exception:
            pass


def main():
    daemon = SidecarDaemon()
    daemon.run()

if __name__ == '__main__':
    main()