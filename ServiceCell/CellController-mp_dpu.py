from __future__ import print_function

import argparse
import json
import os
import sys
import time
import traceback
from threading import Thread
from concurrent import futures

from flask import Flask, Response, json, make_response, request
import prometheus_client
from prometheus_client import CollectorRegistry, Summary, multiprocess, Histogram

from ExternalServiceExecutor_dpu import init_REST, init_gRPC, run_external_service
from InternalServiceExecutor import run_internal_service

from dpumesh.server import (
    get_current_context,
    EgressStep, InternalStep, Order, Pipe,
    order, pipe,
)

import mub_pb2_grpc as pb2_grpc
import mub_pb2 as pb2
import grpc


# Configuration of global variables

jaeger_headers_list = [
    'x-request-id',
    'x-b3-traceid',
    'x-b3-spanid',
    'x-b3-parentspanid',
    'x-b3-sampled',
    'x-b3-flags',
    'x-datadog-trace-id',
    'x-datadog-parent-id',
    'x-datadog-sampling-priority',
    'x-ot-span-context',
    'grpc-trace-bin',
    'traceparent',
    'x-cloud-trace-context',
]

# Flask APP
app = Flask(__name__)
ID = os.environ["APP"]
ZONE = os.environ["ZONE"]  # Pod Zone
K8S_APP = os.environ["K8S_APP"]  # K8s label app
PN = os.environ.get("PN", "4")  # Number of processes
TN = os.environ.get("TN", "16")  # Number of thread per process
traceEscapeString = "__"

globalDict=dict()
def read_config_files():
    res = dict()
    with open('MSConfig/workmodel.json') as f:
        workmodel = json.load(f)
        for service in workmodel:
            app.logger.info(f'service: {service}')
            if service==ID:
                res[service]=workmodel[service]
            else:
                res[service]={"url":workmodel[service]["url"],"path":workmodel[service]["path"]}
    return res
globalDict['work_model'] = read_config_files()

if "request_method" in globalDict['work_model'][ID].keys():
    request_method = globalDict['work_model'][ID]["request_method"].lower()
else:
    request_method = "rest"

########################### PROMETHEUS METRICS
registry = CollectorRegistry()
multiprocess.MultiProcessCollector(registry)

CONTENT_TYPE_LATEST = str('text/plain; version=0.0.4; charset=utf-8')
RESPONSE_SIZE = Summary('mub_response_size', 'Response size',
                        ['zone', 'app_name', 'method', 'endpoint', 'from', 'kubernetes_service'], registry=registry
                        )

INTERNAL_PROCESSING = Summary('mub_internal_processing_latency_milliseconds', 'Latency of internal service',
                           ['zone', 'app_name', 'method', 'endpoint'],registry=registry
                           )
EXTERNAL_PROCESSING = Summary('mub_external_processing_latency_milliseconds', 'Latency of external services',
                           ['zone', 'app_name', 'method', 'endpoint'], registry=registry
                           )
REQUEST_PROCESSING = Summary('mub_request_processing_latency_milliseconds', 'Request latency including external and internal service',
                           ['zone', 'app_name', 'method', 'endpoint', 'from', 'kubernetes_service'],registry=registry
                           )

buckets=[0.5, 1, 10, 100 ,1000, 10000, float("inf")] 
INTERNAL_PROCESSING_BUCKET = Histogram('mub_internal_processing_latency_milliseconds_bucket', 'Latency of internal service',
                           ['zone', 'app_name', 'method', 'endpoint'],registry=registry,buckets=buckets
                           )
EXTERNAL_PROCESSING_BUCKET = Histogram('mub_external_processing_latency_milliseconds_bucket', 'Latency of external services',
                           ['zone', 'app_name', 'method', 'endpoint'], registry=registry,buckets=buckets
                           )
REQUEST_PROCESSING_BUCKET = Histogram('mub_request_processing_latency_milliseconds_bucket', 'Request latency including external and internal service',
                           ['zone', 'app_name', 'method', 'endpoint', 'from', 'kubernetes_service'],registry=registry,buckets=buckets
)


def _run_internal(my_internal_service):
    """InternalStep에서 실행될 wrapper 함수"""
    return run_internal_service(my_internal_service)


