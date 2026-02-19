# DPUmesh Application Development Guide

이 문서는 DPUmesh 라이브러리를 사용하여 마이크로서비스 애플리케이션을 개발하는 방법을 상세히 설명합니다. DPUmesh는 비동기 실행 그래프(Execution Graph)를 기반으로 하여 서비스 간 통신과 내부 연산을 최적화합니다.

## 1. 핵심 개념 (Core Concepts)

*   **CellController**: Flask 기반의 애플리케이션 진입점입니다. 요청이 들어오면 `ExecutionContext`를 생성하고 작업을 정의합니다.
*   **ExecutionContext (`ctx`)**: 현재 요청의 생명주기를 관리합니다. 여기에 "Step"을 추가하여 실행할 작업을 예약합니다.
*   **Step**: 실행의 기본 단위입니다.
    *   `InternalStep`: CPU 연산 등 내부적으로 처리할 작업.
    *   `EgressStep`: 외부 서비스로의 HTTP 요청.
*   **Dependency (Inputs)**: Step 간의 의존성입니다. 이전 Step의 결과가 다음 Step의 입력으로 사용될 수 있습니다.

## 2. High-Level API Reference

개발자는 주로 Flask 핸들러 내부에서 다음 API들을 사용합니다.

### 2.1. `dpumesh.get_context()`
*   **설명**: 현재 요청에 대한 `ExecutionContext` 객체를 반환합니다.
*   **사용처**: 반드시 Flask 라우트 핸들러 내부에서 호출해야 합니다.

### 2.2. `ctx.run_internal(func, *, args=(), kwargs={}, inputs=[], id=None)`
*   **설명**: 내부 함수(Python 함수) 실행을 예약합니다. 별도의 Worker Thread에서 비동기로 실행됩니다.
*   **파라미터**:
    *   `func` (Callable): 실행할 함수 객체.
    *   `args` (tuple): 함수에 전달할 위치 인자(Positional Arguments).
    *   `kwargs` (dict): 함수에 전달할 키워드 인자(Keyword Arguments).
    *   `inputs` (list[str]): 이 함수가 의존하는 이전 Step의 ID 리스트. 지정된 Step들이 완료되어야 실행됩니다.
    *   `id` (str, optional): Step의 고유 ID. 생략 시 자동 생성됩니다.
*   **반환값**: 생성된 Step ID (문자열). 다른 Step의 `inputs`로 사용됩니다.

### 2.3. `ctx.call(method, url, *, headers={}, body=None, inputs=[], request_builder=None, id=None)`
*   **설명**: 외부 서비스로의 HTTP 요청(Egress)을 예약합니다.
*   **파라미터**:
    *   `method` (str): HTTP 메서드 ("GET", "POST", "PUT", "DELETE" 등).
    *   `url` (str): 대상 서비스 URL (예: `"http://shipping-service/api/v1"`).
    *   `headers` (dict): 요청 헤더.
    *   `body` (str/bytes): 요청 본문.
    *   `inputs` (list[str]): 이 요청이 의존하는 이전 Step의 ID 리스트.
    *   `request_builder` (Callable, optional): 동적 요청 생성을 위한 함수 (고급 기능 참조).
    *   `id` (str, optional): Step의 고유 ID.
*   **반환값**: 생성된 Step ID (문자열).

### 2.4. `with ctx.parallel():`
*   **설명**: 병렬 실행 블록을 정의하는 Context Manager입니다.
*   **동작**: 이 블록(`with`) 내부에서 호출된 `run_internal`이나 `call`은 서로 의존성 없이 **동시에** 실행됩니다.

```python
with ctx.parallel():
    ctx.call("GET", "http://service-a")
    ctx.call("GET", "http://service-b")
# 두 요청이 동시에 전송됨
```

---

## 3. 고급 기능 (Advanced Features)

복잡한 마이크로서비스 로직을 구현하기 위한 심화 기능들입니다.

