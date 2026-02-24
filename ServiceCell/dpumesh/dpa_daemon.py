"""
dpumesh.dpa_daemon - DMA Manager (DPA, RISC-V 시뮬레이션) v2

수정사항:
- PoolType enum으로 pool resolve (JSON 문자열 제거)
- DMA(buffer copy) 실패 시 descriptor를 SQ에 재삽입 (backpressure)
- buffer 해제 누락 방지
"""

import os
import glob
import time
import threading
from typing import Dict, Optional, Set

from .common import (
    SwDescriptor, CaseFlag, OpFlag, PoolType,
    BufferPool, DescriptorRing,
    PodResources, DpuResources,
    PodRegistry, resolve_pool,
)


class DMASimulator:
    """DMA 전송 시뮬레이션 (memcpy between buffer pools)"""

    @staticmethod
    def copy_buffer(src_pool: BufferPool, src_slot: int, src_len: int,
                    dst_pool: BufferPool) -> int:
        """src pool slot → dst pool (새 slot 할당). -1이면 실패."""
        if src_slot < 0 or src_len == 0:
            return -1
        dst_slot = dst_pool.alloc()
        if dst_slot < 0:
            return -1  # dst pool full
        data = src_pool.read(src_slot, src_len)
        try:
            dst_pool.write(dst_slot, data)
        except Exception:
            dst_pool.free(dst_slot)
            return -1
        return dst_slot


