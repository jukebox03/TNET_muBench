"""
dpumesh.server - Execution Graph Server

Server는 context(= execution graph + 현재 상태)만 보고 이벤트를 전달하는 역할.
지능은 context에, 실행력은 server에.

Execution Graph 문법:
  order([...])  - 순차 실행
  pipe([...])   - 병렬 실행
  EgressStep    - 외부 서비스 호출 (SHM Queue, non-blocking)
  InternalStep  - 내부 함수 실행 (ThreadPool, non-blocking)

source_worker: 요청을 보낸 Worker ("" = 외부)
dest_worker: 요청을 받을 Worker ("" = 외부)
"""

import sys
import os
import time
import uuid
import importlib
import multiprocessing
import traceback
from concurrent.futures import ThreadPoolExecutor, Future
from io import BytesIO
from typing import Callable, Dict, Any, Optional, List, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum
from . import host_lib as dlib
from .common import IngressResponse, OpType


# ========================================================================================
# 1. EXECUTION GRAPH DATA STRUCTURES
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
    """외부 서비스 호출 Step
    
    정적 모드: requests에 (method, url, headers, body) 직접 지정
    동적 모드: request_builder 함수가 inputs 결과를 받아서 request 생성
    """
    id: str
    requests: Optional[List[Tuple[str, str, Dict, Optional[str]]]] = None
    request_builder: Optional[Callable] = None
    inputs: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if self.requests is None and self.request_builder is None:
            raise ValueError(f"EgressStep '{self.id}': requests 또는 request_builder 중 하나 필요")


@dataclass
class InternalStep:
    """내부 함수 실행 Step
    
    func 시그니처: func(**inputs_data, *args, **kwargs) -> result
    inputs에 명시된 이전 step 결과가 keyword argument로 전달됨
    """
    id: str
    func: Callable
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    inputs: List[str] = field(default_factory=list)


# Step 타입 Union
Step = Union['Order', 'Pipe', EgressStep, InternalStep]


@dataclass
class Order:
    """순차 실행 컨테이너 - steps를 순서대로 실행"""
    steps: List[Step]


@dataclass
class Pipe:
    """병렬 실행 컨테이너 - steps를 동시에 실행"""
    steps: List[Step]


# 편의 함수
def order(steps: List[Step]) -> Order:
    return Order(steps)

def pipe(steps: List[Step]) -> Pipe:
    return Pipe(steps)


# ========================================================================================
# 2. EXECUTION CONTEXT (지능 담당)
# ========================================================================================

class ActionType(Enum):
    EGRESS = "egress"        # SHM Queue로 외부 호출
    INTERNAL = "internal"    # ThreadPool에서 함수 실행
    DONE = "done"            # 최종 응답 준비 완료
    WAIT = "wait"            # 대기 중 (아직 할 일 없음)
    ERROR = "error"          # 에러 발생


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
    """Graph node 실행 상태"""
    PENDING = 0
    RUNNING = 1
    DONE = 2
    ERROR = 3


