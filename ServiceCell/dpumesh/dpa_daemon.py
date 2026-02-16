"""
dpumesh.dpa_daemon - DMA Manager (단순화 버전)

라우팅 규칙:
- REQUEST: dest_worker로 전달
- RESPONSE: dest_worker로 전달 (없으면 Sidecar)

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
import time
import threading
from typing import Dict, Optional, Set

from .common import (
    QueueEntry, OpType,
    SharedMemoryQueue, QueueManager,
    SHM_BASE_PATH, ENV_NODE_NAME
)


class DMATransfer:
    """DMA 전송 추상화"""
    
    @staticmethod
    def copy_to(entry: QueueEntry, dst_queue: SharedMemoryQueue) -> bool:
        return dst_queue.put(entry)


class DMAManager:
    """DMA Manager - Stateless Shuttle"""
    
    def __init__(self):
        self.node_name = os.environ.get(ENV_NODE_NAME, 'unknown')
        self.qm = QueueManager.get_instance()
        
        self._all_worker_ids: Set[str] = set()
        self._workers_lock = threading.Lock()
        
        self._sidecar_sq: Optional[SharedMemoryQueue] = None
        self._sidecar_cq: Optional[SharedMemoryQueue] = None
        
        self._running = False
    
    def run(self):
        self._running = True
        print(f"[DMA Manager] Starting on node: {self.node_name}", flush=True)
        
        # 이전 배포 잔여 SHM 정리
        cleaned = 0
        for f in glob.glob("/dev/shm/dpumesh_*"):
            try:
                os.remove(f)
                cleaned += 1
            except Exception:
                pass
        if cleaned:
            print(f"[DMA Manager] Cleaned {cleaned} stale SHM files", flush=True)
        
        print(f"[DMA Manager] Waiting for Sidecar queues...", flush=True)
        while self._running:
            try:
                self._sidecar_sq, self._sidecar_cq = self.qm.get_sidecar_queues()
                if self._sidecar_sq and self._sidecar_cq:
                    break
            except Exception:
                time.sleep(1)
        print(f"[DMA Manager] Connected to sidecar queues", flush=True)
        
        threads = [
            threading.Thread(target=self._scan_worker_queues, daemon=True, name="Worker-Scanner"),
            threading.Thread(target=self._process_sidecar_cq, daemon=True, name="Sidecar-to-Worker"),
            threading.Thread(target=self._process_worker_cq, daemon=True, name="Worker-to-Sidecar"),
        ]
        
        for t in threads:
            t.start()
        
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self._running = False

    def _scan_worker_queues(self):
        """Worker Queue 자동 탐지"""
        while self._running:
            try:
                for f in os.listdir("/dev/shm"):
                    if f.startswith("dpumesh_worker_") and f.endswith("_sq"):
                        worker_id = f.replace("dpumesh_worker_", "").replace("_sq", "")
                        with self._workers_lock:
                            if worker_id not in self._all_worker_ids:
                                self._all_worker_ids.add(worker_id)
                                print(f"[DMA Manager] Worker discovered: {worker_id}", flush=True)
            except Exception:
                pass
            time.sleep(1)

    def _process_sidecar_cq(self):
        """Sidecar -> Worker: dest_worker를 확인해서 배달"""
        while self._running:
            try:
                entry = self._sidecar_cq.get()
                if entry:
                    if entry.dest_worker:
                        try:
                            # 1. 특정 워커가 지정되어 있으면 거기로 배달
                            worker_sq, _ = self.qm.get_worker_queues(entry.dest_worker)
                            DMATransfer.copy_to(entry, worker_sq)
                            # print(f"[DMA Manager] Sidecar -> Worker: {entry.req_id[:8]} -> {entry.dest_worker}", flush=True)
                        except Exception as e:
                            print(f"[DMA Manager] Delivery to {entry.dest_worker} failed: {e}", flush=True)
                    else:
                        # 2. 목적지가 없으면 다시 Sidecar로 (잘못된 경로 방지)
                        print(f"[DMA Manager] Dropping Sidecar packet without dest_worker: {entry.req_id[:8]}", flush=True)
                else:
                    time.sleep(0.001)
            except Exception as e:
                time.sleep(0.01)

    def _process_worker_cq(self):
        """Worker -> Sidecar: 무조건 Sidecar로 배달"""
        while self._running:
            try:
                with self._workers_lock:
                    worker_ids = list(self._all_worker_ids)
                
                work_done = False
                for worker_id in worker_ids:
                    try:
                        _, worker_cq = self.qm.get_worker_queues(worker_id)
                        entry = worker_cq.get()
                        if entry:
                            work_done = True
                            # 목적지 확인 없이 Sidecar로 전송
                            DMATransfer.copy_to(entry, self._sidecar_sq)
                            # print(f"[DMA Manager] Worker -> Sidecar: {entry.req_id[:8]} from {worker_id}", flush=True)
                    except Exception:
                        pass
                
                if not work_done:
                    time.sleep(0.001)
            except Exception:
                time.sleep(0.01)


def main():
    manager = DMAManager()
    manager.run()


if __name__ == '__main__':
    main()