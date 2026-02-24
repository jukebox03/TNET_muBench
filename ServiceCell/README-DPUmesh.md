# DPUmesh - DPU-accelerated Service Mesh for Microservices

DPUmesh는 BlueField-3 DPU(Data Processing Unit)를 활용하여 마이크로서비스 간 통신을 가속화하는 서비스 메시 라이브러리입니다. Host CPU를 네트워크 처리에서 해방시키고 DPU가 모든 네트워크 처리를 담당하며, Execution Graph 기반 non-blocking 이벤트 루프로 높은 동시성을 지원합니다.

## 아키텍처

### BlueField-3 구조

```text
BlueField-3 (DPU)
├── ARM 코어 (16코어 A78, DPU Sidecar 실행)
├── DPA (RISC-V 16코어, DMA Manager 실행)
├── ConnectX-7 NIC
│   ├── Transport Engine (DMA 실행)
│   ├── eSwitch
│   ├── RDMA 엔진
│   └── Crypto offload (inline encryption/decryption)
├── PCIe Switch (Host ↔ DPU)
└── DPU DRAM (ARM, DPA 공유)

```

### 핵심 원칙

* **Host CPU**: `doca_pe_progress()` + callback 기반, polling 없음
* **DPA**: SQ busy polling
* **ARM**: 이벤트 기반 (notify or `doca_eth_rxq` callback)
* **CQ**: 별도 자료구조 없음, callback이 그 역할 수행

---

## 하드웨어 설계 명세

### Descriptor 구조체 (통일)

모든 데이터 송수신은 아래의 통합된 Descriptor를 사용합니다.

```c
struct sw_descriptor {
    uint64_t header_buf_addr;  // NULL이면 Case 2 (외부→Host)
    uint32_t header_len;
    uint64_t body_buf_addr;
    uint32_t body_len;
    uint32_t req_id;           // ExecutionContext 식별자 (어떤 요청인지)
    uint32_t step_id;          // EgressStep 식별자 (요청 내 어떤 Step인지)
    union {
        struct {
            uint8_t  ip[16]; // IPv6 대응
            uint16_t port;
        } external;          // Case 1: 외부 노드
        uint32_t pod_id;     // Case 2/3: Host Pod 식별자
    } dst;
    uint32_t flags;            // 케이스 구분 (Case 1/2/3)
    uint32_t valid;
};

```

* **Case 1, 3**: `header_buf_addr` 사용
* **Case 2**: `header_buf_addr = NULL`, body buffer에 패킷 통째로 적재
* App 수신 함수에서 `header_buf_addr == NULL`이면 body만 return

### req_id / step_id 매핑

`req_id`와 `step_id`는 정수로 descriptor에 담기며, Host lib 내부에서 매핑 테이블로 관리합니다.

```python
# Host lib 내부 (개념적 표현)
step_registry[(req_id, step_id)] -> step_name (str)

# 예시
step_registry = {
    (1, 0): "fetch_user",
    (1, 1): "fetch_order",
    (2, 0): "fetch_config",
}

```

* **req_id**: `ExecutionContext` 단위 식별자. RX completion 시 어떤 ctx를 깨울지 결정.
* **step_id**: ctx 내 `EgressStep` 단위 식별자. `ctx.on_result(step_name, result)` 호출에 사용.
* TX 시 Host lib이 `(req_id, step_id)` 쌍을 `step_registry`에 등록하고 descriptor에 삽입.
* RX completion callback에서 `(req_id, step_id)`로 `step_registry`를 조회하여 graph 실행을 이어나감.
* `step_id`는 `req_id` 범위 내에서만 유일하면 되므로 ctx마다 0부터 순차 할당해도 충분함.

### Buffer Pool 및 SQ 전체 목록

다음은 Host A와 Host B가 DPU를 통해 통신할 때의 물리적 메모리 배치도입니다.