class ExecutionContext:
    """
    Execution Graph 실행 상태 관리.
    
    Server는 이벤트(egress 응답, threadpool 결과)가 올 때마다
    ctx.on_result()를 호출하고, 리턴되는 Action 리스트를 실행한다.
    """
    
    def __init__(self, req_id: str, graph: Optional[Step] = None):
        self.req_id = req_id
        self.graph = graph
        
        # step_id → StepResult
        self.results: Dict[str, StepResult] = {}
        
        # egress_req_id → step_id
        self._egress_to_step: Dict[str, str] = {}
        
        # step_id → egress_req_id 리스트 (EgressStep이 여러 request를 가질 수 있음)
        self._step_egress_ids: Dict[str, List[str]] = {}
        self._step_egress_current: Dict[str, int] = {}  # 현재 진행 중인 request index
        self._step_egress_requests: Dict[str, List[Tuple]] = {}  # 전체 requests
        
        # internal step tracking
        self._pending_internal: Dict[str, bool] = {}  # step_id → pending
        
        # Node 상태 추적 (node_id → state)
        self._node_states: Dict[str, _NodeState] = {}
        self._node_id_counter = 0
        
        # WSGI 응답 (Flask 실행 결과)
        self.wsgi_status: int = 200
        self.wsgi_headers: Dict[str, str] = {}
        self.wsgi_body: str = ""
        
        # Ingress 정보
        self.source_worker: str = ""
        self.environ: Dict = {}
        self.start_time: float = 0.0
        
        # 완료 여부
        self._done = False
        self._error: Optional[str] = None
        
        # Graph execution state
        self._ec_order_children: Dict[str, List[str]] = {}
        self._ec_order_nodes: Dict[str, Order] = {}
        self._ec_order_current: Dict[str, int] = {}
        self._ec_pipe_children: Dict[str, List[str]] = {}
        self._ec_alias_nodes: Dict[str, str] = {}
    
    def start(self) -> List[Action]:
        """Graph 실행 시작 - 초기 Action 리스트 반환"""
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
        """Egress 응답 도착 시 호출"""
        step_id = self._egress_to_step.get(egress_req_id)
        if step_id is None:
            return []
        
        del self._egress_to_step[egress_req_id]
        
        # EgressStep의 순차 request 처리
        if step_id in self._step_egress_requests:
            requests_list = self._step_egress_requests[step_id]
            current_idx = self._step_egress_current[step_id]
            
            if not result.ok:
                # 에러 시 해당 step 실패
                self.results[step_id] = result
                self._set_node_done(step_id, error=True)
                return self._propagate_completion(step_id)
            
            next_idx = current_idx + 1
            if next_idx < len(requests_list):
                # 다음 request 전송
                self._step_egress_current[step_id] = next_idx
                method, url, headers, body = requests_list[next_idx]
                egress_id = str(uuid.uuid4())
                
                self._egress_to_step[egress_id] = step_id
                
                return [Action(
                    type=ActionType.EGRESS,
                    egress_req_id=egress_id,
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                )]
            else:
                # 모든 request 완료 - 마지막 응답을 결과로 저장
                self.results[step_id] = result
                self._set_node_done(step_id)
                return self._propagate_completion(step_id)
        else:
            # 단일 request
            self.results[step_id] = result
            self._set_node_done(step_id)
            return self._propagate_completion(step_id)
    
    def on_internal_result(self, step_id: str, result: Any) -> List[Action]:
        """Internal 함수 실행 완료 시 호출"""
        self._pending_internal.pop(step_id, None)
        
        # result를 StepResult로 변환
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
            self.results[step_id] = StepResult(
                status_code=500,
                error=str(result),
            )
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
        """노드 실행 - Action 리스트 반환"""
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
        """EgressStep 실행"""
        # step_id = node_id로 통일 (EgressStep.id 사용)
        step_id = step.id
        self._node_states[step_id] = _NodeState.RUNNING
        # node_id와 step_id 매핑 (propagation용)
        if node_id != step_id:
            self._node_states[node_id] = _NodeState.RUNNING
            self._ec_alias_nodes[node_id] = step_id
        
        # 동적 request 빌드
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
        
        # 순차 request: 첫 번째만 보내고 나머지는 응답 올 때마다 보냄
        self._step_egress_requests[step_id] = requests_list
        self._step_egress_current[step_id] = 0
        
        method, url, headers, body = requests_list[0]
        egress_id = str(uuid.uuid4())
        self._egress_to_step[egress_id] = step_id
        
        return [Action(
            type=ActionType.EGRESS,
            egress_req_id=egress_id,
            method=method,
            url=url,
            headers=headers,
            body=body,
        )]
    
    def _execute_internal(self, step: InternalStep, node_id: str) -> List[Action]:
        """InternalStep 실행"""
        step_id = step.id
        self._node_states[step_id] = _NodeState.RUNNING
        if node_id != step_id:
            self._node_states[node_id] = _NodeState.RUNNING
            self._ec_alias_nodes[node_id] = step_id
        
        self._pending_internal[step_id] = True
        
        # inputs 수집
        input_data = {name: self.results[name] for name in step.inputs if name in self.results}
        
        return [Action(
            type=ActionType.INTERNAL,
            step_id=step_id,
            func=step.func,
            args=step.args,
            kwargs={**step.kwargs, **input_data},
        )]
    
    def _execute_order(self, node: Order, node_id: str) -> List[Action]:
        """Order (순차) 실행 - 첫 번째 step만 시작"""
        if not node.steps:
            self._set_node_done(node_id)
            return self._propagate_completion(node_id)
        
        # Order 노드의 자식 정보 저장
        child_ids = []
        for i, child in enumerate(node.steps):
            child_id = self._get_step_id(child, f"{node_id}_s{i}")
            child_ids.append(child_id)
        
        self._ec_order_children[node_id] = child_ids
        self._ec_order_nodes[node_id] = node
        self._ec_order_current[node_id] = 0
        
        # 첫 번째 자식 실행
        return self._execute_node(node.steps[0], child_ids[0])
    
    def _execute_pipe(self, node: Pipe, node_id: str) -> List[Action]:
        """Pipe (병렬) 실행 - 모든 step 동시 시작"""
        if not node.steps:
            self._set_node_done(node_id)
            return self._propagate_completion(node_id)
        
        child_ids = []
        for i, child in enumerate(node.steps):
            child_id = self._get_step_id(child, f"{node_id}_p{i}")
            child_ids.append(child_id)
        
        self._ec_pipe_children[node_id] = child_ids
        
        # 모든 자식 동시 실행
        actions = []
        for child, child_id in zip(node.steps, child_ids):
            actions.extend(self._execute_node(child, child_id))
        
        return actions
    
    def _get_step_id(self, node: Step, fallback: str) -> str:
        """Step의 id 추출 (EgressStep/InternalStep은 id 있음, Order/Pipe는 fallback)"""
        if isinstance(node, (EgressStep, InternalStep)):
            return node.id
        return fallback
    
    def _propagate_completion(self, completed_id: str) -> List[Action]:
        """완료된 노드의 부모에게 전파 → 다음 실행할 Action 결정"""
        # 별칭 해소 (양방향)
        # 정방향: node_id → step_id (예: "root_s0" → "internal_service")
        resolved_id = self._ec_alias_nodes.get(completed_id, completed_id)
        # 역방향: step_id → node_id (예: "internal_service" → "root")
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
                
                # 에러 확인
                state = self._node_states.get(resolved_id, _NodeState.PENDING)
                if state == _NodeState.ERROR:
                    self._set_node_done(order_id, error=True)
                    actions.extend(self._propagate_completion(order_id))
                    return actions
                
                # 다음 step
                next_idx = current + 1
                order_node = self._ec_order_nodes[order_id]
                
                if next_idx < len(order_node.steps):
                    self._ec_order_current[order_id] = next_idx
                    child_id = children[next_idx]
                    actions.extend(self._execute_node(order_node.steps[next_idx], child_id))
                else:
                    # Order 완료
                    self._set_node_done(order_id)
                    actions.extend(self._propagate_completion(order_id))
                
                return actions
        
        # Pipe 부모 확인
        for pipe_id, children in self._ec_pipe_children.items():
            if resolved_id in children or completed_id in children or (reverse_id and reverse_id in children):
                # 모든 자식 완료 확인
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
        
        # root 도달 - 완료
        if completed_id == "root" or resolved_id == "root" or reverse_id == "root":
            self._done = True
            
            # 마지막 step 결과를 응답으로 사용
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
        """가장 마지막에 완료된 step의 결과"""
        if not self.results:
            return None
        # 가장 마지막에 추가된 result 반환
        last_key = list(self.results.keys())[-1]
        return self.results[last_key]
    
    # _order_children, _order_nodes, _order_current, _pipe_children, _alias_nodes
    # are initialized in __init__ as _ec_* attributes


