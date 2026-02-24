"""
dpumesh.server - Execution Graph Server (HW-Simulated)

Server는 context(= execution graph + 현재 상태)만 보고 이벤트를 전달하는 역할.
지능은 context에, 실행력은 server에.

변경점 (HW 시뮬레이션):
- DPUmeshServer: Host RX SQ에서 descriptor polling → buffer pool에서 데이터 읽기
- Egress TX: host_lib.submit_egress_request() (descriptor + buffer pool)
- Ingress Response: host_lib.write_ingress_response() (descriptor + buffer pool)

변경 없음:
- ExecutionContext, High-Level Builder API, Graph 실행 엔진
"""

import sys
import os
import time
import uuid
import json
import importlib
import multiprocessing
import traceback
from concurrent.futures import ThreadPoolExecutor, Future
from io import BytesIO
from typing import Callable, Dict, Any, Optional, List, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum
from . import host_lib as dlib
from .common import (
    IngressResponse, OpType,
    SwDescriptor, CaseFlag, OpFlag,
    deserialize_header,
)


# ========================================================================================
# 1. EXECUTION GRAPH DATA STRUCTURES (변경 없음)
# ========================================================================================

@dataclass
class StepResult:
    """Step 실행 결과"""
    status_code: int = 200
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 400


@dataclass
class EgressStep:
    """외부 서비스 호출 Step"""
    id: str
    requests: Optional[List[Tuple[str, str, Dict, Optional[str]]]] = None
    request_builder: Optional[Callable] = None
    inputs: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.requests is None and self.request_builder is None:
            raise ValueError(f"EgressStep '{self.id}': requests 또는 request_builder 중 하나 필요")


@dataclass
class InternalStep:
    """내부 함수 실행 Step"""
    id: str
    func: Callable
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    inputs: List[str] = field(default_factory=list)


Step = Union['Order', 'Pipe', EgressStep, InternalStep]


@dataclass
class Order:
    """순차 실행 컨테이너"""
    steps: List[Step]


@dataclass
class Pipe:
    """병렬 실행 컨테이너"""
    steps: List[Step]


def order(steps: List[Step]) -> Order:
    return Order(steps)

def pipe(steps: List[Step]) -> Pipe:
    return Pipe(steps)


# ========================================================================================
# 2. EXECUTION CONTEXT (변경 없음)
# ========================================================================================

class ActionType(Enum):
    EGRESS = "egress"
    INTERNAL = "internal"
    DONE = "done"
    WAIT = "wait"
    ERROR = "error"


@dataclass
class Action:
    """Context가 Server에게 요청하는 행동"""
    type: ActionType

    # EGRESS
    egress_req_id: str = ""
    method: str = ""
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[str] = None

    # INTERNAL
    step_id: str = ""
    func: Optional[Callable] = None
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)

    # DONE / ERROR
    response_body: str = ""
    response_status: int = 200
    response_headers: Dict[str, str] = field(default_factory=dict)


class _NodeState(Enum):
    PENDING = 0
    RUNNING = 1
    DONE = 2
    ERROR = 3