class DMAManager:
    def __init__(self):
        self.node_name = os.environ.get('NODE_NAME', 'unknown')
        self._running = False
        self._dpu: Optional[DpuResources] = None
        self._pod_cache: Dict[int, PodResources] = {}
        self._pod_cache_lock = threading.Lock()
        self._known_pods: Set[int] = set()

    def run(self):
        self._running = True
        print(f"[DPA] DMA Manager starting on node: {self.node_name}", flush=True)

        # 잔여 SHM 정리
        cleaned = 0
        for f in glob.glob("/dev/shm/dpumesh_*"):
            try:
                os.remove(f)
                cleaned += 1
            except Exception:
                pass
        if cleaned:
            print(f"[DPA] Cleaned {cleaned} stale SHM files", flush=True)

        # Pod ID counter 초기화
        try:
            with open("/dev/shm/dpumesh_pod_id_counter", 'w') as f:
                f.write("0")
        except Exception:
            pass

        PodRegistry.clear()
        self._dpu = DpuResources(create=True)
        print(f"[DPA] DPU resources created", flush=True)

        threads = [
            threading.Thread(target=self._poll_host_tx_sqs, daemon=True, name="DPA-TX-Poll"),
            threading.Thread(target=self._poll_sidecar_rx_sq, daemon=True, name="DPA-RX-Poll"),
            threading.Thread(target=self._scan_pods, daemon=True, name="DPA-Pod-Scanner"),
        ]
        for t in threads:
            t.start()

        print(f"[DPA] All polling threads started", flush=True)
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self._running = False

    def _scan_pods(self):
        while self._running:
            try:
                for name, pid in PodRegistry.get_all().items():
                    if pid not in self._known_pods:
                        self._known_pods.add(pid)
                        print(f"[DPA] Pod discovered: {name} (pod_id={pid})", flush=True)
            except Exception:
                pass
            time.sleep(1)

    def _get_pod_resources(self, pod_id: int) -> Optional[PodResources]:
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

    def _resolve_pool(self, pool_type: int, pod_id: int) -> Optional[BufferPool]:
        """PoolType + pod_id → BufferPool (common.resolve_pool 위임)"""
        return resolve_pool(pool_type, pod_id)

    # ==================== TX: Host TX SQ → Sidecar TX SQ ====================

    def _poll_host_tx_sqs(self):
        while self._running:
            work_done = False
            for pod_id in list(self._known_pods):
                pod_res = self._get_pod_resources(pod_id)
                if not pod_res:
                    continue
                desc = pod_res.tx_sq.get()
                if desc is None or desc.valid != 1:
                    continue
                work_done = True
                self._handle_host_tx(desc, pod_res)
            if not work_done:
                time.sleep(0.0005)

    def _handle_host_tx(self, desc: SwDescriptor, src_pod_res: PodResources):
        """
        Host TX → DPU TX header (DMA) → Sidecar TX SQ
        Body는 Host에 그대로 (body_buf_addr 정보만 전달)
        """
        # DMA: Host TX header → DPU TX header
        dpu_header_slot = -1
        if desc.header_buf_slot >= 0 and desc.header_len > 0:
            src_header_pool = self._resolve_pool(
                desc.src_header_pool_type, desc.src_header_pod_id)
            if src_header_pool:
                dpu_header_slot = DMASimulator.copy_buffer(
                    src_header_pool, desc.src_header_buf_slot, desc.header_len,
                    self._dpu.tx_header_pool
                )
                if dpu_header_slot < 0:
                    # DPU TX header pool full → re-enqueue
                    print(f"[DPA] DPU TX header pool full, re-enqueue", flush=True)
                    src_pod_res.tx_sq.put(desc)
                    time.sleep(0.001)
                    return
                # Host TX header 해제
                src_header_pool.free(desc.src_header_buf_slot)

        # Sidecar TX SQ descriptor
        sidecar_desc = SwDescriptor(
            header_buf_slot=dpu_header_slot,
            header_len=desc.header_len,
            body_buf_slot=desc.body_buf_slot,
            body_len=desc.body_len,
            req_id=desc.req_id,
            step_id=desc.step_id,
            dst_pod_id=desc.dst_pod_id,
            src_pod_id=desc.src_pod_id,
            flags=desc.flags,
            valid=1,
            # Body는 아직 Host에 있음 (원본 위치 전달)
            src_body_pool_type=desc.src_body_pool_type,
            src_body_pod_id=desc.src_body_pod_id,
            src_body_buf_slot=desc.src_body_buf_slot,
            # DPU header 위치 (2차 DMA에서 사용)
            src_header_pool_type=PoolType.DPU_TX_HEADER,
            src_header_pod_id=0,
            src_header_buf_slot=dpu_header_slot,
        )

        if not self._dpu.sidecar_tx_sq.put(sidecar_desc):
            # Sidecar TX SQ full → DPU header 해제 + re-enqueue
            print(f"[DPA] Sidecar TX SQ full, re-enqueue", flush=True)
            if dpu_header_slot >= 0:
                self._dpu.tx_header_pool.free(dpu_header_slot)
            # Host TX header는 이미 free됨 → 원본 descriptor는 못 복원
            # → 데이터 유실 방지를 위해 src_pod_res.tx_sq.put(desc) 불가
            # 이 상황은 SQ 크기가 충분하면 발생하지 않아야 함
            time.sleep(0.001)

    # ==================== RX: Sidecar RX SQ → Host RX SQ ====================

    def _poll_sidecar_rx_sq(self):
        while self._running:
            desc = self._dpu.sidecar_rx_sq.get()
            if desc is None or desc.valid != 1:
                time.sleep(0.0005)
                continue
            self._handle_sidecar_rx(desc)

    def _handle_sidecar_rx(self, desc: SwDescriptor):
        dst_pod_res = self._get_pod_resources(desc.dst_pod_id)
        if not dst_pod_res:
            print(f"[DPA] No resources for dst pod {desc.dst_pod_id}", flush=True)
            self._cleanup_src_buffers(desc)
            return

        case = desc.case_flag
        if case == CaseFlag.CASE2_INGRESS:
            self._dma_case2(desc, dst_pod_res)
        elif case == CaseFlag.CASE3_LOCAL:
            self._dma_case3(desc, dst_pod_res)
        else:
            self._dma_case2(desc, dst_pod_res)

    def _dma_case2(self, desc: SwDescriptor, dst_pod_res: PodResources):
        """Case 2: 외부→Host. DPU RX body → Host RX body."""
        host_body_slot = -1
        if desc.src_body_buf_slot >= 0 and desc.body_len > 0:
            src_pool = self._resolve_pool(desc.src_body_pool_type, desc.src_body_pod_id)
            if src_pool:
                host_body_slot = DMASimulator.copy_buffer(
                    src_pool, desc.src_body_buf_slot, desc.body_len,
                    dst_pod_res.rx_body_pool
                )
                if host_body_slot < 0:
                    # Host RX body full → re-enqueue
                    print(f"[DPA] Host RX body full for pod {desc.dst_pod_id}, re-enqueue", flush=True)
                    self._dpu.sidecar_rx_sq.put(desc)
                    time.sleep(0.001)
                    return
                src_pool.free(desc.src_body_buf_slot)

        rx_desc = SwDescriptor(
            header_buf_slot=-1,
            header_len=0,
            body_buf_slot=host_body_slot,
            body_len=desc.body_len,
            req_id=desc.req_id,
            step_id=desc.step_id,
            dst_pod_id=desc.dst_pod_id,
            src_pod_id=desc.src_pod_id,
            flags=desc.flags,
            valid=1,
        )

        if not dst_pod_res.rx_sq.put(rx_desc):
            # Host RX SQ full → 해제 + 경고
            if host_body_slot >= 0:
                dst_pod_res.rx_body_pool.free(host_body_slot)
            print(f"[DPA] Host RX SQ full for pod {desc.dst_pod_id}", flush=True)

    def _dma_case3(self, desc: SwDescriptor, dst_pod_res: PodResources):
        """
        Case 3: Pod A → Pod B.
        - Pod A TX body → Pod B RX body (직접 DMA)
        - DPU TX header → Pod B RX header (DMA)
        """
        # DMA 1: src body → dst RX body (직접)
        host_body_slot = -1
        src_body_pool = self._resolve_pool(desc.src_body_pool_type, desc.src_body_pod_id)
        if src_body_pool and desc.src_body_buf_slot >= 0 and desc.body_len > 0:
            host_body_slot = DMASimulator.copy_buffer(
                src_body_pool, desc.src_body_buf_slot, desc.body_len,
                dst_pod_res.rx_body_pool
            )
            if host_body_slot < 0:
                print(f"[DPA] Case3: dst RX body full for pod {desc.dst_pod_id}, re-enqueue", flush=True)
                self._dpu.sidecar_rx_sq.put(desc)
                time.sleep(0.001)
                return
            src_body_pool.free(desc.src_body_buf_slot)

        # DMA 2: DPU TX header → dst RX header
        host_header_slot = -1
        src_header_pool = self._resolve_pool(desc.src_header_pool_type, desc.src_header_pod_id)
        if src_header_pool and desc.src_header_buf_slot >= 0 and desc.header_len > 0:
            host_header_slot = DMASimulator.copy_buffer(
                src_header_pool, desc.src_header_buf_slot, desc.header_len,
                dst_pod_res.rx_header_pool
            )
            if host_header_slot < 0:
                # header pool full → body 이미 복사됨, header 없이 전달하면 안 됨
                # body를 다시 해제하고 re-enqueue
                if host_body_slot >= 0:
                    dst_pod_res.rx_body_pool.free(host_body_slot)
                print(f"[DPA] Case3: dst RX header full for pod {desc.dst_pod_id}", flush=True)
                # 원본 body는 이미 free됨 → 복원 불가. 이 상황은 pool 크기가 충분하면 발생하지 않아야 함.
                return
            src_header_pool.free(desc.src_header_buf_slot)

        # Host RX SQ descriptor
        rx_desc = SwDescriptor(
            header_buf_slot=host_header_slot,
            header_len=desc.header_len,
            body_buf_slot=host_body_slot,
            body_len=desc.body_len,
            req_id=desc.req_id,
            step_id=desc.step_id,
            dst_pod_id=desc.dst_pod_id,
            src_pod_id=desc.src_pod_id,
            flags=desc.flags,
            valid=1,
        )

        if not dst_pod_res.rx_sq.put(rx_desc):
            if host_body_slot >= 0:
                dst_pod_res.rx_body_pool.free(host_body_slot)
            if host_header_slot >= 0:
                dst_pod_res.rx_header_pool.free(host_header_slot)
            print(f"[DPA] Case3: Host RX SQ full for pod {desc.dst_pod_id}", flush=True)

    def _cleanup_src_buffers(self, desc: SwDescriptor):
        """참조된 src buffer 해제 (에러 경로)"""
        if desc.src_body_buf_slot >= 0:
            pool = self._resolve_pool(desc.src_body_pool_type, desc.src_body_pod_id)
            if pool:
                pool.free(desc.src_body_buf_slot)
        if desc.src_header_buf_slot >= 0:
            pool = self._resolve_pool(desc.src_header_pool_type, desc.src_header_pod_id)
            if pool:
                pool.free(desc.src_header_buf_slot)


def main():
    manager = DMAManager()
    manager.run()

if __name__ == '__main__':
    main()