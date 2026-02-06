from __future__ import print_function

import argparse
import json
import os
import sys
import time
import traceback
from threading import Thread
from concurrent import futures

# ===== REMOVED: gunicorn.app.base =====
from flask import Flask, Response, json, make_response, request
import prometheus_client
from prometheus_client import CollectorRegistry, Summary, multiprocess, Histogram

from ExternalServiceExecutor_dpu import init_REST, init_gRPC, run_external_service
from InternalServiceExecutor import run_internal_service

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

#globalDict=Manager().dict()
globalDict=dict()
def read_config_files():
    res = dict()
    with open('MSConfig/workmodel.json') as f:
        workmodel = json.load(f)
        # shrink workmodel
        for service in workmodel:
            app.logger.info(f'service: {service}')
            if service==ID:
                res[service]=workmodel[service]
            else:
                res[service]={"url":workmodel[service]["url"],"path":workmodel[service]["path"]}
    return res
globalDict['work_model'] = read_config_files()    # must be shared among processes for hot update

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
                # Debug: Print raw data if needed
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
            # sanity_check
            if len(trace.keys()) != 1:
                app.logger.error(f"Bad trace format: keys={list(trace.keys())}")
                return make_response(json.dumps({"message": "bad trace format"}), 400)
            
            trace_key = list(trace.keys())[0]
            if ID != trace_key.split(traceEscapeString)[0]:
                app.logger.error(f"Bad trace format: ID mismatch {ID} vs {trace_key}")
                # Optional: return 400 or just continue
            
            trace[ID] = trace[trace_key]
            
        if trace and ID in trace and len(trace[ID]) > 0:
        # trace-driven request
            n_groups = len(trace[ID])
            my_service_graph = list()
            for i in range(0,n_groups):
                group = trace[ID][i]
                group_dict = dict()
                group_dict['seq_len'] = len(group)
                group_dict['services'] = list(group.keys())
                my_service_graph.append(group_dict)
        else:
            # update external service behaviour
            if behaviour_id != 'default' and "alternative_behaviors" in my_work_model.keys():
                if behaviour_id in my_work_model['alternative_behaviors'].keys():
                    if "external_services" in my_work_model['alternative_behaviors'][behaviour_id].keys():
                        my_service_graph = my_work_model['alternative_behaviors'][behaviour_id]['external_services']

        # Execute the internal service
        app.logger.info("*************** INTERNAL SERVICE STARTED ***************")
        start_local_processing = time.time()
        body = run_internal_service(my_internal_service)
        local_processing_latency = time.time() - start_local_processing
        INTERNAL_PROCESSING.labels(ZONE, K8S_APP, request.method, request.path).observe(local_processing_latency*1000)
        INTERNAL_PROCESSING_BUCKET.labels(ZONE, K8S_APP, request.method, request.path).observe(local_processing_latency*1000)
        RESPONSE_SIZE.labels(ZONE, K8S_APP, request.method, request.path, request.remote_addr, ID).observe(len(body))
        app.logger.info("len(body): %d" % len(body))
        app.logger.info("############### INTERNAL SERVICE FINISHED! ###############")

        # Execute the external services
        start_external_request_processing = time.time()
        app.logger.info("*************** EXTERNAL SERVICES STARTED ***************")
        
        if len(my_service_graph) > 0:
            if len(trace)>0:
                service_error_dict = run_external_service(my_service_graph,globalDict['work_model'],query_string,trace[ID],app, jaeger_headers)
            else:
                service_error_dict = run_external_service(my_service_graph,globalDict['work_model'],query_string,dict(),app, jaeger_headers)
            if len(service_error_dict):
                app.logger.error(service_error_dict)
                app.logger.error("Error in request external services")
                app.logger.error(service_error_dict)
                return make_response(json.dumps({"message": "Error in external services request"}), 500)
        app.logger.info("############### EXTERNAL SERVICES FINISHED! ###############")

        response = make_response(body)
        response.mimetype = "text/plain"
        EXTERNAL_PROCESSING.labels(ZONE, K8S_APP, request.method, request.path).observe((time.time() - start_external_request_processing)*1000)
        EXTERNAL_PROCESSING_BUCKET.labels(ZONE, K8S_APP, request.method, request.path).observe((time.time() - start_external_request_processing)*1000)
        
        REQUEST_PROCESSING.labels(ZONE, K8S_APP, request.method, request.path, request.remote_addr, ID).observe((time.time() - start_request_processing)*1000)
        REQUEST_PROCESSING_BUCKET.labels(ZONE, K8S_APP, request.method, request.path, request.remote_addr, ID).observe((time.time() - start_request_processing)*1000)

        # Add trace context propagation headers to the response
        response.headers.update(jaeger_headers)

        return response
    except Exception as err:
        app.logger.error(f"Error in start_worker: {err}")
        # app.logger.error(traceback.format_exc())
        return json.dumps({"message": "Error"}), 500

# Prometheus
@app.route('/metrics')
def metrics():
    return Response(prometheus_client.generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)


# ===== REMOVED: Gunicorn HttpServer class =====
# ===== REMOVED: gRPC server (for now) =====


if __name__ == '__main__':
    if request_method == "rest":
        init_REST(app)
        
        # ===== CHANGED: Use ProcessGraphServer for Multi-Process Non-Blocking =====
        from dpumesh.server import ProcessGraphServer
        
        # Use 'PN' (Process Number) from environment, default to 4 if not set
        num_workers = int(PN) if PN else 4
        
        # Pass app path string so workers can load it fresh
        server = ProcessGraphServer("CellController-mp_dpu:app", host='0.0.0.0', port=8080, workers=num_workers)
        server.run()
        # =========================================================================
        
    elif request_method == "grpc":
        # gRPC not yet supported in DPU mode
        print("gRPC mode not yet supported with DPUmesh")
        sys.exit(1)
    else:
        app.logger.info("Error: Unsupported request method")
        sys.exit(0)