class ExecutionContext:
    """
    Execution Graph 실행 상태 관리. (변경 없음)
    """

    def __init__(self, req_id: str, graph: Optional[Step] = None):
        self.req_id = req_id
        self.graph = graph

        self.results: Dict[str, StepResult] = {}
        self._egress_to_step: Dict[str, str] = {}
        self._step_egress_ids: Dict[str, List[str]] = {}
        self._step_egress_current: Dict[str, int] = {}
        self._step_egress_requests: Dict[str, List[Tuple]] = {}
        self._pending_internal: Dict[str, bool] = {}
        self._node_states: Dict[str, _NodeState] = {}
        self._node_id_counter = 0

        self.wsgi_status: int = 200
        self.wsgi_headers: Dict[str, str] = {}
        self.wsgi_body: str = ""

        self.source_worker: str = ""
        self.environ: Dict = {}
        self.start_time: float = 0.0

        self._done = False
        self._error: Optional[str] = None

        self._ec_order_children: Dict[str, List[str]] = {}
        self._ec_order_nodes: Dict[str, Order] = {}
        self._ec_order_current: Dict[str, int] = {}
        self._ec_pipe_children: Dict[str, List[str]] = {}
        self._ec_alias_nodes: Dict[str, str] = {}

        self._builder_steps: List[Step] = []
        self._builder_counter: int = 0
        self._parallel_ctx: Optional[List[Step]] = None

    # ==================== High-Level Builder API ====================

    def run_internal(self, func: Callable, *,
                     args: tuple = (), kwargs: dict = None,
                     inputs: List[str] = None, id: str = None) -> str:
        step_id = id or f"_internal_{self._builder_counter}"
        self._builder_counter += 1

        step = InternalStep(
            id=step_id,
            func=func,
            args=args,
            kwargs=kwargs or {},
            inputs=inputs or [],
        )

        if self._parallel_ctx is not None:
            self._parallel_ctx.append(step)
        else:
            self._builder_steps.append(step)

        return step_id

    def call(self, method: str, url: str, *,
             headers: Dict[str, str] = None, body: str = None,
             inputs: List[str] = None, request_builder: Callable = None,
             id: str = None) -> str:
        step_id = id or f"_egress_{self._builder_counter}"
        self._builder_counter += 1

        if request_builder:
            step = EgressStep(
                id=step_id,
                request_builder=request_builder,
                inputs=inputs or [],
            )
        else:
            step = EgressStep(
                id=step_id,
                requests=[(method, url, headers or {}, body)],
                inputs=inputs or [],
            )

        if self._parallel_ctx is not None:
            self._parallel_ctx.append(step)
        else:
            self._builder_steps.append(step)

        return step_id

    def parallel(self):
        return _ParallelBlock(self)

    def _build_graph(self):
        if self.graph is not None:
            return
        if not self._builder_steps:
            return
        if len(self._builder_steps) == 1:
            self.graph = self._builder_steps[0]
        else:
            self.graph = Order(self._builder_steps)

    def start(self) -> List[Action]:
        if self.graph is None:
            self._done = True
            return [Action(
                type=ActionType.DONE,
                response_body=self.wsgi_body,
                response_status=self.wsgi_status,
                response_headers=self.wsgi_headers,
            )]
        return self._execute_node(self.graph, "root")

    def on_egress_result(self, egress_req_id: str, result: StepResult) -> List[Action]:
        step_id = self._egress_to_step.get(egress_req_id)
        if step_id is None:
            return []
        del self._egress_to_step[egress_req_id]

        if step_id in self._step_egress_requests:
            requests_list = self._step_egress_requests[step_id]
            current_idx = self._step_egress_current[step_id]

            if not result.ok:
                self.results[step_id] = result
                self._set_node_done(step_id, error=True)
                return self._propagate_completion(step_id)

            next_idx = current_idx + 1
            if next_idx < len(requests_list):
                self._step_egress_current[step_id] = next_idx
                method, url, headers, body = requests_list[next_idx]
                egress_id = str(uuid.uuid4())
                self._egress_to_step[egress_id] = step_id
                return [Action(
                    type=ActionType.EGRESS,
                    egress_req_id=egress_id,
                    method=method, url=url, headers=headers, body=body,
                )]
            else:
                self.results[step_id] = result
                self._set_node_done(step_id)
                return self._propagate_completion(step_id)
        else:
            self.results[step_id] = result
            self._set_node_done(step_id)
            return self._propagate_completion(step_id)

    def on_internal_result(self, step_id: str, result: Any) -> List[Action]:
        self._pending_internal.pop(step_id, None)

        if isinstance(result, StepResult):
            self.results[step_id] = result
        elif isinstance(result, str):
            self.results[step_id] = StepResult(body=result)
        elif isinstance(result, dict):
            self.results[step_id] = StepResult(
                body=result.get('body', str(result)),
                status_code=result.get('status_code', 200),
                headers=result.get('headers', {}),
            )
        elif isinstance(result, Exception):
            self.results[step_id] = StepResult(status_code=500, error=str(result))
            self._set_node_done(step_id, error=True)
            return self._propagate_completion(step_id)
        else:
            self.results[step_id] = StepResult(body=str(result) if result is not None else "")

        self._set_node_done(step_id)
        return self._propagate_completion(step_id)

    @property
    def is_done(self) -> bool:
        return self._done

    # ==================== Internal Execution Engine ====================

    def _gen_node_id(self, prefix: str) -> str:
        self._node_id_counter += 1
        return f"{prefix}_{self._node_id_counter}"

    def _set_node_done(self, node_id: str, error: bool = False):
        self._node_states[node_id] = _NodeState.ERROR if error else _NodeState.DONE

    def _execute_node(self, node: Step, node_id: str) -> List[Action]:
        self._node_states[node_id] = _NodeState.RUNNING

        if isinstance(node, EgressStep):
            return self._execute_egress(node, node_id)
        elif isinstance(node, InternalStep):
            return self._execute_internal(node, node_id)
        elif isinstance(node, Order):
            return self._execute_order(node, node_id)
        elif isinstance(node, Pipe):
            return self._execute_pipe(node, node_id)
        else:
            self._set_node_done(node_id, error=True)
            return []

    def _execute_egress(self, step: EgressStep, node_id: str) -> List[Action]:
        step_id = step.id
        self._node_states[step_id] = _NodeState.RUNNING
        if node_id != step_id:
            self._node_states[node_id] = _NodeState.RUNNING
            self._ec_alias_nodes[node_id] = step_id

        if step.request_builder:
            input_data = {name: self.results[name] for name in step.inputs if name in self.results}
            try:
                requests_list = step.request_builder(**input_data)
            except Exception as e:
                self.results[step_id] = StepResult(status_code=500, error=str(e))
                self._set_node_done(step_id, error=True)
                return self._propagate_completion(step_id)
        else:
            requests_list = step.requests or []

        if not requests_list:
            self.results[step_id] = StepResult(body="")
            self._set_node_done(step_id)
            return self._propagate_completion(step_id)

        self._step_egress_requests[step_id] = requests_list
        self._step_egress_current[step_id] = 0

        method, url, headers, body = requests_list[0]
        egress_id = str(uuid.uuid4())
        self._egress_to_step[egress_id] = step_id

        return [Action(
            type=ActionType.EGRESS,
            egress_req_id=egress_id,
            method=method, url=url, headers=headers, body=body,
        )]

    def _execute_internal(self, step: InternalStep, node_id: str) -> List[Action]:
        step_id = step.id
        self._node_states[step_id] = _NodeState.RUNNING
        if node_id != step_id:
            self._node_states[node_id] = _NodeState.RUNNING
            self._ec_alias_nodes[node_id] = step_id

        self._pending_internal[step_id] = True
        input_data = {name: self.results[name] for name in step.inputs if name in self.results}

        return [Action(
            type=ActionType.INTERNAL,
            step_id=step_id,
            func=step.func,
            args=step.args,
            kwargs={**step.kwargs, **input_data},
        )]

    def _execute_order(self, node: Order, node_id: str) -> List[Action]:
        if not node.steps:
            self._set_node_done(node_id)
            return self._propagate_completion(node_id)

        child_ids = []
        for i, child in enumerate(node.steps):
            child_id = self._get_step_id(child, f"{node_id}_s{i}")
            child_ids.append(child_id)

        self._ec_order_children[node_id] = child_ids
        self._ec_order_nodes[node_id] = node
        self._ec_order_current[node_id] = 0

        return self._execute_node(node.steps[0], child_ids[0])

    def _execute_pipe(self, node: Pipe, node_id: str) -> List[Action]:
        if not node.steps:
            self._set_node_done(node_id)
            return self._propagate_completion(node_id)

        child_ids = []
        for i, child in enumerate(node.steps):
            child_id = self._get_step_id(child, f"{node_id}_p{i}")
            child_ids.append(child_id)

        self._ec_pipe_children[node_id] = child_ids

        actions = []
        for child, child_id in zip(node.steps, child_ids):
            actions.extend(self._execute_node(child, child_id))
        return actions

    def _get_step_id(self, node: Step, fallback: str) -> str:
        if isinstance(node, (EgressStep, InternalStep)):
            return node.id
        return fallback

    def _propagate_completion(self, completed_id: str) -> List[Action]:
        resolved_id = self._ec_alias_nodes.get(completed_id, completed_id)
        reverse_id = None
        for k, v in self._ec_alias_nodes.items():
            if v == completed_id:
                reverse_id = k
                break

        actions = []

        # Order 부모 확인
        for order_id, children in self._ec_order_children.items():
            if resolved_id in children or completed_id in children or (reverse_id and reverse_id in children):
                idx = None
                for i, cid in enumerate(children):
                    if cid == resolved_id or cid == completed_id or (reverse_id and cid == reverse_id):
                        idx = i
                        break

                if idx is None:
                    continue

                current = self._ec_order_current.get(order_id, 0)
                if idx != current:
                    continue

                state = self._node_states.get(resolved_id, _NodeState.PENDING)
                if state == _NodeState.ERROR:
                    self._set_node_done(order_id, error=True)
                    actions.extend(self._propagate_completion(order_id))
                    return actions

                next_idx = current + 1
                order_node = self._ec_order_nodes[order_id]

                if next_idx < len(order_node.steps):
                    self._ec_order_current[order_id] = next_idx
                    child_id = children[next_idx]
                    actions.extend(self._execute_node(order_node.steps[next_idx], child_id))
                else:
                    self._set_node_done(order_id)
                    actions.extend(self._propagate_completion(order_id))

                return actions

        # Pipe 부모 확인
        for pipe_id, children in self._ec_pipe_children.items():
            if resolved_id in children or completed_id in children or (reverse_id and reverse_id in children):
                all_done = all(
                    self._node_states.get(self._ec_alias_nodes.get(cid, cid), _NodeState.PENDING)
                    in (_NodeState.DONE, _NodeState.ERROR)
                    for cid in children
                )
                if all_done:
                    has_error = any(
                        self._node_states.get(self._ec_alias_nodes.get(cid, cid), _NodeState.PENDING)
                        == _NodeState.ERROR
                        for cid in children
                    )
                    self._set_node_done(pipe_id, error=has_error)
                    actions.extend(self._propagate_completion(pipe_id))
                return actions

        # root 도달
        if completed_id == "root" or resolved_id == "root" or reverse_id == "root":
            self._done = True
            last_result = self._get_last_result()

            if last_result and last_result.error:
                return [Action(
                    type=ActionType.DONE,
                    response_body=last_result.body or last_result.error,
                    response_status=500,
                    response_headers=self.wsgi_headers,
                )]

            return [Action(
                type=ActionType.DONE,
                response_body=last_result.body if last_result else self.wsgi_body,
                response_status=last_result.status_code if last_result else self.wsgi_status,
                response_headers=self.wsgi_headers,
            )]

        return actions

    def _get_last_result(self) -> Optional[StepResult]:
        if not self.results:
            return None
        last_key = list(self.results.keys())[-1]
        return self.results[last_key]


