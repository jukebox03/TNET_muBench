"""
dpumesh - DPU Mesh Simulation Library (v4)

Execution Graph 기반 구현:
- order([...])  : 순차 실행
- pipe([...])   : 병렬 실행
- EgressStep    : 외부 서비스 호출 (SHM Queue, non-blocking)
- InternalStep  : 내부 함수 실행 (ThreadPool, non-blocking)

Queue 기반 통신:
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

from .server import (
    # Execution Graph
    Order,
    Pipe,
    EgressStep,
    InternalStep,
    StepResult,
    ExecutionContext,
    order,
    pipe,
    get_current_context,
    # Server
    ProcessGraphServer,
    ProcessDPUServer,
    DPUmeshServer,
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
    # Execution Graph
    'Order',
    'Pipe',
    'EgressStep',
    'InternalStep',
    'StepResult',
    'ExecutionContext',
    'order',
    'pipe',
    'get_current_context',
    # Server
    'ProcessGraphServer',
    'ProcessDPUServer',
    'DPUmeshServer',
]