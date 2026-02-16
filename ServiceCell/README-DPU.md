# DPUmesh - DPU-accelerated Service Mesh for Microservices

DPUmesh는 DPU(Data Processing Unit)를 활용하여 마이크로서비스 간 통신을 가속화하는 서비스 메시 시뮬레이션입니다.

## 아키텍처

```
Node
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Worker Pods (namespace: mubench-dpu 또는 mubench-blocking)         │   │
│  │                                                                     │   │
│  │  Pod: s0-worker          Pod: s1-worker          Pod: s2-worker    │   │
│  │  (host_lib.py)           (host_lib.py)           (host_lib.py)     │   │
│  │       │                       │                       │            │   │
│  └───────┼───────────────────────┼───────────────────────┼────────────┘   │
│          │                       │                       │                 │
│          └───────────────────────┴───────────────────────┘                 │
│                                  │                                          │
│                          TCP (localhost:5010)                               │
│                                  ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  DaemonSet: dpa-daemon (namespace: dpumesh-system)                  │   │
│  │  - DMA Manager 역할 (실제: DPA 하드웨어에서 실행)                    │   │
│  │  - Worker ↔ DPU 간 데이터 전송                                      │   │
│  │  - TCP Bridge (포트 5005) - 외부 요청 수신                          │   │
│  │       │                                                            │   │
│  │       │ TCP (localhost:5011)                                       │   │
│  │       ▼                                                            │   │
│  │  DaemonSet: dpu-daemon                                             │   │
│  │  - Sidecar / L7 Proxy 역할 (실제: DPU 하드웨어에서 실행)            │   │
│  │  - 외부 서비스 HTTP 호출                                            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 실행 모드

| 모드 | 설명 | 사용 시나리오 |
|------|------|--------------|
| **Non-blocking (Graph)** | 이벤트 루프 기반, 비동기 처리 | 높은 동시성, I/O 바운드 워크로드 |
| **Blocking** | 스레드풀 기반, 동기 처리 | 간단한 요청-응답, CPU 바운드 워크로드 |

## 빠른 시작

### 1. 사전 요구사항

- Kubernetes 클러스터
- Docker
- containerd

### 2. 클론

```bash
git clone https://github.com/your-repo/TNET_muBench.git
cd TNET_muBench
```

### 3. 마이크로서비스 구조 설정

`workmodel.json` 파일을 수정하여 서비스 구조와 연산량을 설정합니다:

```json
{
  "s0": {
    "external_services": [
      {"seq_len": 1, "services": ["s1"]},
      {"seq_len": 1, "services": ["s2"]}
    ],
    "internal_service": {
      "loader": {
        "cpu_stress": {
          "run": true,
          "range_complexity": [1000, 1000],
          "thread_pool_size": 1,
          "trials": 1
        },
        "memory_stress": {
          "run": false,
          "memory_size": 10000,
          "memory_io": 1000
        },
        "sleep_stress": {
          "run": false,
          "sleep_time": 0.01
        },
        "mean_response_size": 100,
        "function_id": "f1"
      }
    },
    "url": "s0.mubench-dpu.svc.cluster.local",
    "path": "/api/v1"
  },
  "s1": {
    "external_services": [{"seq_len": 1, "services": []}],
    "internal_service": {
      "loader": {
        "sleep_stress": {"run": true, "sleep_time": 0.01},
        "mean_response_size": 100,
        "function_id": "f1"
      }
    },
    "url": "s1.mubench-dpu.svc.cluster.local",
    "path": "/api/v1"
  },
  "s2": {
    "external_services": [{"seq_len": 1, "services": []}],
    "internal_service": {
      "loader": {
        "sleep_stress": {"run": true, "sleep_time": 0.01},
        "mean_response_size": 100,
        "function_id": "f1"
      }
    },
    "url": "s2.mubench-dpu.svc.cluster.local",
    "path": "/api/v1"
  }
}
```

#### 서비스 구조 설정

- `external_services`: 호출할 하위 서비스 목록
  - `seq_len`: 순차 호출 길이
  - `services`: 호출할 서비스 이름 리스트

#### 연산량 설정 (Internal Service)

| 옵션 | 설명 |
|------|------|
| `cpu_stress` | CPU 부하 시뮬레이션 |
| `memory_stress` | 메모리 I/O 시뮬레이션 |
| `disk_stress` | 디스크 I/O 시뮬레이션 |
| `sleep_stress` | 지연 시뮬레이션 |
| `mean_response_size` | 응답 크기 (바이트) |

---

## 배포 가이드

### Step 1: DPA/DPU DaemonSet 배포 (최초 1회)

```bash
cd ~/TNET_muBench