class _ParallelBlock:
    """Context manager for parallel execution blocks."""

    def __init__(self, ctx: ExecutionContext):
        self._ctx = ctx

    def __enter__(self):
        if self._ctx._parallel_ctx is not None:
            raise RuntimeError("parallel() 블록은 중첩할 수 없습니다.")
        self._ctx._parallel_ctx = []
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        steps = self._ctx._parallel_ctx
        self._ctx._parallel_ctx = None

        if exc_type is not None:
            return False

        if not steps:
            return False

        if len(steps) == 1:
            self._ctx._builder_steps.append(steps[0])
        else:
            self._ctx._builder_steps.append(Pipe(steps))

        return False


# ========================================================================================
# 3. HELPER: APP LOADER (변경 없음)
# ========================================================================================

def load_app_from_path(path):
    try:
        if '.' not in sys.path:
            sys.path.insert(0, '.')
        if '/app' not in sys.path:
            sys.path.insert(0, '/app')

        mod_path, attr = path.rsplit(':', 1) if ':' in path else (path, 'app')

        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            if mod_path.startswith('ServiceCell.'):
                mod = importlib.import_module(mod_path.replace('ServiceCell.', ''))
            else:
                raise

        return getattr(mod, attr)
    except Exception as e:
        print(f"[Loader] Error loading {path}: {e}")
        traceback.print_exc()
        sys.exit(1)


