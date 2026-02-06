import requests
import json
import time
import sys
import threading
import random

# Configuration
CONCURRENT_REQUESTS = 50
TARGET_HOST = 'localhost'
TARGET_PORT = 30081 # Default to the HTTP NodePort for Blocking Service

def send_request(client_id):
    url = f"http://{TARGET_HOST}:{TARGET_PORT}/api/v1"
    headers = {"Content-Type": "application/json"}
    # Payload simulating the trace (s0 -> s1, s2)
    payload = {"s0": [{"s1": {}, "s2": {}}]}
    
    start_time = time.time()
    try:
        resp = requests.post(url, json=payload, headers=headers)
        latency = (time.time() - start_time) * 1000
        
        if resp.status_code == 200:
            print(f"[Client {client_id:02d}] ✅ Status: {resp.status_code} | Latency: {latency:.2f}ms")
        else:
            print(f"[Client {client_id:02d}] ❌ Status: {resp.status_code} | Error: {resp.text[:50]}")
            
    except Exception as e:
        print(f"[Client {client_id:02d}] ❌ Connection Error: {e}")

def run_load_test():
    print(f"=== Starting HTTP Load Test with {CONCURRENT_REQUESTS} Concurrent Requests ===")
    print(f"Target: {TARGET_HOST}:{TARGET_PORT}")
    
    threads = []
    start_total = time.time()

    for i in range(CONCURRENT_REQUESTS):
        t = threading.Thread(target=send_request, args=(i+1,))
        threads.append(t)
        t.start()
        time.sleep(0.01) 

    for t in threads:
        t.join()

    total_time = time.time() - start_total
    print("=" * 50)
    print(f"Test Completed in {total_time:.2f} seconds")
    print(f"Throughput: {CONCURRENT_REQUESTS / total_time:.2f} req/sec")
    print("=" * 50)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        TARGET_HOST = sys.argv[1]
    if len(sys.argv) > 2:
        TARGET_PORT = int(sys.argv[2])
    run_load_test()