# DPA 이미지 빌드
docker build -t dpumesh-dpa:latest -f docker/Dockerfile.dpa .
docker save dpumesh-dpa:latest | sudo ctr -n k8s.io images import -

# DPU 이미지 빌드
docker build -t dpumesh-dpu:latest -f docker/Dockerfile.dpu .
docker save dpumesh-dpu:latest | sudo ctr -n k8s.io images import -

# DaemonSet 배포
kubectl apply -f k8s/dpa-daemonset.yaml
kubectl apply -f k8s/dpu-daemonset.yaml

# 확인 (둘 다 Running 상태여야 함)
kubectl get pods -n dpumesh-system
```

예상 출력:
```
NAME               READY   STATUS    RESTARTS   AGE
dpa-daemon-xxxxx   1/1     Running   0          30s
dpu-daemon-xxxxx   1/1     Running   0          30s
```

### Step 2: Worker 이미지 빌드

```bash
docker build -t mubench-worker:latest -f ServiceCell/Dockerfile.dpu .
docker save mubench-worker:latest | sudo ctr -n k8s.io images import -
```

---

## 테스트 가이드

### 중요: 한 번에 하나의 모드만 테스트

DPA가 하나이므로 **Non-blocking과 Blocking을 동시에 배포하면 안 됩니다.**

---

### Non-blocking (Graph) 모드 테스트

```bash
# 1. Blocking Worker가 있다면 삭제
kubectl delete -f k8s/workers-blocking-deployment.yaml 2>/dev/null

# 2. Non-blocking Worker 배포
kubectl apply -f k8s/workers-deployment.yaml

# 3. Pod 상태 확인 (모두 Running 될 때까지 대기)
kubectl get pods -n mubench-dpu -w

# 4. DPA에 Worker 등록 확인
kubectl logs -n dpumesh-system -l app=dpa-daemon --tail=20

# 5. 테스트 실행
python3 test_client.py localhost 5005
```

예상 출력:
```
=== Starting Load Test with 50 Concurrent Requests ===
Target: localhost:5005
[Client 01] ✅ Status: 200 | ReqID: xxx | Latency: 45.23ms
[Client 02] ✅ Status: 200 | ReqID: xxx | Latency: 52.11ms
...
==================================================
Test Completed in 0.85 seconds
Throughput: 58.82 req/sec
==================================================
```

---

### Blocking 모드 테스트

```bash
# 1. Non-blocking Worker 삭제
kubectl delete -f k8s/workers-deployment.yaml

# 2. Blocking Worker 배포
kubectl apply -f k8s/workers-blocking-deployment.yaml

# 3. Pod 상태 확인 (모두 Running 될 때까지 대기)
kubectl get pods -n mubench-blocking -w

# 4. DPA에 Worker 등록 확인
kubectl logs -n dpumesh-system -l app=dpa-daemon --tail=30

# 5. 테스트 실행
python3 test_client.py localhost 5005
```

---

### 단일 요청 테스트 (디버깅용)

```bash
python3 -c "
import socket
import json

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.connect(('localhost', 5005))

req = {
    'method': 'POST',
    'path': '/api/v1',
    'headers': {'Content-Type': 'application/json'},
    'body': json.dumps({'s0': [{'s1': {}, 's2': {}}]}),
    'query_string': ''
}
sock.sendall(json.dumps(req).encode())
print('Request sent, waiting...')

sock.settimeout(10)
try:
    resp = sock.recv(4096)
    print('Response:', resp.decode())
except socket.timeout:
    print('Timeout!')
sock.close()
"
```

---

## 로그 확인

```bash
# DPA 로그 (TCP Bridge, Worker 연결)
kubectl logs -n dpumesh-system -l app=dpa-daemon -f

