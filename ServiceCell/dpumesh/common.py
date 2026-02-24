"""
dpumesh.common - HW-Simulated Data Structures (v4)

메모리 구조:
- Buffer Pool: Pod(서비스)별 공유 (같은 Pod 내 Worker들이 slot 공유)
- SQ: Worker별 개별 (TX SQ, RX SQ)
- DPU: Node당 1세트

SHM 배치 예시 (서비스 s0, s1):
  /dev/shm/dpumesh_s0_tx_header   ← s0 Pod 내 Worker 50개가 공유
  /dev/shm/dpumesh_s0_tx_body
  /dev/shm/dpumesh_s0_rx_header
  /dev/shm/dpumesh_s0_rx_body
  /dev/shm/dpumesh_s1_tx_header   ← s1 Pod 내 Worker 50개가 공유
  ...
  /dev/shm/dpumesh_pod_42_tx_sq   ← Worker별 SQ
  /dev/shm/dpumesh_pod_42_rx_sq
  /dev/shm/dpumesh_dpu_tx_header  ← DPU 공유
  ...

메모리 (서비스 5개 기준):
  Host: 5 services × 4 pools × 64MB = 1,280MB
  DPU:  3 pools × 64MB = 192MB
  SQs:  negligible
  Total: ~1.5GB
"""

import os
import struct
import mmap
import fcntl
import threading
import time
import json
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple
from enum import IntEnum


# ========================================================================================
# Constants
# ========================================================================================

class CaseFlag(IntEnum):
    CASE1_EXTERNAL = 1
    CASE2_INGRESS = 2
    CASE3_LOCAL = 3

class OpFlag(IntEnum):
    REQUEST = 0x00
    RESPONSE = 0x10

class PoolType(IntEnum):
    """Buffer pool 종류 (descriptor에 정수로 인코딩)"""
    NONE = 0
    HOST_TX_HEADER = 1
    HOST_TX_BODY = 2
    HOST_RX_HEADER = 3
    HOST_RX_BODY = 4
    DPU_TX_HEADER = 5
    DPU_TX_BODY = 6
    DPU_RX_BODY = 7

SLOT_SIZE = 1024 * 1024      # 1MB per slot
NUM_SLOTS = 64               # Pod(서비스)별 공유
DESCRIPTOR_SIZE = 64
MAX_DESCRIPTORS = 512

ENV_NODE_NAME = 'NODE_NAME'
ENV_WORKER_ID = 'WORKER_ID'
ENV_NAMESPACE = 'NAMESPACE'
HTTP_PROXY_PORT = 5050


# ========================================================================================
# SwDescriptor - 고정 64바이트
# ========================================================================================

_DESC_FMT = '<iIiIIIiibbBBiiii12x'
_DESC_SIZE = struct.calcsize(_DESC_FMT)
assert _DESC_SIZE == 64


@dataclass
class SwDescriptor:
    """
    고정 64바이트 descriptor.

    src_*_pod_id: DPA가 어느 서비스(Pod)의 pool에서 읽을지 결정할 때 사용.
                  PodRegistry에서 pod_id → service name 매핑으로 pool 특정.
    """
    header_buf_slot: int = -1
    header_len: int = 0
    body_buf_slot: int = -1
    body_len: int = 0
    req_id: int = 0
    step_id: int = 0
    dst_pod_id: int = 0
    src_pod_id: int = 0
    flags: int = 0
    valid: int = 0
    src_body_pool_type: int = 0
    src_header_pool_type: int = 0
    src_body_pod_id: int = 0
    src_header_pod_id: int = 0
    src_body_buf_slot: int = -1
    src_header_buf_slot: int = -1

    def pack(self) -> bytes:
        return struct.pack(
            _DESC_FMT,
            self.header_buf_slot, self.header_len,
            self.body_buf_slot, self.body_len,
            self.req_id, self.step_id,
            self.dst_pod_id, self.src_pod_id,
            self.flags, self.valid,
            self.src_body_pool_type, self.src_header_pool_type,
            self.src_body_pod_id, self.src_header_pod_id,
            self.src_body_buf_slot, self.src_header_buf_slot,
        )

    @classmethod
    def unpack(cls, data: bytes) -> 'SwDescriptor':
        vals = struct.unpack(_DESC_FMT, data[:DESCRIPTOR_SIZE])
        return cls(
            header_buf_slot=vals[0], header_len=vals[1],
            body_buf_slot=vals[2], body_len=vals[3],
            req_id=vals[4], step_id=vals[5],
            dst_pod_id=vals[6], src_pod_id=vals[7],
            flags=vals[8], valid=vals[9],
            src_body_pool_type=vals[10], src_header_pool_type=vals[11],
            src_body_pod_id=vals[12], src_header_pod_id=vals[13],
            src_body_buf_slot=vals[14], src_header_buf_slot=vals[15],
        )

    @property
    def is_request(self) -> bool:
        return (self.flags & 0xF0) == OpFlag.REQUEST

    @property
    def is_response(self) -> bool:
        return (self.flags & 0xF0) == OpFlag.RESPONSE

    @property
    def case_flag(self) -> int:
        return self.flags & 0x0F

    @property
    def header_is_null(self) -> bool:
        return self.header_buf_slot < 0