```text
+-----------------------------+       +-----------------------------+
| Host A Node (CPU DRAM)      |       | Host B Node (CPU DRAM)      |
|                             |       |                             |
| [TX Header/Body Pool A]     |       | [TX Header/Body Pool B]     |
| [RX Header/Body Pool A]     |       | [RX Header/Body Pool B]     |
| [Host RX SQ A]              |       | [Host RX SQ B]              |
+--------------+--------------+       +--------------+--------------+
               |                                     |
               | PCIe                                | PCIe
               |                                     |
+--------------v-------------------------------------v--------------+
| BlueField-3 DPU (DPU DRAM)                                        |
|                                                                   |
| [Host TX SQ A]                [Host TX SQ B]                      |
|                                                                   |
|            --- DPU Internal ---                                   |
| [DPU TX/RX Buffer Pools (Shared)]                                 |
| [DPU Sidecar TX/RX SQs (Shared)]                                  |
+-------------------------------------------------------------------+

```

**상세 위치 및 용도**

| 이름 | 위치 | 사용 케이스 |
| --- | --- | --- |
| **Host 측 (Pod마다)** |  |  |
| TX header/body buffer pool | CPU DRAM (pinned) | Case 1, 3 |
| RX header/body buffer pool | CPU DRAM (pinned) | Case 2, 3 |
| Host TX SQ | **DPU DRAM** (DPA polling) | Host→DPA 방향 |
| Host RX SQ | CPU DRAM (DPA가 PCIe write) | DPA→Host 방향 |
| **DPU 측 (공유)** |  |  |
| DPU TX header/body pool | DPU DRAM | Case 1, 3 |
| DPU RX buffer pool | DPU DRAM | Case 2 |
| DPU Sidecar TX SQ | DPU DRAM (ARM polling) | DPA write / ARM read |
| DPU Sidecar RX SQ | DPU DRAM (DPA polling) | ARM write / DPA read |

**doca_mmap 등록**

* Host data buffers / Host RX SQ → `doca_mmap`, `DOCA_ACCESS_FLAG_PCI_READ_WRITE`
* Host TX SQ / DPU buffers / DPU Sidecar SQs → `doca_mmap`, `DOCA_ACCESS_FLAG_LOCAL_READ_WRITE` (DPU 관점)

---

## 데이터 흐름 (Case별 상세)

### Case 1: Host → 외부 노드

**SQ 흐름**: Host TX SQ → DPU Sidecar TX SQ → (NIC TX)

1. **[Host Worker Pod]**
* App이 TX header buffer + TX body buffer에 데이터 씀 (분리해서)
* TX SQ에 SW descriptor 삽입 및 `valid=1` 설정


2. **[DPA - DMA Manager]**
* TX SQ polling (`desc->valid`) 후 HW descriptor 작성
* DMA job post: Host TX header/body → DPU TX header/body (PCIe DMA)
* DMA 완료 시 DPU Sidecar TX SQ에 SW descriptor 삽입 후 ARM notify
* Host가 `imm data callback`으로 TX buffer 초기화 (CQ 역할)


3. **[ARM - DPU Sidecar]**
* Notify로 깨어나 descriptor 감지
* DPU 버퍼 데이터 읽어 mTLS encryption 및 L7 proxy 처리
* `doca_eth_txq_task_send` submit
* NIC TX completion callback으로 DPU buffer 초기화


4. **[ConnectX-7 NIC]**
* DMA로 DPU Sidecar buffer 읽어 inline encryption 후 Wire 전송



> **참고**: 외부 노드로 전송된 요청에 대한 응답이 돌아올 경우, 해당 흐름은 **Case 2 (Ingress)**의 절차를 따르게 됩니다. 이때 반환된 패킷의 `req_id`와 `step_id`를 기반으로 대기 중이던 `ctx.on_result()`가 호출되며 Execution Graph 실행이 재개됩니다.

### Case 2: 외부 노드 → Host (Ingress 및 Case 1의 응답)

**SQ 흐름**: (NIC RX) → DPU Sidecar RX SQ → Host RX SQ