# ========================================================================================
# 4. WORKER PROCESS TARGET (descriptor 기반)
# ========================================================================================

def _run_graph_worker(idx, queue, shared_state, app_path, host, port):
    """Graph Mode Worker - Host RX SQ 직접 polling"""
    try:
        wid = f"graph-{idx}-{os.getpid()}"
        dlib.init_worker(wid, start_poller=False)

        app = load_app_from_path(app_path)

        server = DPUmeshServer(app, host, port, worker_id=wid)
        set_server(server)

        print(f"[Worker-{idx}] Ready (pod_id={dlib.get_pod_id()})", flush=True)
        server.run()

    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[Worker-{idx}] Crash: {e}")
        traceback.print_exc()


# ========================================================================================
# 5. SERVER CLASSES
# ========================================================================================

class ProcessGraphServer:
    """Multi-Process Server"""
    def __init__(self, app_path, host, port, workers):
        self.app_path = app_path
        self.host = host
        self.port = port
        self.workers = workers

    def run(self):
        manager = multiprocessing.Manager()
        queues = [manager.Queue() for _ in range(self.workers)]

        procs = []
        for i in range(self.workers):
            p = multiprocessing.Process(
                target=_run_graph_worker,
                args=(i, queues[i], None, self.app_path, self.host, self.port)
            )
            p.start()
            procs.append(p)

        print(f"[Server] Started {self.workers} GRAPH workers.")
        for p in procs:
            p.join()