# ========================================================================================
# FileLock
# ========================================================================================

class FileLock:
    def __init__(self, lock_file: str):
        self.lock_file = lock_file
        self._fd = None

    def __enter__(self):
        try:
            self._fd = open(self.lock_file, 'a+')
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception as e:
            print(f"[FileLock] Error: {self.lock_file}: {e}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None


# ========================================================================================
# BufferPool
# ========================================================================================

class BufferPool:
    """
    고정 크기 슬롯 기반 Buffer Pool (mmap).

    이름 형식: "{service}_{tx/rx}_{header/body}" (Host) 또는 "dpu_{...}" (DPU)
    같은 서비스(Pod) 내 모든 Worker가 공유.
    """

    def __init__(self, name: str, create: bool = False,
                 num_slots: int = NUM_SLOTS, slot_size: int = SLOT_SIZE):
        self.name = name
        self.num_slots = num_slots
        self.slot_size = slot_size
        self.shm_path = f"/dev/shm/dpumesh_{name}"
        self.lock_path = f"{self.shm_path}.lock"
        self.total_size = num_slots + (num_slots * slot_size)
        self._fd = None
        self._mm = None

        if create:
            self._init_storage()
        if not os.path.exists(self.lock_path):
            try:
                with open(self.lock_path, 'a+') as f:
                    pass
            except Exception:
                pass
        try:
            self._fd = os.open(self.shm_path, os.O_RDWR)
            self._mm = mmap.mmap(self._fd, self.total_size)
        except Exception as e:
            if not create:
                raise RuntimeError(f"Failed to connect to BufferPool {name}: {e}")
            else:
                print(f"[BufferPool] Error opening {name}: {e}")

    def _init_storage(self):
        try:
            fd = os.open(self.shm_path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                os.ftruncate(fd, self.total_size)
                mm = mmap.mmap(fd, self.total_size)
                mm.seek(0)
                mm.write(b'\x00' * self.num_slots)
                mm.flush()
                mm.close()
            finally:
                os.close(fd)
            if not os.path.exists(self.lock_path):
                with open(self.lock_path, 'a+') as f:
                    pass
        except Exception as e:
            print(f"[BufferPool] Init error {self.name}: {e}")

    def alloc(self) -> int:
        with FileLock(self.lock_path):
            self._mm.seek(0)
            bitmap = self._mm.read(self.num_slots)
            for i in range(self.num_slots):
                if bitmap[i] == 0:
                    self._mm.seek(i)
                    self._mm.write(b'\x01')
                    self._mm.flush()
                    return i
        return -1

    def free(self, slot: int):
        if slot < 0 or slot >= self.num_slots:
            return
        with FileLock(self.lock_path):
            self._mm.seek(slot)
            self._mm.write(b'\x00')
            self._mm.flush()

    def write(self, slot: int, data: bytes) -> int:
        if slot < 0 or slot >= self.num_slots:
            raise ValueError(f"Invalid slot {slot} for pool {self.name}")
        if len(data) > self.slot_size:
            raise OverflowError(
                f"Data size {len(data)} exceeds slot size {self.slot_size} "
                f"in pool {self.name}"
            )
        offset = self.num_slots + (slot * self.slot_size)
        self._mm.seek(offset)
        self._mm.write(data)
        self._mm.flush()
        return len(data)

    def read(self, slot: int, length: int) -> bytes:
        if slot < 0 or slot >= self.num_slots:
            return b''
        read_len = min(length, self.slot_size)
        offset = self.num_slots + (slot * self.slot_size)
        self._mm.seek(offset)
        return self._mm.read(read_len)

    def destroy(self):
        try:
            if self._mm:
                self._mm.close()
            if self._fd is not None:
                os.close(self._fd)
            if os.path.exists(self.shm_path):
                os.remove(self.shm_path)
            if os.path.exists(self.lock_path):
                os.remove(self.lock_path)
        except Exception:
            pass


# ========================================================================================
# DescriptorRing
# ========================================================================================

class DescriptorRing:
    HEADER_SIZE = 12

    def __init__(self, name: str, create: bool = False,
                 max_descriptors: int = MAX_DESCRIPTORS):
        self.name = name
        self.max_descriptors = max_descriptors
        self.shm_path = f"/dev/shm/dpumesh_{name}"
        self.lock_path = f"{self.shm_path}.lock"
        self.total_size = self.HEADER_SIZE + (max_descriptors * DESCRIPTOR_SIZE)
        self._fd = None
        self._mm = None

        if create:
            self._init_storage()
        if not os.path.exists(self.lock_path):
            try:
                with open(self.lock_path, 'a+') as f:
                    pass
            except Exception:
                pass
        try:
            self._fd = os.open(self.shm_path, os.O_RDWR)
            self._mm = mmap.mmap(self._fd, self.total_size)
        except Exception as e:
            if not create:
                raise RuntimeError(f"Failed to connect to DescriptorRing {name}: {e}")
            else:
                print(f"[DescriptorRing] Error opening {name}: {e}")

    def _init_storage(self):
        try:
            fd = os.open(self.shm_path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                os.ftruncate(fd, self.total_size)
                mm = mmap.mmap(fd, self.total_size)
                mm.seek(0)
                mm.write(struct.pack('<III', 0, 0, 0))
                mm.flush()
                mm.close()
            finally:
                os.close(fd)
            if not os.path.exists(self.lock_path):
                with open(self.lock_path, 'a+') as f:
                    pass
        except Exception as e:
            print(f"[DescriptorRing] Init error {self.name}: {e}")

    def _get_header(self):
        self._mm.seek(0)
        return struct.unpack('<III', self._mm.read(self.HEADER_SIZE))

    def _set_header(self, head, tail, count):
        self._mm.seek(0)
        self._mm.write(struct.pack('<III', head, tail, count))
        self._mm.flush()

    def put(self, desc: SwDescriptor) -> bool:
        packed = desc.pack()
        with FileLock(self.lock_path):
            head, tail, count = self._get_header()
            if count >= self.max_descriptors:
                return False
            offset = self.HEADER_SIZE + (tail * DESCRIPTOR_SIZE)
            self._mm.seek(offset)
            self._mm.write(packed)
            self._set_header(head, (tail + 1) % self.max_descriptors, count + 1)
            return True

    def get(self) -> Optional[SwDescriptor]:
        with FileLock(self.lock_path):
            head, tail, count = self._get_header()
            if count == 0:
                return None
            offset = self.HEADER_SIZE + (head * DESCRIPTOR_SIZE)
            self._mm.seek(offset)
            data = self._mm.read(DESCRIPTOR_SIZE)
            self._set_header((head + 1) % self.max_descriptors, tail, count - 1)
            try:
                return SwDescriptor.unpack(data)
            except Exception as e:
                print(f"[DescriptorRing] Unpack error {self.name}: {e}", flush=True)
                return None

    def size(self):
        try:
            with FileLock(self.lock_path):
                _, _, count = self._get_header()
                return count
        except Exception:
            return 0

    def is_empty(self):
        return self.size() == 0

    def clear(self):
        with FileLock(self.lock_path):
            self._set_header(0, 0, 0)

    def destroy(self):
        try:
            if self._mm:
                self._mm.close()
            if self._fd is not None:
                os.close(self._fd)
            if os.path.exists(self.shm_path):
                os.remove(self.shm_path)
            if os.path.exists(self.lock_path):
                os.remove(self.lock_path)
        except Exception:
            pass


# ========================================================================================
# ServicePools - Pod(서비스)별 공유 Buffer Pool
# ========================================================================================

class ServicePools:
    """
    서비스(Pod)별 Buffer Pool 4개.
    같은 서비스의 Worker들이 slot을 공유.

    이름: {service}_tx_header, {service}_tx_body, {service}_rx_header, {service}_rx_body
    """

    _instances: Dict[str, 'ServicePools'] = {}
    _lock = threading.Lock()

    def __init__(self, service: str, create: bool = False):
        self.service = service
        self.tx_header = BufferPool(f"{service}_tx_header", create=create)
        self.tx_body = BufferPool(f"{service}_tx_body", create=create)
        self.rx_header = BufferPool(f"{service}_rx_header", create=create)
        self.rx_body = BufferPool(f"{service}_rx_body", create=create)

    @classmethod
    def get_or_create(cls, service: str, create: bool = False) -> 'ServicePools':
        if service not in cls._instances:
            with cls._lock:
                if service not in cls._instances:
                    cls._instances[service] = cls(service, create=create)
        return cls._instances[service]

    @classmethod
    def reset(cls):
        cls._instances.clear()

    def destroy(self):
        for attr in ('tx_header', 'tx_body', 'rx_header', 'rx_body'):
            getattr(self, attr).destroy()
        ServicePools._instances.pop(self.service, None)


class DpuPools:
    """DPU 측 Buffer Pool (Node당 1세트)"""
    _instance: Optional['DpuPools'] = None
    _lock = threading.Lock()

    def __init__(self, create: bool = False):
        self.tx_header = BufferPool("dpu_tx_header", create=create)
        self.tx_body = BufferPool("dpu_tx_body", create=create)
        self.rx_body = BufferPool("dpu_rx_body", create=create)

    @classmethod
    def get_or_create(cls, create: bool = False) -> 'DpuPools':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(create=create)
        return cls._instance

    @classmethod
    def reset(cls):
        cls._instance = None

    def destroy(self):
        for attr in ('tx_header', 'tx_body', 'rx_body'):
            getattr(self, attr).destroy()
        DpuPools._instance = None


# ========================================================================================
# WorkerSQs - Worker별 SQ
# ========================================================================================

class WorkerSQs:
    def __init__(self, pod_id: int, create: bool = False):
        self.pod_id = pod_id
        self.tx_sq = DescriptorRing(f"pod_{pod_id}_tx_sq", create=create)
        self.rx_sq = DescriptorRing(f"pod_{pod_id}_rx_sq", create=create)

    def destroy(self):
        self.tx_sq.destroy()
        self.rx_sq.destroy()


class DpuSQs:
    def __init__(self, create: bool = False):
        self.sidecar_tx_sq = DescriptorRing("dpu_sidecar_tx_sq", create=create)
        self.sidecar_rx_sq = DescriptorRing("dpu_sidecar_rx_sq", create=create)

    def destroy(self):
        self.sidecar_tx_sq.destroy()
        self.sidecar_rx_sq.destroy()


# ========================================================================================
# PodRegistry - pod_id ↔ (worker_name, service_name) 매핑
# ========================================================================================

class PodRegistry:
    """
    pod_id → (worker_name, service_name) 매핑.
    DPA/ARM이 pod_id로 service name을 알아내어 ServicePools를 특정.
    """
    REGISTRY_PATH = "/dev/shm/dpumesh_pod_registry"
    LOCK_PATH = "/dev/shm/dpumesh_pod_registry.lock"

    @classmethod
    def _ensure_files(cls):
        if not os.path.exists(cls.LOCK_PATH):
            try:
                with open(cls.LOCK_PATH, 'a+') as f:
                    pass
            except Exception:
                pass
        if not os.path.exists(cls.REGISTRY_PATH):
            try:
                with open(cls.REGISTRY_PATH, 'w') as f:
                    json.dump({}, f)
            except Exception:
                pass

    @classmethod
    def register(cls, worker_name: str, pod_id: int, service: str = ""):
        """워커 등록. service는 환경변수 APP에서 가져옴."""
        cls._ensure_files()
        with FileLock(cls.LOCK_PATH):
            try:
                with open(cls.REGISTRY_PATH, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data[worker_name] = {'pod_id': pod_id, 'service': service}
            with open(cls.REGISTRY_PATH, 'w') as f:
                json.dump(data, f)

    @classmethod
    def get_pod_id(cls, worker_name: str) -> int:
        cls._ensure_files()
        with FileLock(cls.LOCK_PATH):
            try:
                with open(cls.REGISTRY_PATH, 'r') as f:
                    data = json.load(f)
                entry = data.get(worker_name)
                if isinstance(entry, dict):
                    return entry.get('pod_id', -1)
                elif isinstance(entry, int):
                    return entry  # legacy compat
                return -1
            except Exception:
                return -1

    @classmethod
    def get_service(cls, pod_id: int) -> str:
        """pod_id → service name"""
        cls._ensure_files()
        with FileLock(cls.LOCK_PATH):
            try:
                with open(cls.REGISTRY_PATH, 'r') as f:
                    data = json.load(f)
                for name, entry in data.items():
                    if isinstance(entry, dict) and entry.get('pod_id') == pod_id:
                        return entry.get('service', '')
                    elif isinstance(entry, int) and entry == pod_id:
                        return name.split('-')[0] if '-' in name else name
                return ""
            except Exception:
                return ""

    @classmethod
    def get_worker_name(cls, pod_id: int) -> str:
        cls._ensure_files()
        with FileLock(cls.LOCK_PATH):
            try:
                with open(cls.REGISTRY_PATH, 'r') as f:
                    data = json.load(f)
                for name, entry in data.items():
                    pid = entry.get('pod_id') if isinstance(entry, dict) else entry
                    if pid == pod_id:
                        return name
                return ""
            except Exception:
                return ""

    @classmethod
    def get_all(cls) -> Dict[str, int]:
        """worker_name → pod_id (legacy compat)"""
        cls._ensure_files()
        with FileLock(cls.LOCK_PATH):
            try:
                with open(cls.REGISTRY_PATH, 'r') as f:
                    data = json.load(f)
                result = {}
                for name, entry in data.items():
                    if isinstance(entry, dict):
                        result[name] = entry.get('pod_id', -1)
                    else:
                        result[name] = entry
                return result
            except Exception:
                return {}

    @classmethod
    def get_workers_by_service(cls, service: str) -> list:
        cls._ensure_files()
        with FileLock(cls.LOCK_PATH):
            try:
                with open(cls.REGISTRY_PATH, 'r') as f:
                    data = json.load(f)
                result = []
                for name, entry in data.items():
                    if isinstance(entry, dict):
                        svc = entry.get('service', '')
                        pid = entry.get('pod_id', -1)
                    else:
                        svc = name.split('-')[0] if '-' in name else name
                        pid = entry
                    if svc == service:
                        result.append((name, pid))
                return sorted(result, key=lambda x: x[1])
            except Exception:
                return []

    @classmethod
    def clear(cls):
        try:
            with open(cls.REGISTRY_PATH, 'w') as f:
                json.dump({}, f)
        except Exception:
            pass

    @classmethod
    def cleanup(cls):
        for p in (cls.REGISTRY_PATH, cls.LOCK_PATH):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# ========================================================================================
# Pool Resolver
# ========================================================================================

def resolve_pool(pool_type: int, pod_id: int = 0) -> Optional[BufferPool]:
    """
    PoolType + pod_id → BufferPool 인스턴스.

    Host pool: pod_id로 서비스 이름 조회 → ServicePools에서 해당 pool 반환
    DPU pool: pod_id 무관
    """
    if pool_type == PoolType.NONE:
        return None

    # DPU pools
    if pool_type == PoolType.DPU_TX_HEADER:
        return DpuPools.get_or_create().tx_header
    elif pool_type == PoolType.DPU_TX_BODY:
        return DpuPools.get_or_create().tx_body
    elif pool_type == PoolType.DPU_RX_BODY:
        return DpuPools.get_or_create().rx_body

    # Host pools: pod_id → service → ServicePools
    service = PodRegistry.get_service(pod_id)
    if not service:
        return None
    pools = ServicePools.get_or_create(service)
    if pool_type == PoolType.HOST_TX_HEADER:
        return pools.tx_header
    elif pool_type == PoolType.HOST_TX_BODY:
        return pools.tx_body
    elif pool_type == PoolType.HOST_RX_HEADER:
        return pools.rx_header
    elif pool_type == PoolType.HOST_RX_BODY:
        return pools.rx_body
    return None


# Legacy compat
def resolve_pool_type(pool_type: int, pod_id: int = 0) -> str:
    names = {
        PoolType.HOST_TX_HEADER: "tx_header", PoolType.HOST_TX_BODY: "tx_body",
        PoolType.HOST_RX_HEADER: "rx_header", PoolType.HOST_RX_BODY: "rx_body",
        PoolType.DPU_TX_HEADER: "dpu_tx_header", PoolType.DPU_TX_BODY: "dpu_tx_body",
        PoolType.DPU_RX_BODY: "dpu_rx_body",
    }
    return names.get(pool_type, "")


# ========================================================================================
# StepRegistry
# ========================================================================================

class StepRegistry:
    def __init__(self):
        self._map: Dict[Tuple[int, int], str] = {}
        self._lock = threading.Lock()
        self._next_req_id = 1
        self._step_counters: Dict[int, int] = {}

    def alloc_req_id(self) -> int:
        with self._lock:
            rid = self._next_req_id
            self._next_req_id += 1
            self._step_counters[rid] = 0
            return rid

    def alloc_step_id(self, req_id: int) -> int:
        with self._lock:
            sid = self._step_counters.get(req_id, 0)
            self._step_counters[req_id] = sid + 1
            return sid

    def register(self, req_id: int, step_id: int, step_name: str):
        with self._lock:
            self._map[(req_id, step_id)] = step_name

    def lookup(self, req_id: int, step_id: int) -> Optional[str]:
        with self._lock:
            return self._map.get((req_id, step_id))

    def remove(self, req_id: int, step_id: int):
        with self._lock:
            self._map.pop((req_id, step_id), None)

    def remove_req(self, req_id: int):
        with self._lock:
            to_remove = [k for k in self._map if k[0] == req_id]
            for k in to_remove:
                del self._map[k]
            self._step_counters.pop(req_id, None)


# ========================================================================================
# Header Serialization
# ========================================================================================

def serialize_header(method="", url="", path="", headers=None,
                     query_string="", remote_addr="", status_code=0,
                     req_id_str="", source_worker="", dest_worker="") -> bytes:
    return json.dumps({
        'method': method, 'url': url, 'path': path,
        'headers': headers or {},
        'query_string': query_string, 'remote_addr': remote_addr,
        'status_code': status_code, 'req_id_str': req_id_str,
        'source_worker': source_worker, 'dest_worker': dest_worker,
    }).encode()


def deserialize_header(data: bytes) -> Dict[str, Any]:
    return json.loads(data.decode())


# ========================================================================================
# Legacy Compatibility
# ========================================================================================

class OpType:
    REQUEST = 1
    RESPONSE = 2

@dataclass
class IngressRequest:
    req_id: str; method: str; path: str; headers: Dict[str, str]
    body: Optional[str]; query_string: str; remote_addr: str
    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)

@dataclass
class IngressResponse:
    req_id: str; status_code: int; headers: Dict[str, str]; body: str
    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)

@dataclass
class EgressRequest:
    worker_id: str; req_id: str; method: str; url: str
    headers: Dict[str, str]; body: Optional[str]

@dataclass
class EgressResponse:
    req_id: str; status_code: int; headers: Dict[str, str]; body: str


# Legacy wrappers
class PodResources:
    """v2 호환: ServicePools(service) + WorkerSQs(pod_id) 조합"""
    def __init__(self, pod_id: int, create: bool = False, service: str = ""):
        self.pod_id = pod_id
        if not service:
            service = PodRegistry.get_service(pod_id)
        if not service:
            service = os.environ.get('APP', 'unknown')
        self.service = service
        pools = ServicePools.get_or_create(service, create=create)
        self.tx_header_pool = pools.tx_header
        self.tx_body_pool = pools.tx_body
        self.rx_header_pool = pools.rx_header
        self.rx_body_pool = pools.rx_body
        self._sqs = WorkerSQs(pod_id, create=create)
        self.tx_sq = self._sqs.tx_sq
        self.rx_sq = self._sqs.rx_sq

    def destroy(self):
        self._sqs.destroy()


class DpuResources:
    """v2 호환: DpuPools + DpuSQs"""
    def __init__(self, create: bool = False):
        pools = DpuPools.get_or_create(create=create)
        self.tx_header_pool = pools.tx_header
        self.tx_body_pool = pools.tx_body
        self.rx_body_pool = pools.rx_body
        self._sqs = DpuSQs(create=create)
        self.sidecar_tx_sq = self._sqs.sidecar_tx_sq
        self.sidecar_rx_sq = self._sqs.sidecar_rx_sq

    def destroy(self):
        self._sqs.destroy()