1. **[ConnectX-7 NIC]**
* Wire에서 packet 수신 및 inline decryption 수행
* RX descriptor ring에 적재 후 DPU RX buffer에 씀


2. **[ARM - DPU Sidecar]**
* `doca_eth_rxq`로 수신 packet 감지
* mTLS 검증 및 L7 proxy 처리
* DPU Sidecar RX SQ에 SW descriptor 삽입 (`header_buf_addr=NULL`, `valid=1`)


3. **[DPA - DMA Manager]**
* DPU Sidecar RX SQ polling
* DMA job post: DPU RX buffer → Host RX body buffer (PCIe DMA)
* DMA 완료 시 Host RX SQ에 SW descriptor 삽입 (이때, `req_id` 및 `step_id` 포함) 및 `imm data`로 Host 깨우기
* DPU ARM이 callback으로 DPU RX buffer 초기화


4. **[Host Worker Pod]**
* `imm data callback`으로 깨어남
* `header_buf_addr == NULL`이므로 body만 그대로 App에 전달
* Host RX SQ에서 읽은 `req_id`와 `step_id`로 `step_registry` 조회 후 `ctx.on_result` 호출
* Host RX body buffer 초기화



### Case 3: 같은 노드 내 Pod A → Pod B (핵심 최적화)

**SQ 흐름**: Pod A TX SQ → DPU Sidecar TX SQ → DPU Sidecar RX SQ → Pod B RX SQ
**특징**: Header만 DPU Sidecar 경유 (라우팅 결정), Body는 Pod A에서 Pod B로 직접 DMA.

1. **[Host Worker Pod A]**
* TX header + body buffer에 데이터 씀
* TX SQ에 SW descriptor 삽입


2. **[DPA - DMA Manager (1차)]**
* TX SQ polling
* DMA job post: Pod A TX header buffer → DPU TX header buffer (header만 먼저)
* 완료 후 DPU Sidecar TX SQ에 SW descriptor 삽입. 이때 **Pod A의 `body_buf_addr`와 `body_len`을 함께 포함하여 기록** (body는 아직 Pod A에 머물러 있음)
* ARM notify


3. **[ARM - DPU Sidecar]**
* Notify로 깨어나 DPU TX header 읽음
* L7 정책 처리 및 routing 결정 (`dst=Pod B` 확정)
* DPU Sidecar RX SQ에 SW descriptor 삽입. DPA 1차에서 넘어온 `body_buf_addr(Pod A)` 정보와 확정된 목적지 `dst=Pod B`를 함께 전달


4. **[DPA - DMA Manager (2차)]**
* DPU Sidecar RX SQ polling
* Descriptor에 명시된 `body_buf_addr(Pod A)` 정보를 바탕으로 DMA job post:
    * Pod A TX body buffer → Pod B RX body buffer (직접 DMA)
    * DPU TX header buffer → Pod B RX header buffer
* 완료 후 Pod B Host RX SQ에 SW descriptor 삽입 및 Pod B 깨우기
* ARM 및 Pod A 콜백으로 해당 버퍼들 초기화


5. **[Host Worker Pod B]**
* Callback으로 깨어나 Host RX SQ 감지
* `req_id` 및 `step_id`로 `step_registry` 조회 후 `ctx.on_result` 호출
* 매핑된 header + body를 App에 전달 후 RX buffer 초기화



---

## 현재 미결 사항 및 코드 상태

**미결 사항 (다음 세션 결정)**

* Buffer pool 슬롯 수 N 결정 (in-flight 요청 최대 개수 기준)
* Buffer pool 슬롯 크기 결정 (최대 패킷 크기 기준)
* DPA가 Host TX SQ와 DPU Sidecar RX SQ를 동시에 polling하는 구조 설계
* Host SQ가 DPU DRAM에 있을 때 Host App의 PCIe write 구현 방법
* polling vs interrupt (NAPI 고려)
* DPU Sidecar SQ descriptor의 dst 필드 표현 방식 결정 (Case 1은 외부 IP/port, Case 2/3는 Host Pod 식별자 등)

