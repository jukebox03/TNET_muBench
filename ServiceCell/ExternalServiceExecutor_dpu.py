"""
ExternalServiceExecutor - DPUmesh version

Key changes:
1. No ThreadPoolExecutor - single thread handles all
2. Uses requests.parallel() for true non-blocking I/O
3. All external calls submitted at once, responses collected at once
"""

import random
import json

from dpumesh import requests


def init_REST(app):
    app.logger.info("Init REST function (DPUmesh mode)")


def init_gRPC(my_service_graph, workmodel, server_port, app):
    # gRPC not yet supported in DPU mode
    raise NotImplementedError("gRPC not supported in DPUmesh mode")


def run_external_service(services_group, work_model, query_string, trace, app, trace_context=None):
    """
    Execute external service calls using DPU.
    
    No threads! All requests submitted to Egress SQ at once.
    """
    app.logger.info("** EXTERNAL SERVICES (DPUmesh parallel mode)")
    
    if not services_group:
        return {}
    
    # ===== Phase 1: Build request list =====
    request_list = []  # (method, url, headers, body, service_name)
    service_names = []
    
    group_id = 0
    for group in services_group:
        # Select services based on seq_len
        if group["seq_len"] < len(group["services"]):
            selected_services = random.sample(group["services"], k=group["seq_len"])
        else:
            selected_services = group["services"]
        
        # Get probabilities if exist
        probabilities = group.get("probabilities", {})
        
        for service in selected_services:
            # Probability check
            p = probabilities.get(service, 1)
            if random.random() >= p:
                continue
            
            # Build URL
            service_no_escape = service.split("__")[0]
            url = f'http://{work_model[service_no_escape]["url"]}{work_model[service_no_escape]["path"]}'
            
            # Determine method and body
            method = "GET"
            headers = trace_context.copy() if trace_context else {}
            body = None
            
            if trace and group_id < len(trace) and service in trace[group_id]:
                # Trace-driven request
                method = "POST"
                headers.update({'Content-type': 'application/json', 'Accept': 'text/plain'})
                json_dict = {service: trace[group_id][service]}
                body = json.dumps(json_dict)
                
                if query_string:
                    url = f"{url}?{query_string}"
            elif query_string:
                url = f"{url}?{query_string}"
            
            request_list.append((method, url, headers, body))
            service_names.append(service)
        
        group_id += 1
    
    if not request_list:
        app.logger.info("No external services to call")
        return {}
    
    app.logger.info(f"Submitting {len(request_list)} external requests in parallel")
    
    # ===== Phase 2: Submit all, wait all =====
    responses = requests.parallel(request_list)
    
    # ===== Phase 3: Check results =====
    service_error_dict = {}
    
    for i, resp in enumerate(responses):
        service_name = service_names[i]
        
        if resp.status_code == 202 and resp.text == "pending":
            # Server mode - responses handled by main loop
            app.logger.info(f"Service {service_name}: submitted (async)")
        elif resp.status_code == 200:
            app.logger.info(f"Service {service_name}: OK, len={len(resp.text)}")
        else:
            app.logger.error(f"Service {service_name}: ERROR status={resp.status_code}")
            service_error_dict[service_name] = Exception(f"status_code: {resp.status_code}")
    
    app.logger.info("** EXTERNAL SERVICES Done!")
    return service_error_dict