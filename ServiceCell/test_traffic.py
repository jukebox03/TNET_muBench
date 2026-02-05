import socket
import json
import time

host = 's0-dpu-svc'
port = 5005

print(f"Sending 30 requests to {host}:{port} ...")

for i in range(30):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((host, port))
        
        req = {
            "method": "GET",
            "path": f"/test-loadbalacing-{i}",
            "headers": {},
            "body": ""
        }
        s.sendall(json.dumps(req).encode())
        
        # Read response to ensure request is processed
        data = s.recv(1024)
        s.close()
        # print(f"Request {i} sent, received: {len(data)} bytes")
    except Exception as e:
        print(f"Error on req {i}: {e}")
    time.sleep(0.05)

print("Done.")