"""
dpumesh - DPU-accelerated Service Mesh Library (HW-Simulated v4)

Memory: Per-service shared pools + per-worker SQs
"""

from .common import (
    SwDescriptor, CaseFlag, OpFlag, PoolType,
    BufferPool, DescriptorRing,
    ServicePools, DpuPools, WorkerSQs, DpuSQs,
    PodResources, DpuResources,
    PodRegistry, StepRegistry,
    resolve_pool, resolve_pool_type,
    serialize_header, deserialize_header,
    OpType, IngressRequest, IngressResponse,
    EgressRequest, EgressResponse,
    SLOT_SIZE, NUM_SLOTS, DESCRIPTOR_SIZE, MAX_DESCRIPTORS,
)

from .host_lib import (
    init_worker, submit_egress_request,
    write_ingress_response, write_ingress_cq,
    wait_interrupt, shutdown,
    get_worker_id, get_pod_id, get_pod_resources, get_step_registry,
    register_worker_with_queue, get_instance, get_shared_state, set_shared_state,
)

from .server import (
    get_context, get_current_context, ExecutionContext, StepResult,
    order, pipe, Order, Pipe, EgressStep, InternalStep,
    ProcessGraphServer,
)

from . import host_lib as dpu_lib