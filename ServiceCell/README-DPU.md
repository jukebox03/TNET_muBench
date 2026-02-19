# DPUmesh - DPU-accelerated Service Mesh for Microservices

DPUmesh는 DPU(Data Processing Unit)를 활용하여 마이크로서비스 간 통신을 가속화하는 서비스 메시 시뮬레이션입니다. Execution Graph 기반 non-blocking 이벤트 루프로 높은 동시성을 지원합니다.

## 아키텍처

```
Node
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Worker Pods (namespace: mubench-dpu)                               │   │
│  │                                                                     │   │
│  │  Pod: s0-worker          Pod: s1-worker          Pod: s2-worker    │   │
│  │  (host_lib.py)           (host_lib.py)           (host_lib.py)     │   │
│  │       │                       │                       │            │   │
│  └───────┼───────────────────────┼───────────────────────┼────────────┘   │
│          │                       │                       │                 │
│          └───────────────────────┴───────────────────────┘                 │
│                                  │                                          │
│                            /dev/shm (SHM Queue)                            │
│                                  │                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  DaemonSet (namespace: dpumesh-system)                              │   │
│  │                                                                     │   │
│  │  dpa-daemon                                                        │   │
│  │  - DMA Manager 역할 (실제: DPA 하드웨어에서 실행)                    │   │
│  │  - Worker ↔ Sidecar 간 SHM Queue 데이터 전송                       │   │
│  │                                                                     │   │
│  │  dpu-daemon                                                        │   │
│  │  - Sidecar / L7 Proxy 역할 (실제: DPU 하드웨어에서 실행)            │   │
│  │  - TCP Bridge (포트 5005) - 외부 요청 수신                          │   │
│  │  - 로컬 라우팅: 같은 노드 Worker → SHM Queue                       │   │
│  │  - HTTP Fallback: Worker 없는 서비스 → HTTP 호출                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 데이터 흐름

```
외부 요청 → TCP Bridge(Sidecar:5005)
          → sidecar_cq → DMA Manager → worker_sq → Worker 처리
          → worker_cq → DMA Manager → sidecar_sq → Sidecar
          → (Egress인 경우) 대상 Worker로 라우팅
          → (응답인 경우) TCP Bridge → 외부 클라이언트
```

---

## Execution Graph

마이크로서비스의 실행 흐름을 **Execution Graph**로 선언적으로 정의합니다. Flask handler는 graph를 빌드하고 등록만 하며, 실제 실행은 Server event loop이 non-blocking으로 처리합니다.

### 설계 철학

- **지능은 Context에, 실행력은 Server에**: Server는 context가 시키는 대로만 실행
- **Non-blocking**: EgressStep은 SHM Queue, InternalStep은 ThreadPool로 실행
- **Worker를 점유하지 않음**: Flask handler는 graph 등록 후 즉시 return

### 기본 요소

| 요소 | 역할 | 실행 방식 |
|------|------|-----------|
| `order([...])` | 순차 실행 | 안의 step들을 순서대로 실행 |
| `pipe([...])` | 병렬 실행 | 안의 step들을 동시에 실행 |
| `EgressStep` | 외부 서비스 호출 | SHM Queue → Sidecar (non-blocking) |
| `InternalStep` | 내부 함수 실행 | ThreadPool (non-blocking) |

### `order(steps)` - 순차 실행

안의 step들을 **순서대로** 실행합니다. 이전 step이 완료되어야 다음 step이 시작됩니다.

```python
from dpumesh.server import order, EgressStep, InternalStep

# A 완료 → B 완료 → C 완료
order([
    EgressStep("A", requests=[("GET", "http://svc-a/api", {}, None)]),
    InternalStep("B", func=process),
    EgressStep("C", requests=[("POST", "http://svc-c/api", {}, None)]),
])
```

### `pipe(steps)` - 병렬 실행

안의 step들을 **동시에** 실행합니다. 모든 step이 완료되어야 다음으로 넘어갑니다.

```python
from dpumesh.server import pipe, EgressStep

