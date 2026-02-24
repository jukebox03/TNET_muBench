"""
dpumesh.host_lib - Host Library for Worker Pods (HW-Simulated v2)

수정사항:
- pool 참조를 PoolType enum + pod_id 정수로 (JSON 문자열 제거)
- SQ 삽입 실패 시 buffer 해제 (rollback)
- body 크기 초과 시 명시적 에러
"""

import os
import uuid
import time
import threading
from typing import Optional, Dict
from urllib.parse import urlparse

from .common import (
    SwDescriptor, CaseFlag, OpFlag, PoolType,
    BufferPool, DescriptorRing, PodResources,
    StepRegistry, PodRegistry,
    serialize_header, deserialize_header,
    IngressResponse,
    ENV_WORKER_ID, SLOT_SIZE,
)


# ========================================================================================
# Global State
# ========================================================================================

_worker_id: Optional[str] = None
_pod_id: int = -1
_pod_res: Optional[PodResources] = None
_step_registry: StepRegistry = StepRegistry()
_running = False

_POD_ID_COUNTER_PATH = "/dev/shm/dpumesh_pod_id_counter"
_POD_ID_COUNTER_LOCK = "/dev/shm/dpumesh_pod_id_counter.lock"


# ========================================================================================
# Pod ID Allocation
# ========================================================================================

def _alloc_pod_id() -> int:
    from .common import FileLock
    if not os.path.exists(_POD_ID_COUNTER_LOCK):
        try:
            with open(_POD_ID_COUNTER_LOCK, 'a+') as f:
                pass
        except Exception:
            pass

    with FileLock(_POD_ID_COUNTER_LOCK):
        try:
            if os.path.exists(_POD_ID_COUNTER_PATH):
                with open(_POD_ID_COUNTER_PATH, 'r') as f:
                    current = int(f.read().strip())
            else:
                current = 0
            new_id = current + 1
            with open(_POD_ID_COUNTER_PATH, 'w') as f:
                f.write(str(new_id))
            return new_id
        except Exception:
            import random
            return random.randint(1000, 9999)


# ========================================================================================
# Initialization
# ========================================================================================

def init_worker(worker_id: Optional[str] = None, start_poller: bool = True):
    global _worker_id, _pod_id, _pod_res, _running

    app_name = os.environ.get('APP', 'unknown')

    if worker_id is None:
        worker_id = os.environ.get(ENV_WORKER_ID)
    if worker_id is None:
        worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    _worker_id = f"{app_name}-{worker_id}"
    _pod_id = _alloc_pod_id()
    _pod_res = PodResources(_pod_id, create=True)
    PodRegistry.register(_worker_id, _pod_id, service=app_name)
    _running = True

    print(f"[Host] Worker {_worker_id} initialized (pod_id={_pod_id})", flush=True)


def shutdown():
    global _running
    _running = False


# ========================================================================================
# Direct Access
# ========================================================================================

def get_pod_resources() -> Optional[PodResources]:
    return _pod_res

def get_step_registry() -> StepRegistry:
    return _step_registry

def get_pod_id() -> int:
    return _pod_id


# ========================================================================================
# Egress TX - Worker → 외부 서비스 요청
# ========================================================================================

def submit_egress_request(method: str, url: str, headers: Dict, body: Optional[str],
                          req_id: Optional[str] = None) -> str:
    """
    외부 서비스 요청 제출.

    1. TX header buffer에 HTTP 메타데이터 쓰기
    2. TX body buffer에 body 쓰기
    3. TX SQ에 descriptor 삽입
    4. 실패 시 모든 buffer rollback

    Raises:
        RuntimeError: pool full 또는 SQ full
        OverflowError: 데이터가 slot 크기 초과
    """
    if req_id is None:
        req_id = str(uuid.uuid4())

    parsed = urlparse(url)
    header_slot = -1
    body_slot = -1

    try:
        # Step 1: Header buffer
        header_data = serialize_header(
            method=method,
            url=url,
            path=parsed.path or "/",
            headers=headers or {},
            query_string=parsed.query or "",
            req_id_str=req_id,
            source_worker=_worker_id,
            dest_worker="",
        )

        header_slot = _pod_res.tx_header_pool.alloc()
        if header_slot < 0:
            raise RuntimeError("TX header pool full")
        header_len = _pod_res.tx_header_pool.write(header_slot, header_data)

        # Step 2: Body buffer
        body_bytes = (body or "").encode() if isinstance(body, str) else (body or b"")
        body_slot = _pod_res.tx_body_pool.alloc()
        if body_slot < 0:
            raise RuntimeError("TX body pool full")
        body_len = _pod_res.tx_body_pool.write(body_slot, body_bytes)

        # Step 3: TX SQ descriptor
        desc = SwDescriptor(
            header_buf_slot=header_slot,
            header_len=header_len,
            body_buf_slot=body_slot,
            body_len=body_len,
            req_id=0,
            step_id=0,
            dst_pod_id=0,
            src_pod_id=_pod_id,
            flags=OpFlag.REQUEST | CaseFlag.CASE3_LOCAL,
            valid=1,
            src_header_pool_type=PoolType.HOST_TX_HEADER,
            src_header_pod_id=_pod_id,
            src_header_buf_slot=header_slot,
            src_body_pool_type=PoolType.HOST_TX_BODY,
            src_body_pod_id=_pod_id,
            src_body_buf_slot=body_slot,
        )

        success = _pod_res.tx_sq.put(desc)
        if not success:
            raise RuntimeError("TX SQ full")

        return req_id

    except Exception:
        # Rollback: 할당한 buffer 해제
        if header_slot >= 0:
            _pod_res.tx_header_pool.free(header_slot)
        if body_slot >= 0:
            _pod_res.tx_body_pool.free(body_slot)
        raise