### 3.1. 데이터 체이닝 (Data Chaining)
이전 Step의 결과를 다음 Step에서 사용하려면 `inputs` 파라미터를 사용합니다.

*   `run_internal`에 등록된 함수나 `request_builder` 함수는 `inputs`에 지정된 Step ID를 **키워드 인자(Keyword Argument)**로 전달받습니다.
*   전달되는 값은 `StepResult` 객체입니다.

**StepResult 객체 구조:**
```python
class StepResult:
    body: str          # 응답 본문 또는 함수 리턴값
    status_code: int   # HTTP 상태 코드 (InternalStep의 경우 기본 200)
    headers: dict      # HTTP 헤더
```

**예제:**
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
    # process_user_data(user_step=StepResult(...)) 형태로 호출됨
    step2 = ctx.run_internal(process_user_data, inputs=[step1])
    
    return ""
```

### 3.2. 동적 요청 생성 (Dynamic Request Builder)
URL이나 Body가 이전 단계의 결과에 따라 결정되어야 할 때 `request_builder`를 사용합니다.

*   **함수 시그니처**: `def builder(**inputs) -> list[tuple]`
*   **반환값**: `[(method, url, headers, body)]` 형태의 튜플 리스트.

**예제:**
```python
def build_req(config_step):
    # config_step.body에 따라 대상 URL이 달라짐
    target_host = config_step.body.strip()
    return [("POST", f"http://{target_host}/api", {}, "data")]

@app.route("/dynamic")
def dynamic_handler():
    ctx = get_context()
    
    # 설정 조회
    s1 = ctx.call("GET", "http://config-service/target", id="config_step")
    
    # 조회된 설정으로 동적 호출
    ctx.call("GET", "dummy", # url은 무시됨
             request_builder=build_req, 
             inputs=[s1])
    return ""
```

### 3.3. 조건부 실행 (Conditional Execution)
특정 조건에서만 실행하거나 실행을 건너뛰고 싶을 때, `request_builder`에서 **빈 리스트(`[]`)**를 반환하면 해당 Step은 실행되지 않고 넘어갑니다(Skip).

```python
def check_and_call(auth_step):
    if auth_step.status_code != 200:
        return []  # 인증 실패 시 호출 생략
    return [("GET", "http://secure-service/data", {}, "")]
```

### 3.4. Fan-Out (1:N 요청)
하나의 `EgressStep`에서 순차적으로 여러 요청을 보낼 수도 있습니다. `request_builder`가 여러 개의 튜플을 반환하면 됩니다. (병렬 실행을 원하면 `with ctx.parallel()`을 사용하세요.)

---

## 4. Low-Level API (Manual Graph Construction)

일반적인 경우에는 필요하지 않지만, 그래프 자체를 프로그래밍 방식으로 조립해야 할 때 사용합니다.

```python
from dpumesh import order, pipe, EgressStep, InternalStep
```

*   `order([step1, step2, ...])`: 순차 실행 컨테이너 (`Order`)
*   `pipe([step1, step2, ...])`: 병렬 실행 컨테이너 (`Pipe`)
*   `EgressStep(id, requests=..., request_builder=...)`
*   `InternalStep(id, func, ...)`

---

## 5. 주의 사항 및 팁

1.  **핸들러 반환값**: Flask 핸들러(`def handler():`)는 항상 **빈 문자열 `""`**을 반환해야 합니다. 실제 클라이언트로의 응답은 그래프 실행이 모두 완료된 후 `dpumesh`가 자동으로 전송합니다. 마지막 Step의 결과가 최종 응답이 됩니다.
2.  **ID 중복 방지**: `id`를 수동으로 지정할 경우, 하나의 요청 내에서 중복되지 않도록 주의하세요. 가급적 자동 생성을 권장합니다.
3.  **디버깅**: `run_internal` 함수 내에서 `print()`를 사용하면 로그를 확인할 수 있습니다.