# A, B, C 동시 실행
pipe([
    EgressStep("A", requests=[("GET", "http://svc-a/api", {}, None)]),
    EgressStep("B", requests=[("GET", "http://svc-b/api", {}, None)]),
    EgressStep("C", requests=[("GET", "http://svc-c/api", {}, None)]),
])
```

### `EgressStep` - 외부 서비스 호출

```python
EgressStep(
    id="fetch_user",                          # 고유 ID (결과 참조에 사용)
    requests=[                                 # (method, url, headers, body) 리스트
        ("GET", "http://user-svc/api/v1", {"Authorization": "Bearer ..."}, None),
    ],
    inputs=["prev_step"],                      # (선택) 이전 step 결과 참조
    request_builder=build_fn,                  # (선택) 동적 request 빌드 함수
)
```

**정적 모드**: `requests`에 직접 지정

```python
EgressStep(
    id="get_user",
    requests=[("GET", "http://user-svc/api/v1", {}, None)],
)
```

**동적 모드**: 이전 step 결과를 받아서 request 생성

```python
def build_save_request(process_result):
    url = f"http://storage/api/v1/{process_result.body}"
    return [("POST", url, {}, process_result.body)]

EgressStep(
    id="save",
    request_builder=build_save_request,
    inputs=["process_result"],
)
```

**순차 requests**: 하나의 EgressStep에 여러 request를 넣으면 순차 실행됩니다.

```python
EgressStep(
    id="chain",
    requests=[
        ("GET", "http://svc-a/api", {}, None),   # 먼저 실행
        ("POST", "http://svc-b/api", {}, None),  # a 완료 후 실행
    ],
)
```

### `InternalStep` - 내부 함수 실행

```python
InternalStep(
    id="merge",                    # 고유 ID
    func=merge_results,            # 실행할 함수
    args=(config,),                # (선택) 위치 인자
    kwargs={"timeout": 30},        # (선택) 키워드 인자
    inputs=["fetch_user", "fetch_order"],  # (선택) 이전 step 결과 참조
)
```

**함수 시그니처**: `inputs`에 명시된 결과가 keyword argument로 전달됩니다.

```python
def merge_results(fetch_user, fetch_order, config, timeout=30):
    """
    fetch_user: StepResult (from EgressStep "fetch_user")
    fetch_order: StepResult (from EgressStep "fetch_order")
    config: args로 전달된 고정 매개변수
    timeout: kwargs로 전달된 키워드 매개변수
    """
    user_data = json.loads(fetch_user.body)
    order_data = json.loads(fetch_order.body)
    return {"merged": {**user_data, **order_data}}
```

**반환값**: 함수의 반환값은 자동으로 `StepResult`로 변환됩니다.

| 반환 타입 | 변환 |
|-----------|------|
| `StepResult` | 그대로 사용 |
| `str` | `StepResult(body=str)` |
| `dict` | `StepResult(body=dict.body, status_code=dict.status_code)` |
| `Exception` | `StepResult(status_code=500, error=str(e))` |
| 기타 | `StepResult(body=str(value))` |

### `StepResult` - Step 실행 결과

```python
@dataclass
class StepResult:
    status_code: int = 200
    headers: Dict[str, str] = {}
    body: str = ""
    error: Optional[str] = None
    
    @property
    def ok(self) -> bool:  # 200-399면 True
