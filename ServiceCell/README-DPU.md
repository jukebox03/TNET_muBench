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

**SHM Queue 이름 규칙:**

| Queue | 방향 | 용도 |
|-------|------|------|
| `worker_sq` | DMA Manager → Worker | Worker가 요청/응답 수신 |
| `worker_cq` | Worker → DMA Manager | Worker가 요청/응답 송신 |
| `sidecar_sq` | DMA Manager → Sidecar | Sidecar가 응답 수신 |
| `sidecar_cq` | Sidecar → DMA Manager | Sidecar가 요청 송신 |

---

## Execution Graph

마이크로서비스의 실행 흐름을 **Execution Graph**로 선언적으로 정의합니다. Flask handler는 graph를 빌드하고 등록만 하며, 실제 실행은 Server event loop이 non-blocking으로 처리합니다.

### 설계 철학

- **지능은 Context에, 실행력은 Server에**: Server는 context가 시키는 대로만 실행
- **Non-blocking**: EgressStep은 SHM Queue, InternalStep은 ThreadPool로 실행
- **Worker를 점유하지 않음**: Flask handler는 graph 등록 후 즉시 return

### 핵심 개념

| 요소 | 역할 | 설명 |
|------|------|------|
| **CellController** | 애플리케이션 진입점 | Flask 기반. 요청마다 `ExecutionContext`를 생성하고 작업을 정의 |
| **ExecutionContext (`ctx`)** | 요청 생명주기 관리 | Step을 추가하여 실행할 작업을 예약 |
| **InternalStep** | 내부 함수 실행 | CPU 연산 등. ThreadPool에서 non-blocking 실행 |
| **EgressStep** | 외부 서비스 호출 | HTTP 요청. SHM Queue → Sidecar로 non-blocking 실행 |
| **StepResult** | Step 실행 결과 | 이전 Step의 결과를 다음 Step에 전달 |

### 기본 요소

| 요소 | 역할 | 실행 방식 |
|------|------|-----------|
| `order([...])` | 순차 실행 | 안의 step들을 순서대로 실행 |
| `pipe([...])` | 병렬 실행 | 안의 step들을 동시에 실행 |
| `EgressStep` | 외부 서비스 호출 | SHM Queue → Sidecar (non-blocking) |
| `InternalStep` | 내부 함수 실행 | ThreadPool (non-blocking) |

---

## High-Level API Reference

개발자는 주로 Flask 핸들러 내부에서 다음 API들을 사용합니다.

### `dpumesh.get_context()`

현재 요청에 대한 `ExecutionContext` 객체를 반환합니다. 반드시 Flask 라우트 핸들러 내부에서 호출해야 합니다.

> **참고**: `get_current_context()`는 동일한 함수의 별칭(alias)입니다.

### `ctx.run_internal(func, *, args=(), kwargs={}, inputs=[], id=None)`

내부 함수(Python 함수) 실행을 예약합니다. 별도의 Worker Thread에서 비동기로 실행됩니다.

| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `func` | Callable | 실행할 함수 객체 |
| `args` | tuple | 함수에 전달할 위치 인자 |
| `kwargs` | dict | 함수에 전달할 키워드 인자 |
| `inputs` | list[str] | 의존하는 이전 Step의 ID 리스트. 지정된 Step들이 완료되어야 실행 |
| `id` | str (optional) | Step의 고유 ID. 생략 시 자동 생성 |

**반환값**: 생성된 Step ID (문자열). 다른 Step의 `inputs`로 사용 가능.

### `ctx.call(method, url, *, headers={}, body=None, inputs=[], request_builder=None, id=None)`

외부 서비스로의 HTTP 요청(Egress)을 예약합니다.

| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `method` | str | HTTP 메서드 ("GET", "POST" 등) |
| `url` | str | 대상 서비스 URL |
| `headers` | dict | 요청 헤더 |
| `body` | str/bytes | 요청 본문 |
| `inputs` | list[str] | 의존하는 이전 Step의 ID 리스트 |
| `request_builder` | Callable (optional) | 동적 요청 생성 함수 (고급 기능 참조) |
| `id` | str (optional) | Step의 고유 ID |

**반환값**: 생성된 Step ID (문자열).

### `with ctx.parallel():`

병렬 실행 블록을 정의하는 Context Manager입니다. 블록 내부에서 호출된 `run_internal`이나 `call`은 서로 의존성 없이 **동시에** 실행됩니다.

```python
with ctx.parallel():
    ctx.call("GET", "http://service-a")
    ctx.call("GET", "http://service-b")
# 두 요청이 동시에 전송됨
```

### 기본 사용 예제

```python
from dpumesh.server import get_context

@app.route('/api/v1', methods=['GET', 'POST'])
def handler():
    ctx = get_context()
    
    if ctx is not None:
        # Step 1: 내부 연산 (ThreadPool)
        ctx.run_internal(process_data, args=(config,), id="process")
        
        # Step 2: 외부 호출 (병렬, SHM Queue)
        with ctx.parallel():
            ctx.call("GET", "http://svc-a/api/v1")
            ctx.call("GET", "http://svc-b/api/v1")
    
    return ""  # 빈 응답 필수 — Server가 graph 실행 후 실제 응답으로 교체
```