**현재 구현 상태**

* Host data buffer → DPU buffer DMA copy (DPA busy polling)
* `doca_comch_msgq` ARM↔DPA 통신 채널
* `doca_dpa_dev_comch_producer_dma_copy`: DMA + Host imm data 전달 동시 처리
* Host consumer callback (CQ 역할)

**미구현 상태**

* DPU Sidecar TX SQ 및 RX SQ
* `doca_eth_txq` 및 `doca_eth_rxq`
* DPA→ARM notify용 별도 producer
* Header/body 분리 buffer 구조
* Host SQ를 DPU DRAM에 배치

---

## Execution Graph (High-Level API)

마이크로서비스의 실행 흐름을 **Execution Graph**로 선언적으로 정의합니다. Flask handler는 graph를 빌드하고 등록만 하며, 실제 실행은 Server event loop이 non-blocking으로 처리합니다.

### 설계 철학

* **지능은 Context에, 실행력은 Server에**: Server는 context가 시키는 대로만 실행
* **Non-blocking**: EgressStep은 HW DMA(큐), InternalStep은 ThreadPool로 실행
* **Worker를 점유하지 않음**: Flask handler는 graph 등록 후 즉시 return

### 핵심 개념

| 요소 | 역할 | 설명 |
| --- | --- | --- |
| **CellController** | 애플리케이션 진입점 | Flask 기반. 요청마다 `ExecutionContext`를 생성하고 작업을 정의 |
| **ExecutionContext (`ctx`)** | 요청 생명주기 관리 | Step을 추가하여 실행할 작업을 예약 |
| **InternalStep** | 내부 함수 실행 | CPU 연산 등. ThreadPool에서 non-blocking 실행 |
| **EgressStep** | 외부 서비스 호출 | HTTP 요청. Descriptor 기반 DMA 전송(non-blocking) |
| **StepResult** | Step 실행 결과 | 이전 Step의 결과를 다음 Step에 전달 |

### `dpumesh.get_context()`

현재 요청에 대한 `ExecutionContext` 객체를 반환합니다. 반드시 Flask 라우트 핸들러 내부에서 호출해야 합니다.

### `ctx.run_internal(func, *, args=(), kwargs={}, inputs=[], id=None)`

내부 함수(Python 함수) 실행을 예약합니다. 별도의 Worker Thread에서 비동기로 실행됩니다.

| 파라미터 | 타입 | 설명 |
| --- | --- | --- |
| `func` | Callable | 실행할 함수 객체 |
| `args` | tuple | 함수에 전달할 위치 인자 |
| `kwargs` | dict | 함수에 전달할 키워드 인자 |
| `inputs` | list[str] | 의존하는 이전 Step의 ID 리스트. 지정된 Step들이 완료되어야 실행 |
| `id` | str (optional) | Step의 고유 ID. 생략 시 자동 생성 |

### `ctx.call(method, url, *, headers={}, body=None, inputs=[], request_builder=None, id=None)`

외부 서비스로의 HTTP 요청(Egress)을 예약합니다. URL은 내부 엔진을 통해 IP/Port 또는 Pod ID로 자동 해석됩니다.

### `with ctx.parallel():`

병렬 실행 블록을 정의하는 Context Manager입니다. 블록 내부에서 호출된 API들은 서로 의존성 없이 **동시에** 실행됩니다.

### 기본 사용 예제

```python
from dpumesh.server import get_context

@app.route('/api/v1', methods=['GET', 'POST'])
def handler():
    ctx = get_context()
    
    if ctx is not None:
        # Step 1: 내부 연산 (ThreadPool)
        ctx.run_internal(process_data, args=(config,), id="process")
        
        # Step 2: 외부 호출 (병렬, DMA 처리)
        with ctx.parallel():
            ctx.call("GET", "http://svc-a/api/v1")
            ctx.call("GET", "http://svc-b/api/v1")
    
    return ""  # 빈 응답 필수 — Server가 graph 실행 후 실제 응답으로 교체

```

---

