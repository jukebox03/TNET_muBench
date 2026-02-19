"""
dpumesh - DPU-accelerated Service Mesh Library

High-Level API:
  from dpumesh.server import get_context
  
  ctx = get_context()
  ctx.run_internal(func, args=(...))
  ctx.call("GET", "http://svc/api")
  with ctx.parallel():
      ctx.call("GET", "http://s1/api")
      ctx.call("GET", "http://s2/api")

Low-Level API:
  from dpumesh.server import order, pipe, EgressStep, InternalStep
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
    # High-level API (권장)
    get_context,
    get_current_context,
    ExecutionContext,
    StepResult,
    
    # Low-level API (직접 graph 조립)
    order,
    pipe,
    Order,
    Pipe,
    EgressStep,
    InternalStep,
    
    # Server
    ProcessGraphServer,
)

# 기존 코드 호환용 alias
from . import host_lib as dpu_lib

__all__ = [
    # High-level API
    'get_context',
    'get_current_context',
    'ExecutionContext',
    'StepResult',
    
    # Low-level API
    'order', 'pipe',
    'Order', 'Pipe',
    'EgressStep', 'InternalStep',
    
    # Server
    'ProcessGraphServer',
    
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
    'register_worker_with_queue',
    'get_instance',
    'get_shared_state',
    'set_shared_state',
    'dpu_lib',
]