"""
dpumesh - DPU Mesh Simulation Library (v3)

Queue 기반 구현:
- Worker: Queue에 넣고 빼기만
- DMA Manager: Queue 간 데이터 이동
- Sidecar: 라우팅 결정 (같은노드=Queue, 다른노드=HTTP)
"""

from .common import (
    OpType,
    QueueEntry,
    SharedMemoryQueue,
    QueueManager,
    IngressRequest,
    IngressResponse,
    EgressRequest,
    EgressResponse,
)

from .host_lib import (
    init_worker,
    submit_egress_request,
    poll_egress_cq,
    wait_egress_response,
    poll_ingress_sq,
    write_ingress_cq,
    wait_interrupt,
    shutdown,
    get_worker_id,
    IngressRequest,
    IngressResponse,
    # Compatibility
    register_worker_with_queue,
    get_instance,
    get_shared_state,
    set_shared_state,
)

# 기존 코드 호환용 alias
from . import host_lib as dpu_lib

__all__ = [
    # Common
    'OpType',
    'QueueEntry',
    'SharedMemoryQueue',
    'QueueManager',
    'IngressRequest',
    'IngressResponse',
    'EgressRequest',
    'EgressResponse',
    # Host Lib
    'init_worker',
    'submit_egress_request',
    'poll_egress_cq',
    'wait_egress_response',
    'poll_ingress_sq',
    'write_ingress_cq',
    'wait_interrupt',
    'shutdown',
    'get_worker_id',
    'IngressRequest',
    'IngressResponse',
    'register_worker_with_queue',
    'get_instance',
    'get_shared_state',
    'set_shared_state',
    'dpu_lib',
]