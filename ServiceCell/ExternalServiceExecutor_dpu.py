"""
ExternalServiceExecutor - DPUmesh graph version

Role: Build egress_graph and register it on ctx.
      Server handles all the execution.

That's it. No threads, no callbacks, no submit calls.

Original (thread-based):
    ThreadPoolExecutor(N groups)
      ├→ Thread: svc_A(block) → svc_B(block)
      └→ Thread: svc_C(block)

DPUmesh (graph-based):
    Build graph:
      Group 0: [(GET, url_A, ...), (GET, url_B, ...)]
      Group 1: [(GET, url_C, ...)]
    Register on ctx → return
    Server drives everything.
"""

import random
import json

from dpumesh.server import get_current_context, EgressGroup


def init_REST(app):
    app.logger.info("Init REST function (DPUmesh graph mode)")


def init_gRPC(my_service_graph, workmodel, server_port, app):
    raise NotImplementedError("gRPC not supported in DPUmesh mode")


def _build_request(service, group_id, work_model, query_string, trace, trace_context):
    """Build (method, url, headers, body) for a service call"""
    service_no_escape = service.split("__")[0]
    url = f'http://{work_model[service_no_escape]["url"]}{work_model[service_no_escape]["path"]}'
    
    method = "GET"
    headers = trace_context.copy() if trace_context else {}
    body = None
    
    if trace and group_id < len(trace) and service in trace[group_id]:
        method = "POST"
        headers.update({'Content-type': 'application/json', 'Accept': 'text/plain'})
        json_dict = {service: trace[group_id][service]}
        body = json.dumps(json_dict)
        if query_string:
            url = f"{url}?{query_string}"
    elif query_string:
        url = f"{url}?{query_string}"
    
    return (method, url, headers, body)


def run_external_service(services_group, work_model, query_string, trace, app, trace_context=None):
    """
    Build egress graph and register on ctx. That's all.
    
    Server event loop handles:
    - Submitting first service of each group (parallel)
    - On response: check graph → send next (sequential within group)
    - All done → finalize
    """
    app.logger.info("** EXTERNAL SERVICES (DPUmesh graph mode)")
    
    if not services_group:
        return {}
    
    ctx = get_current_context()
    if ctx is None:
        app.logger.error("No dpumesh context available")
        return {"error": Exception("No dpumesh context")}
    
    # Build egress graph
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
        
        # Build request tuples for this group
        request_list = []
        for service in filtered:
            req = _build_request(service, group_id, work_model, query_string, trace, trace_context)
            request_list.append(req)
        
        # Register group
        ctx.egress_graph.append(EgressGroup(requests=request_list))
        
        app.logger.info(f"[Group {group_id}] Registered: {' → '.join(filtered)}")
    
    app.logger.info(f"Registered {len(ctx.egress_graph)} groups on egress graph")
    
    # No errors at this point - errors are handled by server asynchronously
    return {}