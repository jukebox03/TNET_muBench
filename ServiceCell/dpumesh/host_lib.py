"""
dpumesh.host_lib - Host Library for Worker Pods

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
import uuid
import time
import threading
from typing import Optional, Dict
from queue import Queue as LocalQueue
from urllib.parse import urlparse

from .common import (
    QueueEntry, OpType,
    SharedMemoryQueue, QueueManager,
    ENV_WORKER_ID
)



# ========================================================================================
# Global State
# ========================================================================================

_worker_id: Optional[str] = None
_worker_sq: Optional[SharedMemoryQueue] = None  # DMA → Worker (read)
_worker_cq: Optional[SharedMemoryQueue] = None  # Worker → DMA (write)

# Pending responses - Blocking mode only
_pending_responses: Dict[str, QueueEntry] = {}
_pending_lock = threading.Lock()

# Local queue - Blocking mode only
_local_sq: LocalQueue = LocalQueue()

# SQ 폴링 스레드 - Blocking mode only
_sq_poller_thread: Optional[threading.Thread] = None
_running = False


# ========================================================================================
# Initialization
# ========================================================================================

def init_worker(worker_id: Optional[str] = None, start_poller: bool = True):
    """
    Worker 초기화
    
    Args:
        worker_id: Worker ID (None이면 자동 생성)
        start_poller: SQ polling 스레드 시작 여부
                      - True: Blocking mode (poll 스레드가 SHM → LocalQueue로 중계)
                      - False: Graph mode (server가 SHM에서 직접 읽음)
    """
    global _worker_id, _worker_sq, _worker_cq, _sq_poller_thread, _running
    
    app_name = os.environ.get('APP', 'unknown')
    
    if worker_id is None:
        worker_id = os.environ.get(ENV_WORKER_ID)
    if worker_id is None:
        worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    
    _worker_id = f"{app_name}-{worker_id}"
    
    qm = QueueManager.get_instance()
    _worker_sq, _worker_cq = qm.create_worker_queues(_worker_id)
    
    if start_poller:
        _running = True
        _sq_poller_thread = threading.Thread(target=_sq_poll_loop, daemon=True)
        _sq_poller_thread.start()
    
    print(f"[Host] Worker {_worker_id} initialized (poller={'on' if start_poller else 'off'})", flush=True)


def _sq_poll_loop():
    """SQ 폴링 스레드 - Blocking mode 전용"""
    global _running
    
    print(f"[Host] Polling thread started for {_worker_id}", flush=True)
    
    while _running:
        try:
            entry = _worker_sq.get()
            if entry:
                print(f"[Host] Received entry: {entry.req_id[:8] if entry.req_id else 'None'} ({entry.op_type})", flush=True)
                _local_sq.put(entry)
                
                if entry.op_type == OpType.RESPONSE:
                    with _pending_lock:
                        _pending_responses[entry.req_id] = entry
            else:
                time.sleep(0.001)
        except Exception as e:
            if _running:
                print(f"[Host] SQ poll error: {e}", flush=True)
            time.sleep(0.01)


def shutdown():
    """종료"""
    global _running
    _running = False


# ========================================================================================
# Direct SHM Access - Graph mode 전용
# ========================================================================================

def get_worker_sq() -> Optional[SharedMemoryQueue]:
    """worker_sq 직접 접근 (Graph mode에서 server가 직접 읽기 위함)"""
    return _worker_sq


# ========================================================================================
# Egress API
# ========================================================================================

def submit_egress_request(method: str, url: str, headers: Dict, body: Optional[str], 
                          req_id: Optional[str] = None) -> str:
    """
    외부 서비스 요청 제출
    
    Egress 요청: source=현재Worker, dest="" (Sidecar가 결정)
    """
    if req_id is None:
        req_id = str(uuid.uuid4())
    
    # URL 파싱하여 path와 query 추출
    parsed = urlparse(url)
    
    entry = QueueEntry(
        req_id=req_id,
        op_type=OpType.REQUEST,
        method=method,
        url=url,
        path=parsed.path or "/",
        query_string=parsed.query or "",
        headers=headers or {},
        body=body,
        source_worker=_worker_id,  # 요청 보내는 Worker
        dest_worker=""              # Sidecar가 결정
    )
    
    success = _worker_cq.put(entry)
    if not success:
        raise RuntimeError("Worker CQ is full")
    
    return req_id


def wait_egress_response(req_id: str, timeout: float = 30.0) -> QueueEntry:
    """특정 요청의 응답 대기 (Blocking mode 전용)"""
    start = time.time()
    
    while time.time() - start < timeout:
        with _pending_lock:
            if req_id in _pending_responses:
                return _pending_responses.pop(req_id)
        
        time.sleep(0.001)
    
    raise TimeoutError(f"Timeout waiting for response: {req_id}")


def poll_egress_response() -> Optional[QueueEntry]:
    """Egress 응답 폴링 (Blocking mode 전용, Non-blocking)"""
    try:
        entry = _local_sq.get_nowait()
        if entry and entry.op_type == OpType.RESPONSE:
            return entry
        elif entry:
            _local_sq.put(entry)
        return None
    except:
        return None


def poll_egress_cq() -> Optional[QueueEntry]:
    """Egress 응답 폴링 (별칭)"""
    return poll_egress_response()


# ========================================================================================
# Ingress API
# ========================================================================================

def poll_ingress_sq() -> Optional[QueueEntry]:
    """Ingress 요청 폴링 (Blocking mode 전용)"""
    try:
        entry = _local_sq.get_nowait()
        if entry and entry.op_type == OpType.REQUEST:
            return entry
        elif entry:
            _local_sq.put(entry)
        return None
    except:
        return None


def write_ingress_cq(response: 'IngressResponse', source_worker: str = "", dest_worker: str = ""):
    """
    Ingress 응답 전송
    
    TCP Bridge 응답: source=처리한Worker, dest="" (외부)
    Egress 응답: source=처리한Worker, dest=요청한Worker
    """
    entry = QueueEntry(
        req_id=response.req_id,
        op_type=OpType.RESPONSE,
        method="",
        url="",
        path="",
        headers=response.headers,
        body=response.body,
        status_code=response.status_code,
        source_worker=source_worker,  # 응답 보내는 Worker
        dest_worker=dest_worker        # 응답 받을 Worker (""이면 외부)
    )
    
    _worker_cq.put(entry)


# ========================================================================================
# Utility
# ========================================================================================

def wait_interrupt():
    """짧은 대기"""
    time.sleep(0.001)


def get_worker_id() -> str:
    """현재 Worker ID"""
    return _worker_id


# ========================================================================================
# Compatibility Classes
# ========================================================================================

class IngressResponse:
    def __init__(self, req_id: str, status_code: int, headers: Dict, body: str):
        self.req_id = req_id
        self.status_code = status_code
        self.headers = headers
        self.body = body


class IngressRequest:
    """Ingress 요청 래퍼"""
    def __init__(self, entry: QueueEntry):
        self.req_id = entry.req_id
        self.method = entry.method
        self.path = entry.path
        self.headers = entry.headers
        self.body = entry.body
        self.query_string = entry.query_string
        self.remote_addr = entry.remote_addr
        self.source_worker = entry.source_worker
        self.dest_worker = entry.dest_worker


def register_worker_with_queue(wid: str, queue):
    """기존 코드 호환"""
    init_worker(wid)


def get_instance():
    """기존 코드 호환"""
    return None


def get_shared_state():
    """기존 코드 호환"""
    return {}


def set_shared_state(state):
    """기존 코드 호환"""
    pass