```

### 중첩 (Nesting)

`order`와 `pipe`는 **자유롭게 중첩** 가능합니다.

**예제 1: 기본 패턴 (Internal → External)**

μBench 기본 패턴입니다.

```python
order([
    InternalStep("compute", func=run_internal_service, args=(config,)),
    pipe([
        EgressStep("call_a", requests=[...]),
        EgressStep("call_b", requests=[...]),
    ]),
])
```

```
compute → [call_a, call_b] (병렬)
```

**예제 2: Fan-out / Fan-in**

여러 서비스를 병렬 호출 후 결과를 합칩니다.

```python
order([
    pipe([
        EgressStep("fetch_user", requests=[("GET", user_url, {}, None)]),
        EgressStep("fetch_order", requests=[("GET", order_url, {}, None)]),
    ]),
    InternalStep("merge", func=merge_fn, inputs=["fetch_user", "fetch_order"]),
])
```

```
[fetch_user, fetch_order] (병렬) → merge(결과 합침)
```

**예제 3: 복잡한 Pipeline**

```python
order([
    pipe([
        order([                           # Pipeline A
            EgressStep("fetch_user", requests=[...]),
            InternalStep("parse_user", func=parse, inputs=["fetch_user"]),
            EgressStep("fetch_detail", request_builder=build_detail, inputs=["parse_user"]),
        ]),
        order([                           # Pipeline B
            EgressStep("fetch_order", requests=[...]),
            EgressStep("fetch_payment", requests=[...]),
        ]),
        InternalStep("compute", func=compute),
    ]),
    InternalStep("merge", func=merge_all, 
                 inputs=["fetch_detail", "fetch_payment", "compute"]),
    EgressStep("save", request_builder=build_save, inputs=["merge"]),
    InternalStep("finalize", func=build_response, inputs=["save"]),
])
```

```
┌─ fetch_user → parse_user → fetch_detail ─┐
├─ fetch_order → fetch_payment             ─┼→ merge → save → finalize
└─ compute                                 ─┘
```

### Flask handler에서 graph 등록

```python
from dpumesh.server import get_current_context, order, pipe, EgressStep, InternalStep

@app.route('/api/v1', methods=['GET', 'POST'])
def handler():
    ctx = get_current_context()
    
    if ctx is not None:
        ctx.graph = order([
            InternalStep("preprocess", func=preprocess),
            pipe([
                EgressStep("svc_a", requests=[("GET", "http://svc-a/api", {}, None)]),
                EgressStep("svc_b", requests=[("GET", "http://svc-b/api", {}, None)]),
            ]),
            InternalStep("postprocess", func=postprocess, 
                         inputs=["svc_a", "svc_b"]),
        ])
    
    return ""
```

### Server Event Loop

```
1. TCP 요청 → Sidecar → SHM Queue → DMA Manager → Worker SQ

2. Server event loop:
   a. SHM에서 ingress 요청 읽음
   b. Flask handler 실행 → ctx.graph 등록됨
   c. ctx.start() → 초기 Action 리스트 반환
   d. Action 실행:
      - EGRESS → SHM Queue에 넣음 (non-blocking)
      - INTERNAL → ThreadPool에 넣음 (non-blocking)
   e. 이벤트 도착 (egress 응답 / threadpool 완료)
      → ctx.on_result() → 다음 Action 리스트 반환 → 반복
   f. DONE Action → HTTP 응답 전송

3. Worker CQ → DMA Manager → Sidecar → TCP 응답
```

### 에러 처리

- **EgressStep 실패** (status_code >= 400): 해당 step 에러, order/pipe 부모로 전파
- **InternalStep 예외**: Exception → StepResult(error=...) 변환, 부모로 전파
- **order 내 에러**: 즉시 중단, 부모에 에러 전파
- **pipe 내 에러**: 나머지 step은 계속 실행, 하나라도 에러면 pipe 에러

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

### Step 2: Worker 이미지 빌드

```bash
docker build -t mubench-worker:latest -f ServiceCell/Dockerfile.dpu .
docker save mubench-worker:latest | sudo ctr -n k8s.io images import -
```

### Step 3: Worker 배포

```bash
kubectl apply -f k8s/workers-deployment.yaml

# Pod 상태 확인 (모두 Running 될 때까지 대기)
kubectl get pods -n mubench-dpu -w

# DPA에 Worker 등록 확인
kubectl logs -n dpumesh-system -l app=dpa-daemon --tail=20
```

### Step 4: 테스트 실행

```bash
python3 test_client.py localhost 5005
```

### 단일 요청 테스트 (디버깅용)

```bash
python3 -c "
import socket, json

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

