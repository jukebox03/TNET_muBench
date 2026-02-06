import socket
import json
import time
import sys
import threading
import random

# Configuration
CONCURRENT_REQUESTS = 50  # Number of parallel clients
TARGET_HOST = 'localhost'
TARGET_PORT = 30005

def send_request(client_id):
    """
    Sends a single request and measures latency.
    """
    req_data = {
        "method": "POST",
        "path": "/api/v1",
        "headers": {"Content-Type": "application/json"},
        # Trace: s0 calls s1, s2 (Simulates workload)
        "body": json.dumps({"s0": [{"s1": {}, "s2": {}}]}),
        "query_string": ""
    }

    start_time = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((TARGET_HOST, TARGET_PORT))
        
        sock.sendall(json.dumps(req_data).encode())
        
        # Read full response
        buffer = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk: break
            buffer += chunk
            
        sock.close()
        
        latency = (time.time() - start_time) * 1000  # ms
        
        if buffer:
            resp = json.loads(buffer.decode())
            # Print concise output (No Body)
            print(f"[Client {client_id:02d}] ✅ Status: {resp.get('status')} | ReqID: {resp.get('req_id')} | Latency: {latency:.2f}ms")
            return True
        else:
            print(f"[Client {client_id:02d}] ❌ Empty response")
            return False

    except Exception as e:
        print(f"[Client {client_id:02d}] ❌ Connection Error: {e}")
        return False

def run_load_test():
    print(f"=== Starting Load Test with {CONCURRENT_REQUESTS} Concurrent Requests ===")
    print(f"Target: {TARGET_HOST}:{TARGET_PORT}")
    
    threads = []
    start_total = time.time()

    # Launch threads
    for i in range(CONCURRENT_REQUESTS):
        t = threading.Thread(target=send_request, args=(i+1,))
        threads.append(t)
        t.start()
        # Small stagger to simulate burst arrival, not instant collision
        time.sleep(0.01) 

    # Wait for all
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