@app.route(f"{globalDict['work_model'][ID]['path']}", methods=['GET','POST'])
def start_worker():
    global globalDict
    
    try:
        start_request_processing = time.time()
        app.logger.info('Request Received')
        
        query_string = request.query_string.decode()
        behaviour_id = request.args.get('bid', default = 'default', type = str)
        
        # default behaviour
        my_work_model = globalDict['work_model'][ID]
        my_service_graph = my_work_model['external_services'] 
        my_internal_service = my_work_model['internal_service']

        # update internal service behaviour
        if behaviour_id != 'default' and "alternative_behaviors" in my_work_model.keys():
                if behaviour_id in my_work_model['alternative_behaviors'].keys():
                    if "internal_services" in my_work_model['alternative_behaviors'][behaviour_id].keys():
                        my_internal_service = my_work_model['alternative_behaviors'][behaviour_id]['internal_service']

        # trace context propagation
        jaeger_headers = dict()
        for jhdr in jaeger_headers_list:
            val = request.headers.get(jhdr)
            if val is not None:
                jaeger_headers[jhdr] = val

        # if POST check the presence of a trace
        trace = {}
        if request.method == 'POST':
            try:
                raw_data = request.get_data()
                if not raw_data:
                     app.logger.warning("Empty raw body in POST")
                
                trace = request.get_json(force=True, silent=True)
                if trace is None:
                    app.logger.warning(f"Failed to parse JSON. Raw body: {raw_data.decode('utf-8', errors='ignore')[:200]}")
                    trace = {}
            except Exception as e:
                app.logger.error(f"JSON Parsing Error: {e}")
                trace = {}

        if trace and len(trace) > 0:
            if len(trace.keys()) != 1:
                app.logger.error(f"Bad trace format: keys={list(trace.keys())}")
                return make_response(json.dumps({"message": "bad trace format"}), 400)
            
            trace_key = list(trace.keys())[0]
            if ID != trace_key.split(traceEscapeString)[0]:
                app.logger.error(f"Bad trace format: ID mismatch {ID} vs {trace_key}")
            
            trace[ID] = trace[trace_key]
            
        if trace and ID in trace and len(trace[ID]) > 0:
            n_groups = len(trace[ID])
            my_service_graph = list()
            for i in range(0,n_groups):
                group = trace[ID][i]
                group_dict = dict()
                group_dict['seq_len'] = len(group)
                group_dict['services'] = list(group.keys())
                my_service_graph.append(group_dict)
        else:
            if behaviour_id != 'default' and "alternative_behaviors" in my_work_model.keys():
                if behaviour_id in my_work_model['alternative_behaviors'].keys():
                    if "external_services" in my_work_model['alternative_behaviors'][behaviour_id].keys():
                        my_service_graph = my_work_model['alternative_behaviors'][behaviour_id]['external_services']

        # ========================================================
        # Build Execution Graph
        # ========================================================
        # 
        # μBench pattern: Internal first, then External
        # Graph: order([InternalStep, pipe([EgressGroups...])])
        #
        # For general microservices, modify this section to build
        # any order/pipe combination needed.
        # ========================================================
        
        ctx = get_current_context()
        
        if ctx is not None:
            graph_steps = []
            
            # Step 1: Internal service (in ThreadPool)
            graph_steps.append(
                InternalStep(
                    id="internal_service",
                    func=_run_internal,
                    args=(my_internal_service,),
                )
            )
            
            # Step 2: External services (via SHM Queue)
            # run_external_service builds egress sub-graph and sets ctx.graph
            # But we need to integrate it into our graph, so we build it here directly
            if len(my_service_graph) > 0:
                trace_data = trace.get(ID, {}) if trace else {}
                
                egress_graph = _build_egress_graph(
                    my_service_graph, globalDict['work_model'], 
                    query_string, trace_data, jaeger_headers, app
                )
                
                if egress_graph:
                    graph_steps.append(egress_graph)
            
            # Assemble final graph
            if len(graph_steps) == 1:
                ctx.graph = graph_steps[0]
            else:
                ctx.graph = order(graph_steps)
            
            app.logger.info(f"Execution graph registered with {len(graph_steps)} steps")
        else:
            # Fallback: no context (shouldn't happen in graph mode)
            app.logger.warning("No dpumesh context - running synchronously")
            body = run_internal_service(my_internal_service)
            if len(my_service_graph) > 0:
                trace_data = trace.get(ID, {}) if trace else {}
                run_external_service(
                    my_service_graph, globalDict['work_model'],
                    query_string, trace_data, app, jaeger_headers
                )
            response = make_response(body)
            response.mimetype = "text/plain"
            response.headers.update(jaeger_headers)
            return response

        # Graph mode: return placeholder (server will replace with actual result)
        response = make_response("")
        response.mimetype = "text/plain"
        response.headers.update(jaeger_headers)
        return response
        
    except Exception as err:
        app.logger.error(f"Error in start_worker: {err}")
        return json.dumps({"message": "Error"}), 500


def _build_egress_graph(services_group, work_model, query_string, trace, trace_context, app):
    """Build egress execution graph from service groups.
    
    Returns: Order, Pipe, EgressStep, or None
    """
    import random
    
    groups = []
    
    for group_id, group in enumerate(services_group):
        if group["seq_len"] < len(group["services"]):
            selected = random.sample(group["services"], k=group["seq_len"])
        else:
            selected = list(group["services"])
        
        probabilities = group.get("probabilities", {})
        filtered = []
        for service in selected:
            p = probabilities.get(service, 1)
            if random.random() < p:
                filtered.append(service)
        
        if not filtered:
            continue
        
        steps = []
        for svc_idx, service in enumerate(filtered):
            from ExternalServiceExecutor_dpu import _build_request
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
        
        app.logger.info(f"[Group {group_id}] {' → '.join(filtered)}")
    
    if not groups:
        return None
    elif len(groups) == 1:
        return groups[0]
    else:
        return pipe(groups)


# Prometheus
@app.route('/metrics')
def metrics():
    return Response(prometheus_client.generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    if request_method == "rest":
        init_REST(app)
        
        from dpumesh.server import ProcessGraphServer
        
        num_workers = int(PN) if PN else 4
        server = ProcessGraphServer("CellController-mp_dpu:app", host='0.0.0.0', port=8080, workers=num_workers)
        server.run()
        
    elif request_method == "grpc":
        print("gRPC mode not yet supported with DPUmesh")
        sys.exit(1)
    else:
        app.logger.info("Error: Unsupported request method")
        sys.exit(0)