# ========================================================================================
# 3. HELPER: APP LOADER
# ========================================================================================

def load_app_from_path(path):
    try:
        if '.' not in sys.path: sys.path.insert(0, '.')
        if '/app' not in sys.path: sys.path.insert(0, '/app')
        
        mod_path, attr = path.rsplit(':', 1) if ':' in path else (path, 'app')
        
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            if mod_path.startswith('ServiceCell.'):
                mod = importlib.import_module(mod_path.replace('ServiceCell.', ''))
            else: raise
        
        return getattr(mod, attr)
    except Exception as e:
        print(f"[Loader] Error loading {path}: {e}")
        traceback.print_exc()
        sys.exit(1)


# ========================================================================================
# 4. WORKER PROCESS TARGETS
# ========================================================================================

def _run_graph_worker(idx, queue, shared_state, app_path, host, port):
    """Graph Mode Worker - SHM 직접 읽기"""
    try:
        wid = f"graph-{idx}-{os.getpid()}"
        dlib.init_worker(wid, start_poller=False)
        
        app = load_app_from_path(app_path)
        
        server = DPUmeshServer(app, host, port, worker_id=wid)
        set_server(server)
        
        print(f"[Worker-{idx}] Ready (Graph Mode)", flush=True)
        server.run()
        
    except KeyboardInterrupt: pass
    except Exception as e:
        print(f"[Worker-{idx}] Crash: {e}")
        traceback.print_exc()


# ========================================================================================
# 5. SERVER CLASSES (Multi-Process Launchers)
# ========================================================================================

class ProcessGraphServer:
    """Multi-Process Server for NON-BLOCKING (Graph) applications"""
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
        for p in procs: p.join()


# ========================================================================================
# 6. DPUMESH SERVER (Event Loop - context 기반)
# ========================================================================================

