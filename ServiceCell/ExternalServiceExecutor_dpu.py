"""
ExternalServiceExecutor - DPUmesh Execution Graph version

Role: Build execution graph (order/pipe/EgressStep/InternalStep)
      and register it on ctx.
      Server handles all execution via event loop.

Original (thread-based):
    ThreadPoolExecutor(N groups)
      ├→ Thread: svc_A(block) → svc_B(block)
      └→ Thread: svc_C(block)

DPUmesh (graph-based):
    pipe([
      order([EgressStep(svc_A), EgressStep(svc_B)]),  # Group 0: sequential
      order([EgressStep(svc_C)]),                       # Group 1: sequential
    ])
    → Groups run in parallel (pipe), services within group run sequentially (order)
"""

import random
import json

from dpumesh.server import (
    get_current_context,
    EgressStep, InternalStep, Order, Pipe,
    order, pipe,
)


def init_REST(app):
    app.logger.info("Init REST function (DPUmesh graph mode)")


def init_gRPC(my_service_graph, workmodel, server_port, app):
    raise NotImplementedError("gRPC not supported in DPUmesh mode")


def _build_request(service, group_id, work_model, query_string, trace, trace_context):
    """Build (method, url, headers, body) for a service call"""
    service_no_escape = service.split("__")[0]
    base_url = f'http://{work_model[service_no_escape]["url"]}{work_model[service_no_escape]["path"]}'
    
    method = "GET"
    headers = trace_context.copy() if trace_context else {}
    body = None
    
    if trace and group_id < len(trace) and service in trace[group_id]:
        method = "POST"
        headers.update({'Content-type': 'application/json', 'Accept': 'text/plain'})
        json_dict = {service: trace[group_id][service]}
        body = json.dumps(json_dict)
        if query_string:
            base_url = f"{base_url}?{query_string}"
    elif query_string:
        base_url = f"{base_url}?{query_string}"
    
    return (method, base_url, headers, body)


def run_external_service(services_group, work_model, query_string, trace, app, trace_context=None):
    """
    Build execution graph and register on ctx.
    
    Groups → pipe (parallel)
    Services within group → order (sequential)
    """
    app.logger.info("** EXTERNAL SERVICES (DPUmesh graph mode)")
    
    if not services_group:
        return {}
    
    ctx = get_current_context()
    if ctx is None:
        app.logger.error("No dpumesh context available")
        return {"error": Exception("No dpumesh context")}
    
    # Build groups
    groups = []
    
    for group_id, group in enumerate(services_group):
        # Select services based on seq_len
        if group["seq_len"] < len(group["services"]):
            selected = random.sample(group["services"], k=group["seq_len"])
        else:
            selected = list(group["services"])
        
        # Filter by probability
        probabilities = group.get("probabilities", {})
        filtered = []
        for service in selected:
            p = probabilities.get(service, 1)
            if random.random() < p:
                filtered.append(service)
        
        if not filtered:
            continue
        
        # Build EgressStep list for this group (sequential within group)
        steps = []
        for svc_idx, service in enumerate(filtered):
            req = _build_request(service, group_id, work_model, query_string, trace, trace_context)
            step = EgressStep(
                id=f"egress_g{group_id}_s{svc_idx}_{service}",
                requests=[req],
            )
            steps.append(step)
        
        if len(steps) == 1:
            groups.append(steps[0])
        else:
            groups.append(order(steps))
        
        app.logger.info(f"[Group {group_id}] Registered: {' → '.join(filtered)}")
    
    # Assemble graph
    if not groups:
        app.logger.info("No groups to execute")
        return {}
    elif len(groups) == 1:
        ctx.graph = groups[0]
    else:
        # Multiple groups → parallel execution
        ctx.graph = pipe(groups)
    
    app.logger.info(f"Registered execution graph with {len(groups)} groups")
    
    # No errors at this point - errors handled by server asynchronously
    return {}