# ========================================================================================
# Ingress TX - Worker → 외부/로컬 응답
# ========================================================================================

def write_ingress_response(response: IngressResponse,
                           source_worker: str = "", dest_worker: str = "",
                           req_id_int: int = 0, step_id_int: int = 0):
    """
    Ingress 응답 전송. 실패 시 buffer rollback.
    """
    header_slot = -1
    body_slot = -1

    try:
        # Header
        header_data = serialize_header(
            status_code=response.status_code,
            headers=response.headers,
            req_id_str=response.req_id,
            source_worker=source_worker,
            dest_worker=dest_worker,
        )

        header_slot = _pod_res.tx_header_pool.alloc()
        if header_slot < 0:
            print(f"[Host] TX header pool full for response {response.req_id[:8]}", flush=True)
            return
        header_len = _pod_res.tx_header_pool.write(header_slot, header_data)

        # Body
        body_bytes = (response.body or "").encode() if isinstance(response.body, str) else (response.body or b"")
        body_slot = _pod_res.tx_body_pool.alloc()
        if body_slot < 0:
            print(f"[Host] TX body pool full for response {response.req_id[:8]}", flush=True)
            _pod_res.tx_header_pool.free(header_slot)
            return
        body_len = _pod_res.tx_body_pool.write(body_slot, body_bytes)

        # dst 결정
        dst_pod_id = 0
        if dest_worker:
            dst_pod_id = PodRegistry.get_pod_id(dest_worker)

        case = CaseFlag.CASE2_INGRESS if not dest_worker else CaseFlag.CASE3_LOCAL

        desc = SwDescriptor(
            header_buf_slot=header_slot,
            header_len=header_len,
            body_buf_slot=body_slot,
            body_len=body_len,
            req_id=req_id_int,
            step_id=step_id_int,
            dst_pod_id=dst_pod_id,
            src_pod_id=_pod_id,
            flags=OpFlag.RESPONSE | case,
            valid=1,
            src_header_pool_type=PoolType.HOST_TX_HEADER,
            src_header_pod_id=_pod_id,
            src_header_buf_slot=header_slot,
            src_body_pool_type=PoolType.HOST_TX_BODY,
            src_body_pod_id=_pod_id,
            src_body_buf_slot=body_slot,
        )

        success = _pod_res.tx_sq.put(desc)
        if not success:
            # SQ full → buffer rollback
            _pod_res.tx_header_pool.free(header_slot)
            _pod_res.tx_body_pool.free(body_slot)
            print(f"[Host] TX SQ full for response {response.req_id[:8]}", flush=True)

    except OverflowError as e:
        if header_slot >= 0:
            _pod_res.tx_header_pool.free(header_slot)
        if body_slot >= 0:
            _pod_res.tx_body_pool.free(body_slot)
        print(f"[Host] Data overflow: {e}", flush=True)
    except Exception as e:
        if header_slot >= 0:
            _pod_res.tx_header_pool.free(header_slot)
        if body_slot >= 0:
            _pod_res.tx_body_pool.free(body_slot)
        print(f"[Host] write_ingress_response error: {e}", flush=True)


# ========================================================================================
# Compatibility
# ========================================================================================

def write_ingress_cq(response, source_worker: str = "", dest_worker: str = ""):
    if isinstance(response, IngressResponse):
        write_ingress_response(response, source_worker, dest_worker)
    else:
        write_ingress_response(
            IngressResponse(
                req_id=getattr(response, 'req_id', ''),
                status_code=getattr(response, 'status_code', 200),
                headers=getattr(response, 'headers', {}),
                body=getattr(response, 'body', ''),
            ),
            source_worker, dest_worker
        )


def wait_interrupt():
    time.sleep(0.001)

def get_worker_id() -> str:
    return _worker_id


class IngressRequest:
    def __init__(self, req_id="", method="", path="", headers=None,
                 body=None, query_string="", remote_addr="",
                 source_worker="", dest_worker=""):
        self.req_id = req_id
        self.method = method
        self.path = path
        self.headers = headers or {}
        self.body = body
        self.query_string = query_string
        self.remote_addr = remote_addr
        self.source_worker = source_worker
        self.dest_worker = dest_worker


def register_worker_with_queue(wid, queue):
    init_worker(wid)

def get_instance():
    return None

def get_shared_state():
    return {}

def set_shared_state(state):
    pass

def poll_egress_cq():
    return None

def wait_egress_response(req_id, timeout=30.0):
    raise NotImplementedError("Use graph mode instead")

def poll_ingress_sq():
    return None