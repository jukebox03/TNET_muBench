# muBench-DPU: DPU-Accelerated Microservice Benchmark

This project demonstrates a **DPU-accelerated microservice architecture** simulation running on Kubernetes.

It abstracts the network processing and external service calls into a specialized library (`dpumesh`), simulating the behavior of a Data Processing Unit (DPU) Sidecar using Python threads and queues.

## Architecture

The system mimics a hardware offload architecture using a **Queue-based** communication model:

1.  **Application Layer:** Runs the business logic (Flask-based) but does NOT perform network I/O directly. It writes requests to shared memory queues.
2.  **DPU Simulation Layer (`dpumesh`):**
    *   **Ingress:** A TCP Bridge (Port 5005) receives external traffic and places it into the Ingress Queue.
    *   **Egress:** A multi-threaded simulator consumes requests from the Egress Queue, mocks network latency (e.g., 50ms), and returns responses via the Egress CQ.
    *   **Zero-Copy Simulation:** Uses in-process Python Queues to simulate DMA transfers between Host and DPU.

## Prerequisites

*   **Docker**
*   **Kubernetes Cluster** (Minikube, Kind, or a bare-metal cluster)
*   **Python 3.9+** (for running the test client)

## Build & Deploy

### 1. Build Docker Image

Build the DPU-enabled application image from the project root.

```bash
# Run from the root directory (TNET_muBench/)
docker build -t mubench-dpu:latest -f ServiceCell/Dockerfile.dpu ServiceCell/
```

> **Note:** Since the deployment uses `imagePullPolicy: Never`, ensure this image exists on your Kubernetes node (or load it into Minikube/Kind).
> *   Minikube: `minikube image load mubench-dpu:latest`
> *   Kind: `kind load docker-image mubench-dpu:latest`

### 2. Create Namespace

```bash
kubectl create namespace mubench-dpu
```

### 3. Deploy Application & Service

Deploy the application (Deployment) and the Load Balancer (NodePort Service).

```bash
# 1. Deploy the Pods (Default Replicas: 1)
kubectl apply -f ServiceCell/k8s-dpu-deploy.yaml

# 2. Expose via NodePort (Port 30005)
kubectl apply -f ServiceCell/k8s-dpu-svc.yaml
```

### 4. Scale (Optional)

To observe the effect of parallel processing and load balancing, scale the deployment.

```bash
kubectl scale deployment s0-dpu --replicas=3 -n mubench-dpu
```

Verify the pods are running:

```bash
kubectl get pods -n mubench-dpu
```

---

## Testing

The service exposes **Port 30005** on the node. You can run the provided python client to generate concurrent traffic.

### 1. Run Test Client

Create a file named `test_client.py` (or use the existing one) and run it:

```bash
python3 test_client.py
```

### 2. Expected Output

You should see requests being handled by different replicas (if scaled) and processed in parallel.

```text
=== Starting Load Test with 50 Concurrent Requests ===
Target: localhost:30005
[Client 01] ✅ Status: 200 | ReqID: 301 | Latency: 94.81ms
[Client 02] ✅ Status: 200 | ReqID: 304 | Latency: 152.97ms
...
```

*   **Latency:** Reduced significantly due to the multi-threaded Egress simulation.
*   **Throughput:** Increases linearly with the number of replicas.

## Project Structure

*   `ServiceCell/CellController-mp_dpu.py`: Main entry point (DPU-enabled Controller).
*   `ServiceCell/dpumesh/`: The core library simulating the DPU hardware.
    *   `dpu_lib.py`: Hardware abstraction layer (Queues, TCP Bridge, Egress Simulator).
    *   `server.py`: Non-blocking Event Loop server.
*   `ServiceCell/k8s-dpu-*.yaml`: Kubernetes manifests.

## Configuration

You can modify the simulation parameters in `ServiceCell/dpumesh/dpu_lib.py`:

*   `time.sleep(0.05)`: Adjusts the simulated network latency for external calls.
