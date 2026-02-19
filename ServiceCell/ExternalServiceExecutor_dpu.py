"""
ExternalServiceExecutor - DPUmesh High-Level API version

Role: workmodel.json의 external_services를 파싱하여
      ctx.call()로 등록.

muBench external_services 구조:
  [
    {"seq_len": 1, "services": ["s1"]},       # group 0
    {"seq_len": 1, "services": ["s2"]},       # group 1
  ]
  → group 간 = 병렬 (parallel)
  → group 내 services = 순차 (선언 순서)
"""

import random
import json


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


def register_external_calls(ctx, services_group, work_model, query_string, trace, app, trace_context=None):
    """
    workmodel의 external_services를 ctx에 등록.
    
    ctx.parallel() 안에서 호출되면 group들이 병렬로 실행됨.
    각 group 내 서비스들은 순차 실행 (ctx.call 선언 순서대로).
    
    사용법 (CellController에서):
        with ctx.parallel():
            register_external_calls(ctx, services_group, ...)
    """
    if not services_group:
        return
    
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
        
        # 각 서비스를 ctx.call()로 등록
        for service in filtered:
            method, url, headers, body = _build_request(
                service, group_id, work_model, query_string, trace, trace_context
            )
            ctx.call(
                method, url,
                headers=headers,
                body=body,
                id=f"egress_g{group_id}_{service}",
            )
        
        app.logger.info(f"[Group {group_id}] {' → '.join(filtered)}")