class DPUmeshServer:
    """
    Single-Process Event Loop
    
    역할: context에게 이벤트 전달, context가 시키는 대로 실행
    - Egress 요청 → SHM Queue
    - Internal 실행 → ThreadPool
    - 완료 → HTTP 응답 전송
    """
    
    def __init__(self, app, host, port, worker_id, thread_pool_size=8):
        self.app = app
        self.host = host
        self.port = port
        self.worker_id = worker_id
        self._running = False
        
        # worker_sq 직접 참조
        self._worker_sq = dlib.get_worker_sq()
        
        # ThreadPool for InternalStep
        self._thread_pool = ThreadPoolExecutor(max_workers=thread_pool_size)
        
        # ingress_req_id → ExecutionContext
        self.active_contexts: Dict[str, ExecutionContext] = {}
        
        # egress_req_id → ingress_req_id
        self.egress_to_ingress: Dict[str, str] = {}
        
        # Future → (ingress_req_id, step_id)
        self._pending_futures: Dict[Future, Tuple[str, str]] = {}
    
    def run(self):
        self._running = True
        while self._running:
            work_done = False
            
            # 1. SHM에서 읽기 — REQUEST/RESPONSE 구분
            entry = self._worker_sq.get()
            if entry:
                work_done = True
                if entry.op_type == OpType.REQUEST:
                    self._handle_new_request(entry)
                elif entry.op_type == OpType.RESPONSE:
                    self._handle_egress_response(entry)
            
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

    def _handle_new_request(self, req):
        """새 Ingress 요청 → WSGI 실행 → graph 시작"""
        ctx = ExecutionContext(req_id=req.req_id)
        ctx.start_time = time.time()
        ctx.source_worker = getattr(req, 'source_worker', '') or ''
        ctx.environ = self._build_environ(req)
        
        self.active_contexts[ctx.req_id] = ctx
        
        # WSGI 앱 실행 (Flask handler → graph 등록)
        self._process_wsgi_app(ctx)
        
        # Graph 실행 시작
        actions = ctx.start()
        self._execute_actions(ctx.req_id, actions)

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

    def _handle_egress_response(self, resp):
        """Egress 응답 → context에 전달"""
        egress_req_id = resp.req_id
        
        ingress_req_id = self.egress_to_ingress.pop(egress_req_id, None)
        if not ingress_req_id:
            print(f"[Server] Unknown egress response: {egress_req_id[:8]}", flush=True)
            return
        
        ctx = self.active_contexts.get(ingress_req_id)
        if not ctx:
            print(f"[Server] No context for ingress: {ingress_req_id[:8]}", flush=True)
            return
        
        result = StepResult(
            status_code=resp.status_code,
            headers=resp.headers,
            body=resp.body,
        )
        
        actions = ctx.on_egress_result(egress_req_id, result)
        self._execute_actions(ingress_req_id, actions)

    def _handle_internal_result(self, ingress_req_id: str, step_id: str, future: Future):
        """ThreadPool 완료 → context에 전달"""
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
        """Action 리스트 실행 - Server는 시키는 대로만"""
        for action in actions:
            if action.type == ActionType.EGRESS:
                # SHM Queue로 egress 요청
                self.egress_to_ingress[action.egress_req_id] = ingress_req_id
                dlib.submit_egress_request(
                    action.method, action.url, action.headers, action.body,
                    action.egress_req_id
                )
                
            elif action.type == ActionType.INTERNAL:
                # ThreadPool에 함수 제출
                future = self._thread_pool.submit(
                    self._run_internal_func,
                    action.func, action.args, action.kwargs
                )
                self._pending_futures[future] = (ingress_req_id, action.step_id)
                
            elif action.type == ActionType.DONE:
                # 완료 마킹 (finalize에서 처리)
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
        """ThreadPool에서 실행되는 함수 wrapper"""
        try:
            return func(*args, **kwargs)
        except Exception as e:
            return e

    def _finalize_done_contexts(self):
        """완료된 context 응답 전송"""
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
            
            # 응답 전송
            dlib.write_ingress_cq(
                IngressResponse(
                    ctx.req_id,
                    ctx.wsgi_status,
                    ctx.wsgi_headers,
                    ctx.wsgi_body
                ),
                source_worker=self.worker_id,
                dest_worker=ctx.source_worker
            )
            
            elapsed = (time.time() - ctx.start_time) * 1000
            print(f"[Server] Completed {ctx.req_id[:8]} in {elapsed:.1f}ms", flush=True)

    def _build_environ(self, req):
        """WSGI environ 생성"""
        body_bytes = req.body.encode() if req.body else b''
        
        env = {
            'REQUEST_METHOD': req.method,
            'PATH_INFO': req.path,
            'QUERY_STRING': req.query_string,
            'SERVER_NAME': self.host,
            'SERVER_PORT': str(self.port),
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'REMOTE_ADDR': req.remote_addr,
            'wsgi.input': BytesIO(body_bytes),
            'wsgi.errors': sys.stderr,
            'wsgi.url_scheme': 'http',
            'wsgi.multithread': False,
            'wsgi.multiprocess': True,
            'wsgi.run_once': False,
            'CONTENT_LENGTH': str(len(body_bytes)),
            'CONTENT_TYPE': req.headers.get('Content-Type', ''),
        }
        
        for k, v in req.headers.items():
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