# ========================================================================================
# 6. DPUMESH SERVER (Event Loop - descriptor 기반)
# ========================================================================================

class DPUmeshServer:
    """
    Single-Process Event Loop (HW-Simulated)

    Host RX SQ에서 descriptor polling:
    - REQUEST descriptor → WSGI 실행 → graph 시작
    - RESPONSE descriptor → buffer에서 데이터 읽기 → ctx.on_egress_result()
    """

    def __init__(self, app, host, port, worker_id, thread_pool_size=8):
        self.app = app
        self.host = host
        self.port = port
        self.worker_id = worker_id
        self._running = False

        # Pod 리소스 (RX SQ polling 대상)
        self._pod_res = dlib.get_pod_resources()
        self._step_registry = dlib.get_step_registry()

        # ThreadPool for InternalStep
        self._thread_pool = ThreadPoolExecutor(max_workers=thread_pool_size)

        # ingress_req_id_str → ExecutionContext
        self.active_contexts: Dict[str, ExecutionContext] = {}

        # egress_req_id_str → ingress_req_id_str
        self.egress_to_ingress: Dict[str, str] = {}

        # Future → (ingress_req_id_str, step_id)
        self._pending_futures: Dict[Future, Tuple[str, str]] = {}

    def run(self):
        self._running = True
        while self._running:
            work_done = False

            # 1. Host RX SQ에서 descriptor polling
            desc = self._pod_res.rx_sq.get()
            if desc:
                work_done = True
                if desc.valid != 1:
                    pass
                elif desc.is_request:
                    self._handle_new_request(desc)
                elif desc.is_response:
                    self._handle_egress_response(desc)

            # 2. ThreadPool 완료 확인
            done_futures = [f for f in self._pending_futures if f.done()]
            for future in done_futures:
                work_done = True
                ingress_req_id, step_id = self._pending_futures.pop(future)
                self._handle_internal_result(ingress_req_id, step_id, future)

            # 3. 완료된 context 정리
            self._finalize_done_contexts()

            if not work_done:
                dlib.wait_interrupt()

    def _handle_new_request(self, desc: SwDescriptor):
        """
        새 Ingress 요청 (Host RX SQ에서 descriptor 수신)

        Case 2: header_is_null → body에 패킷 통째로 (header+body 합본)
        Case 3: header + body 분리
        """
        # Buffer에서 데이터 읽기
        if desc.header_is_null:
            # Case 2: body에 combined JSON
            body_data = self._pod_res.rx_body_pool.read(desc.body_buf_slot, desc.body_len)
            self._pod_res.rx_body_pool.free(desc.body_buf_slot)

            try:
                combined = json.loads(body_data.decode())
                header_info = combined.get('header', {})
                body_str = combined.get('body', '')
            except Exception as e:
                print(f"[Server] Case 2 parse error: {e}", flush=True)
                return
        else:
            # Case 3: header와 body 분리
            header_data = self._pod_res.rx_header_pool.read(
                desc.header_buf_slot, desc.header_len)
            self._pod_res.rx_header_pool.free(desc.header_buf_slot)

            body_data = self._pod_res.rx_body_pool.read(desc.body_buf_slot, desc.body_len)
            self._pod_res.rx_body_pool.free(desc.body_buf_slot)

            try:
                header_info = deserialize_header(header_data)
            except Exception as e:
                print(f"[Server] Header parse error: {e}", flush=True)
                return
            body_str = body_data.decode('utf-8', errors='ignore')

        # IngressRequest 구성
        req_id_str = header_info.get('req_id_str', str(uuid.uuid4()))

        ctx = ExecutionContext(req_id=req_id_str)
        ctx.start_time = time.time()
        ctx.source_worker = header_info.get('source_worker', '')
        ctx.environ = self._build_environ(header_info, body_str)

        self.active_contexts[ctx.req_id] = ctx

        # WSGI 앱 실행 (Flask handler → graph 등록)
        self._process_wsgi_app(ctx)

        # Graph 빌드 및 실행
        ctx._build_graph()
        actions = ctx.start()
        self._execute_actions(ctx.req_id, actions)

    def _handle_egress_response(self, desc: SwDescriptor):
        """
        Egress 응답 (Host RX SQ에서 descriptor 수신)

        descriptor의 req_id/step_id → step_registry에서 egress_req_id 조회
        buffer에서 데이터 읽기 → StepResult 생성 → ctx.on_egress_result()
        """
        # Buffer에서 데이터 읽기
        header_info = {}
        if not desc.header_is_null:
            header_data = self._pod_res.rx_header_pool.read(
                desc.header_buf_slot, desc.header_len)
            self._pod_res.rx_header_pool.free(desc.header_buf_slot)
            try:
                header_info = deserialize_header(header_data)
            except Exception:
                pass

        body_str = ""
        if desc.body_buf_slot >= 0 and desc.body_len > 0:
            body_data = self._pod_res.rx_body_pool.read(desc.body_buf_slot, desc.body_len)
            self._pod_res.rx_body_pool.free(desc.body_buf_slot)
            body_str = body_data.decode('utf-8', errors='ignore')

        # egress_req_id 복원
        egress_req_id = header_info.get('req_id_str', '')

        ingress_req_id = self.egress_to_ingress.pop(egress_req_id, None)
        if not ingress_req_id:
            print(f"[Server] Unknown egress response: {egress_req_id[:8] if egress_req_id else '?'}", flush=True)
            return

        ctx = self.active_contexts.get(ingress_req_id)
        if not ctx:
            return

        status_code = header_info.get('status_code', 200)
        resp_headers = header_info.get('headers', {})

        result = StepResult(
            status_code=status_code,
            headers=resp_headers,
            body=body_str,
        )

        actions = ctx.on_egress_result(egress_req_id, result)
        self._execute_actions(ingress_req_id, actions)

    def _handle_internal_result(self, ingress_req_id: str, step_id: str, future: Future):
        ctx = self.active_contexts.get(ingress_req_id)
        if not ctx:
            return

        try:
            result = future.result()
        except Exception as e:
            result = e

        actions = ctx.on_internal_result(step_id, result)
        self._execute_actions(ingress_req_id, actions)

    def _execute_actions(self, ingress_req_id: str, actions: List[Action]):
        """Action 리스트 실행"""
        for action in actions:
            if action.type == ActionType.EGRESS:
                # Egress 요청 → TX buffer + TX SQ
                self.egress_to_ingress[action.egress_req_id] = ingress_req_id
                dlib.submit_egress_request(
                    action.method, action.url, action.headers, action.body,
                    action.egress_req_id
                )

            elif action.type == ActionType.INTERNAL:
                future = self._thread_pool.submit(
                    self._run_internal_func,
                    action.func, action.args, action.kwargs
                )
                self._pending_futures[future] = (ingress_req_id, action.step_id)

            elif action.type == ActionType.DONE:
                ctx = self.active_contexts.get(ingress_req_id)
                if ctx:
                    ctx.wsgi_body = action.response_body
                    ctx.wsgi_status = action.response_status
                    ctx.wsgi_headers = action.response_headers

            elif action.type == ActionType.ERROR:
                ctx = self.active_contexts.get(ingress_req_id)
                if ctx:
                    ctx._done = True
                    ctx.wsgi_status = action.response_status
                    ctx.wsgi_body = action.response_body

    @staticmethod
    def _run_internal_func(func, args, kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            return e

    def _process_wsgi_app(self, ctx: ExecutionContext):
        """WSGI 앱 실행 - Flask handler가 ctx에 graph를 등록"""
        _set_current_context(ctx)

        def start_response(status, headers, exc_info=None):
            ctx.wsgi_status = int(status.split()[0])
            ctx.wsgi_headers = dict(headers)

        try:
            result = self.app(ctx.environ, start_response)
            ctx.wsgi_body = ''.join([
                c.decode('utf-8') if isinstance(c, bytes) else str(c)
                for c in result
            ])
        except Exception as e:
            print(f"[Server] WSGI error: {e}", flush=True)
            traceback.print_exc()
            ctx.wsgi_status = 500
            ctx.wsgi_body = f"Internal Server Error: {e}"
        finally:
            _clear_current_context()

    def _finalize_done_contexts(self):
        """완료된 context 응답 전송 (descriptor 기반)"""
        done_ids = [
            rid for rid, ctx in self.active_contexts.items()
            if ctx.is_done
        ]

        for rid in done_ids:
            ctx = self.active_contexts.pop(rid)

            # 남은 egress 매핑 정리
            stale = [eid for eid, iid in self.egress_to_ingress.items() if iid == rid]
            for eid in stale:
                del self.egress_to_ingress[eid]

            # 응답 전송 (TX buffer + TX SQ)
            dlib.write_ingress_response(
                IngressResponse(
                    ctx.req_id,
                    ctx.wsgi_status,
                    ctx.wsgi_headers,
                    ctx.wsgi_body
                ),
                source_worker=self.worker_id,
                dest_worker=ctx.source_worker,
            )

            elapsed = (time.time() - ctx.start_time) * 1000
            print(f"[Server] Completed {ctx.req_id[:8]} in {elapsed:.1f}ms", flush=True)

    def _build_environ(self, header_info: dict, body_str: str) -> dict:
        """WSGI environ 생성 (header_info dict에서)"""
        body_bytes = body_str.encode() if body_str else b''

        headers = header_info.get('headers', {})
        content_type = headers.get('Content-Type', headers.get('content-type', ''))

        env = {
            'REQUEST_METHOD': header_info.get('method', 'GET'),
            'PATH_INFO': header_info.get('path', '/'),
            'QUERY_STRING': header_info.get('query_string', ''),
            'SERVER_NAME': self.host,
            'SERVER_PORT': str(self.port),
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'REMOTE_ADDR': header_info.get('remote_addr', '127.0.0.1'),
            'wsgi.input': BytesIO(body_bytes),
            'wsgi.errors': sys.stderr,
            'wsgi.url_scheme': 'http',
            'wsgi.multithread': False,
            'wsgi.multiprocess': True,
            'wsgi.run_once': False,
            'CONTENT_LENGTH': str(len(body_bytes)),
            'CONTENT_TYPE': content_type,
        }

        for k, v in headers.items():
            k_u = k.upper().replace('-', '_')
            if k_u not in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                env[f'HTTP_{k_u}'] = v

        return env


# ========================================================================================
# 7. CONTEXT & ENTRY POINT
# ========================================================================================

import threading
_context_local = threading.local()

def _set_current_context(ctx):
    _context_local.current_ctx = ctx

def _clear_current_context():
    _context_local.current_ctx = None

def get_current_context() -> Optional[ExecutionContext]:
    return getattr(_context_local, 'current_ctx', None)

get_context = get_current_context

def set_server(s):
    _context_local.server = s


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m dpumesh.server <app_path> [options]")
        sys.exit(1)

    app_path = sys.argv[1]
    host = os.environ.get('DPUMESH_HOST', '0.0.0.0')
    port = int(os.environ.get('DPUMESH_PORT', '8080'))
    workers = int(os.environ.get('DPUMESH_WORKERS', '50'))

    server = ProcessGraphServer(app_path, host, port, workers)

    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()