## 고급 기능 (Advanced Features)

### 데이터 체이닝 (Data Chaining)

이전 Step의 결과를 다음 Step에서 사용하려면 `inputs` 파라미터를 사용합니다. `inputs`에 지정된 Step ID가 **키워드 인자(Keyword Argument)**로 전달되며, 값은 `StepResult` 객체입니다.

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
    step2 = ctx.run_internal(process_user_data, inputs=[step1])
    
    return ""

```

### 동적 요청 생성 (Dynamic Request Builder)

URL이나 Body가 이전 단계의 결과에 따라 결정되어야 할 때 `request_builder`를 사용합니다.

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

### 조건부 실행 및 Fan-Out

* **조건부 실행**: `request_builder`에서 빈 리스트(`[]`)를 반환하면 해당 Step은 Skip됩니다.
* **Fan-Out**: `request_builder`가 여러 개의 튜플 리스트를 반환하면 하나의 `EgressStep`에서 순차적(또는 병렬 블록 내에서 동시)으로 여러 요청을 보낼 수 있습니다.

---

## Low-Level API (Manual Graph Construction)

High-Level API는 내부적으로 Low-Level API(`order`, `pipe`, `EgressStep`, `InternalStep`)를 사용합니다. 복잡한 구조가 필요할 때 사용합니다.

```python
from dpumesh import order, pipe, EgressStep, InternalStep

```

### 기본 요소

* `order(steps)`: 안의 step들을 **순서대로** 실행합니다.
* `pipe(steps)`: 안의 step들을 **동시에** 실행합니다.
* `EgressStep`: 외부 서비스 호출을 정의합니다. `requests` 파라미터에 `(method, url, headers, body)` 리스트를 전달합니다.
* `InternalStep`: Python 함수 실행을 정의합니다.
* `StepResult`: `status_code`, `headers`, `body`, `error`, `ok` 속성을 포함하는 실행 결과 객체입니다.

### 중첩 (Nesting) 예제

`order`와 `pipe`는 자유롭게 중첩 가능합니다.

```python
# Fan-out / Fan-in 예제
order([
    pipe([
        EgressStep("fetch_user", requests=[("GET", user_url, {}, None)]),
        EgressStep("fetch_order", requests=[("GET", order_url, {}, None)]),
    ]),
    InternalStep("merge", func=merge_fn, inputs=["fetch_user", "fetch_order"]),
])

```

---

## Server Event Loop

```
1. 수신 이벤트 발생 (외부 Ingress 요청 또는 로컬 Egress 응답)
   - NIC 또는 다른 Pod를 거쳐 DMA를 통해 Host RX SQ에 Descriptor가 적재되고 Callback 발생

2. Server event loop:
   a. Callback에 의해 깨어남, RX 버퍼의 데이터를 파싱
   b. 이벤트 분류 및 처리:
      - 새로운 Ingress 요청인 경우: Flask handler 실행 → ctx.graph 등록 → ctx.start() 호출
      - 기존 Egress에 대한 응답이거나 ThreadPool 완료 이벤트인 경우: ctx.on_result() 호출
   c. 상태 전환에 따른 Action 실행:
      - EGRESS Action 반환 시 → Descriptor 생성 후 TX SQ에 삽입 (non-blocking)
      - INTERNAL Action 반환 시 → ThreadPool에 작업 할당 (non-blocking)
   d. 위 과정을 반복하여 Graph 진행

3. Graph 실행 완료 (DONE Action 반환) 시, 최종 결과를 TX SQ를 통해 응답으로 전송

```

### 에러 처리

* **EgressStep 실패** (`status_code >= 400`): 해당 step 에러 처리 후 order/pipe 부모로 전파.
* **InternalStep 예외**: Exception 발생 시 `StepResult(error=...)`로 변환하여 부모로 전파.
* **파이프/오더 내 에러**: `order`는 즉시 중단 및 에러 전파, `pipe`는 나머지 동시 실행은 유지하되 결과는 파이프 에러로 처리.