---

## 고급 기능 (Advanced Features)

### 데이터 체이닝 (Data Chaining)

이전 Step의 결과를 다음 Step에서 사용하려면 `inputs` 파라미터를 사용합니다. `inputs`에 지정된 Step ID가 **키워드 인자(Keyword Argument)**로 전달됩니다. 전달되는 값은 `StepResult` 객체입니다.

```python
def process_user_data(user_step):
    # user_step은 StepResult 객체
    return f"Hello, {user_step.body}"

@app.route("/chain")
def chain_handler():
    ctx = get_context()
    
    # 1단계: 사용자 정보 가져오기 (ID: "get_user")
    step1 = ctx.call("GET", "http://user-service/me", id="get_user")
    
    # 2단계: 데이터 가공 (1단계 결과에 의존)
    # process_user_data(get_user=StepResult(...)) 형태로 호출됨
    step2 = ctx.run_internal(process_user_data, inputs=[step1])
    
    return ""
```

### 동적 요청 생성 (Dynamic Request Builder)

URL이나 Body가 이전 단계의 결과에 따라 결정되어야 할 때 `request_builder`를 사용합니다.

- **함수 시그니처**: `def builder(**inputs) -> list[tuple]`
- **반환값**: `[(method, url, headers, body)]` 형태의 튜플 리스트

```python
def build_req(config_step):
    target_host = config_step.body.strip()
    return [("POST", f"http://{target_host}/api", {}, "data")]

@app.route("/dynamic")
def dynamic_handler():
    ctx = get_context()
    
    s1 = ctx.call("GET", "http://config-service/target", id="config_step")
    
    # url 파라미터는 무시됨 — request_builder의 반환값이 사용됨
    ctx.call("GET", "dummy",
             request_builder=build_req, 
             inputs=[s1])
    return ""
```

### 조건부 실행 (Conditional Execution)

`request_builder`에서 **빈 리스트(`[]`)**를 반환하면 해당 Step은 실행되지 않고 넘어갑니다(Skip).

```python
def check_and_call(auth_step):
    if auth_step.status_code != 200:
        return []  # 인증 실패 시 호출 생략
    return [("GET", "http://secure-service/data", {}, "")]
```

### Fan-Out (1:N 요청)

하나의 `EgressStep`에서 순차적으로 여러 요청을 보낼 수 있습니다. `request_builder`가 여러 개의 튜플을 반환하면 됩니다. (병렬 실행을 원하면 `with ctx.parallel()`을 사용하세요.)

---

## Low-Level API (Manual Graph Construction)

High-Level API(`ctx.run_internal`, `ctx.call`)는 내부적으로 Low-Level API를 사용하여 graph를 구성합니다. 일반적으로는 High-Level API를 권장하지만, graph 구조를 직접 프로그래밍 방식으로 조립해야 할 때 Low-Level API를 사용합니다.

```python
from dpumesh import order, pipe, EgressStep, InternalStep
```

### `order(steps)` - 순차 실행

안의 step들을 **순서대로** 실행합니다. 이전 step이 완료되어야 다음 step이 시작됩니다.

```python
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

**InternalStep 반환값 → StepResult 변환 규칙:**

| 반환 타입 | 변환 |
|-----------|------|
| `StepResult` | 그대로 사용 |
| `str` | `StepResult(body=str)` |
| `dict` | `StepResult(body=dict.body, status_code=dict.status_code)` |
| `Exception` | `StepResult(status_code=500, error=str(e))` |
| 기타 | `StepResult(body=str(value))` |

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

### Low-Level API로 Flask handler에서 graph 등록

```python
from dpumesh.server import get_context, order, pipe, EgressStep, InternalStep

@app.route('/api/v1', methods=['GET', 'POST'])
def handler():
    ctx = get_context()
    
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

---

## Server Event Loop

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

## 주의 사항 및 팁

1. **핸들러 반환값**: Flask 핸들러는 항상 **빈 문자열 `""`**을 반환해야 합니다. 실제 클라이언트로의 응답은 그래프 실행이 모두 완료된 후 `dpumesh`가 자동으로 전송합니다. 마지막 Step의 결과가 최종 응답이 됩니다.
2. **ID 중복 방지**: `id`를 수동으로 지정할 경우, 하나의 요청 내에서 중복되지 않도록 주의하세요. 가급적 자동 생성을 권장합니다.
3. **디버깅**: `run_internal` 함수 내에서 `print()`를 사용하면 로그를 확인할 수 있습니다.
4. **`get_context()` vs `get_current_context()`**: 동일한 함수입니다. `get_context()` 사용을 권장합니다.

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
kubectl rollout restart -n dpumesh-system daemonset/dpu-daemon
kubectl rollout restart -n dpumesh-system daemonset/dpa-daemon
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

## 환경 변수

| 변수 | 설명 | 기본값 | 설정 위치 |
|------|------|--------|-----------|
| `APP` | 서비스 이름 (s0, s1, s2 등) | - | Deployment YAML |
| `ZONE` | Pod Zone 정보 | - | Deployment YAML |
| `K8S_APP` | K8s label app | - | Deployment YAML |
| `PN` | Worker 프로세스 수 | `4` | Deployment YAML |

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

## License

MIT License