# DPU 로그 (외부 HTTP 호출)
kubectl logs -n dpumesh-system -l app=dpu-daemon -f

# Worker 로그 (Non-blocking)
kubectl logs -n mubench-dpu -l app=s0-worker -f

# Worker 로그 (Blocking)
kubectl logs -n mubench-blocking -l app=s0-worker-blocking -f
```

---

## 트러블슈팅

### 1. Connection refused

```
[Client 01] ❌ Connection Error: [Errno 111] Connection refused
```

**원인**: DPA가 실행 중이 아니거나, 포트가 열리지 않음

**해결**:
```bash
kubectl get pods -n dpumesh-system
kubectl logs -n dpumesh-system -l app=dpa-daemon
```

### 2. Timeout (응답 없음)

**원인**: Worker가 DPA에 등록되지 않음

**해결**:
```bash
# DPA 로그에서 Worker 등록 확인
kubectl logs -n dpumesh-system -l app=dpa-daemon | grep "Worker registered"

# Worker 재시작
kubectl delete pods -n mubench-dpu --all
```

### 3. Worker Crash

```
[Worker-0] Crash: ...
```

**해결**:
```bash
kubectl logs -n mubench-dpu -l app=s0-worker --tail=50
```

### 4. 두 모드 동시 배포 시 문제

**원인**: DPA가 하나인데 두 모드의 Worker가 모두 연결됨

**해결**: 한 모드만 배포
```bash
kubectl delete -f k8s/workers-deployment.yaml
# 또는
kubectl delete -f k8s/workers-blocking-deployment.yaml
```

---

## 정리

```bash
# Worker 삭제
kubectl delete -f k8s/workers-deployment.yaml
kubectl delete -f k8s/workers-blocking-deployment.yaml

# DaemonSet 삭제
kubectl delete -f k8s/dpa-daemonset.yaml
kubectl delete -f k8s/dpu-daemonset.yaml

# Namespace 삭제
kubectl delete namespace mubench-dpu
kubectl delete namespace mubench-blocking
kubectl delete namespace dpumesh-system
```

---

## 디렉토리 구조

```
TNET_muBench/
├── ServiceCell/
│   ├── dpumesh/                          # DPUmesh 라이브러리
│   │   ├── __init__.py
│   │   ├── common.py                     # 공유 데이터 구조
│   │   ├── host_lib.py                   # Worker용 API
│   │   ├── dpa_daemon.py                 # DPA DaemonSet
│   │   ├── dpu_daemon.py                 # DPU DaemonSet
│   │   ├── requests.py                   # HTTP 클라이언트
│   │   └── server.py                     # 서버 로직
│   │
│   ├── CellController-mp_dpu.py          # Non-blocking 컨트롤러
│   ├── CellController-mp_dpu_blocking.py # Blocking 컨트롤러
│   ├── ExternalServiceExecutor_dpu.py    # Non-blocking Executor
│   ├── ExternalServiceExecutor_dpu_blocking.py  # Blocking Executor
│   ├── InternalServiceExecutor.py        # 연산량 시뮬레이션
│   └── Dockerfile.dpu                    # Worker 이미지
│
├── docker/
│   ├── Dockerfile.dpa                    # DPA 이미지
│   └── Dockerfile.dpu                    # DPU 이미지
│
├── k8s/
│   ├── dpa-daemonset.yaml               # DPA DaemonSet
│   ├── dpu-daemonset.yaml               # DPU DaemonSet
│   ├── workers-deployment.yaml          # Non-blocking Workers
│   └── workers-blocking-deployment.yaml # Blocking Workers
│
├── workmodel.json                        # 서비스 구조 설정
├── test_client.py                        # 테스트 클라이언트
└── README.md                             # 이 문서
```

---

## 환경 변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `APP` | 서비스 이름 (s0, s1, s2 등) | - |
| `PN` | Worker 프로세스 수 | 4 |
| `TN` | Worker당 스레드 수 (Blocking) | 4 |
| `DPA_HOST` | DPA Daemon 주소 | Node의 hostIP |

---

## License

MIT License