## 마이크로서비스 구조 설정

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
        "sleep_stress": { "run": false, "sleep_time": 0.01 },
        "mean_response_size": 100,
        "function_id": "f1"
      }
    },
    "url": "s0.mubench-dpu.svc.cluster.local",
    "path": "/api/v1"
  }
}
```

### 서비스 구조

- `external_services`: 호출할 하위 서비스 목록
  - `seq_len`: 순차 호출 길이
  - `services`: 호출할 서비스 이름 리스트

### 연산량 (Internal Service)

| 옵션 | 설명 |
|------|------|
| `cpu_stress` | CPU 부하 시뮬레이션 |
| `memory_stress` | 메모리 I/O 시뮬레이션 |
| `disk_stress` | 디스크 I/O 시뮬레이션 |
| `sleep_stress` | 지연 시뮬레이션 |
| `mean_response_size` | 응답 크기 (바이트) |

---

## 로그 확인

```bash
# DPA 로그 (DMA Manager, Worker 연결)
kubectl logs -n dpumesh-system -l app=dpa-daemon -f

# DPU 로그 (Sidecar, TCP Bridge)
kubectl logs -n dpumesh-system -l app=dpu-daemon -f

# Worker 로그
kubectl logs -n mubench-dpu -l app=s0-worker -f
```

---

## 트러블슈팅

### Connection refused

**원인**: DPA가 실행 중이 아니거나 포트가 열리지 않음

```bash
kubectl get pods -n dpumesh-system
kubectl logs -n dpumesh-system -l app=dpa-daemon
```

### 504 Gateway Timeout

**원인 1**: Worker가 DPA에 등록되지 않음

```bash
kubectl logs -n dpumesh-system -l app=dpa-daemon | grep "Worker discovered"
```

**원인 2**: stale SHM 파일로 인해 죽은 Worker로 라우팅

```bash
# SHM 정리 후 전체 재시작
kubectl exec -n dpumesh-system $(kubectl get pod -n dpumesh-system -l app=dpu-daemon -o name | head -1) -- bash -c "rm -f /dev/shm/dpumesh_*"
kubectl rollout restart -n dpumesh-system deployment/dpu-daemon
kubectl rollout restart -n dpumesh-system deployment/dpa-daemon
kubectl rollout restart -n mubench-dpu deployment/s0-worker deployment/s1-worker deployment/s2-worker
```

---

## 정리

```bash
kubectl delete -f k8s/workers-deployment.yaml
kubectl delete -f k8s/dpa-daemonset.yaml
kubectl delete -f k8s/dpu-daemonset.yaml
kubectl delete namespace mubench-dpu
kubectl delete namespace dpumesh-system
```

---

## 디렉토리 구조

```
TNET_muBench/
├── ServiceCell/
│   ├── dpumesh/                          # DPUmesh 라이브러리
│   │   ├── __init__.py
│   │   ├── common.py                     # SHM Queue, QueueEntry 등 공유 구조
│   │   ├── host_lib.py                   # Worker용 API (Egress/Ingress)
│   │   ├── dpa_daemon.py                 # DMA Manager (DPA 하드웨어 시뮬레이션)
│   │   ├── dpu_daemon.py                 # Sidecar (DPU 하드웨어 시뮬레이션)
│   │   ├── requests.py                   # HTTP 클라이언트 호환 레이어
│   │   └── server.py                     # Execution Graph 엔진 + Server
│   │
│   ├── CellController-mp_dpu.py          # Flask 컨트롤러 (Graph 빌드)
│   ├── ExternalServiceExecutor_dpu.py    # Egress Graph 빌더
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
│   └── workers-deployment.yaml          # Worker Deployments
│
├── workmodel.json                        # 서비스 구조 설정
├── test_client.py                        # 테스트 클라이언트
└── README.md
```

---

## 환경 변수

| 변수 | 설명 | 기본값 | 설정 위치 |
|------|------|--------|-----------|
| `APP` | 서비스 이름 (s0, s1, s2 등) | - | Deployment YAML |
| `PN` | Worker 프로세스 수 | `4` | Deployment YAML |

---

## License

MIT License