"""
dpumesh.common - Shared Data Structures & Shared Memory Queue

Queue 기반 통신:
- Worker ↔ DMA Manager: Worker Queue (SQ/CQ)
- DMA Manager ↔ Sidecar: Sidecar Queue (SQ/CQ)

지금: /dev/shm 공유 메모리
나중: PCIe DMA / RDMA
"""

import os
import json
import mmap
import struct
import threading
import time
import fcntl
import pickle
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from enum import Enum


# ========================================================================================
# Data Structures
# ========================================================================================

class OpType(Enum):
    REQUEST = 1
    RESPONSE = 2


# ========================================================================================
# Legacy Data Structures (기존 코드 호환용)
# ========================================================================================

@dataclass
class IngressRequest:
    """외부에서 들어오는 요청"""
    req_id: str
    method: str
    path: str
    headers: Dict[str, str]
    body: Optional[str]
    query_string: str
    remote_addr: str
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclass
class IngressResponse:
    """외부로 나가는 응답"""
    req_id: str
    status_code: int
    headers: Dict[str, str]
    body: str
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclass
class EgressRequest:
    """Worker -> 외부 서비스 요청"""
    worker_id: str
    req_id: str
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[str]
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclass
class EgressResponse:
    """외부 서비스 -> Worker 응답"""
    req_id: str
    status_code: int
    headers: Dict[str, str]
    body: str
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# ========================================================================================
# Queue Entry
# ========================================================================================

@dataclass
class QueueEntry:
    """Queue에 들어가는 엔트리"""
    req_id: str
    op_type: OpType
    method: str
    url: str                    # 목적지 URL (예: http://s1.mubench-dpu.svc.cluster.local/api/v1)
    path: str
    headers: Dict[str, str]
    body: Optional[str]
    query_string: str = ""
    remote_addr: str = ""
    status_code: int = 200
    source_worker: str = ""     # 요청을 보낸 Worker ("" = 외부 클라이언트)
    dest_worker: str = ""       # 요청을 받을 Worker ("" = 외부 클라이언트)
    
    def to_dict(self):
        d = asdict(self)
        d['op_type'] = self.op_type.value
        return d
    
    @classmethod
    def from_dict(cls, d):
        d['op_type'] = OpType(d['op_type'])
        return cls(**d)
    
    def serialize(self) -> bytes:
        return pickle.dumps(self.to_dict())
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'QueueEntry':
        return cls.from_dict(pickle.loads(data))


# ========================================================================================
# Shared Memory Queue (mmap + Ring Buffer)
# ========================================================================================

class FileLock:
    """프로세스 간 동기화를 위한 파일 락"""
    def __init__(self, lock_file: str):
        self.lock_file = lock_file
        self._fd = None
        
    def __enter__(self):
        try:
            # 'a+' 모드로 열어서 기존 내용 보존하고 파일이 없으면 생성
            self._fd = open(self.lock_file, 'a+')
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception as e:
            print(f"[FileLock] Error acquiring lock {self.lock_file}: {e}")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None


class SharedMemoryQueue:
    """
    mmap 기반의 고정 크기 Ring Buffer Queue
    
    Header (12 bytes):
      - head (4 bytes): 읽기 위치 인덱스
      - tail (4 bytes): 쓰기 위치 인덱스
      - count (4 bytes): 항목 개수
    Data Area:
      - [Length(4) + Data(Length)] 반복 구조
    """
    
    HEADER_SIZE = 12
    SHM_SIZE = 10 * 1024 * 1024  # 10MB 고정 크기
    
    def __init__(self, name: str, create: bool = False, max_size: int = 1000):
        self.name = name
        self.max_size = max_size
        self.shm_name = f"dpumesh_{name}"
        self.shm_path = f"/dev/shm/{self.shm_name}"
        self.lock_path = f"{self.shm_path}.lock"
        
        if create:
            self._init_storage()
        
        # 락 파일 확보 (항상 존재해야 함)
        if not os.path.exists(self.lock_path):
            try:
                with open(self.lock_path, 'a+') as f: pass
            except: pass
            
        try:
            self._fd = os.open(self.shm_path, os.O_RDWR)
            self._mm = mmap.mmap(self._fd, self.SHM_SIZE)
        except Exception as e:
            if not create:
                # 연결 모드인데 실패하면 예외 발생 (재시도용)
                raise RuntimeError(f"Failed to connect to SHM {self.shm_name}: {e}")
            else:
                print(f"[SHM] Error opening mmap {self.shm_name}: {e}")
        
    def _init_storage(self):
        """저장소 파일 초기화 (항상 헤더 리셋 - 이전 배포 잔여 데이터 방지)"""
        try:
            # 1. 파일 생성 또는 기존 파일 열기 + 크기 확보
            fd = os.open(self.shm_path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                os.ftruncate(fd, self.SHM_SIZE)
                
                # 2. 헤더 항상 초기화 (head=HEADER_SIZE, tail=HEADER_SIZE, count=0)
                mm = mmap.mmap(fd, self.SHM_SIZE)
                mm.seek(0)
                mm.write(struct.pack('III', self.HEADER_SIZE, self.HEADER_SIZE, 0))
                mm.flush()
                mm.close()
            finally:
                os.close(fd)
            
            if not os.path.exists(self.lock_path):
                with open(self.lock_path, 'a+') as f:
                    pass
                
        except Exception as e:
            print(f"[SHM] Init error: {e}")

    def _get_header(self):
        """Header 읽기: (head, tail, count)"""
        self._mm.seek(0)
        return struct.unpack('III', self._mm.read(self.HEADER_SIZE))

    def _set_header(self, head, tail, count):
        """Header 쓰기"""
        self._mm.seek(0)
        self._mm.write(struct.pack('III', head, tail, count))
        self._mm.flush()

    def put(self, entry: QueueEntry) -> bool:
        """항목 추가 (Thread/Process Safe)"""
        data = entry.serialize()
        data_len = len(data)
        
        with FileLock(self.lock_path):
            head, tail, count = self._get_header()
            
            # 초기화가 안되어있으면 보정 (매우 중요)
            if head == 0 or tail == 0:
                head = self.HEADER_SIZE
                tail = self.HEADER_SIZE
                count = 0
            
            needed = 4 + data_len
            
            # Ring Buffer Wrap-around (단순화: 공간 부족시 처음부터)
            if tail + needed > self.SHM_SIZE:
                tail = self.HEADER_SIZE
                # 주의: head가 앞쪽에 있으면 덮어쓰게 됨 (Ring logic 필요하지만 일단 단순화)
            
            try:
                self._mm.seek(tail)
                self._mm.write(struct.pack('I', data_len))
                self._mm.write(data)
                
                new_tail = self._mm.tell()
                self._set_header(head, new_tail, count + 1)
                
                # Debug log only for specific events to avoid noise
                if "worker" in self.name and "sq" in self.name:
                    pass # print(f"[SHM-PUT] {self.name} count={count+1} head={head} tail={new_tail}", flush=True)
                    
                return True
            except Exception as e:
                print(f"[SHM] Put error: {e}")
                return False

    def get(self) -> Optional[QueueEntry]:
        """항목 꺼내기 (Thread/Process Safe)"""
        with FileLock(self.lock_path):
            head, tail, count = self._get_header()
            
            if count == 0:
                return None
            
            # 유효성 검사
            if head < self.HEADER_SIZE or head >= self.SHM_SIZE:
                head = self.HEADER_SIZE
                
            self._mm.seek(head)
            try:
                len_data_bytes = self._mm.read(4)
                if len(len_data_bytes) < 4:
                    # 파일 끝?
                    self._set_header(self.HEADER_SIZE, self.HEADER_SIZE, 0)
                    return None
                    
                len_data = struct.unpack('I', len_data_bytes)[0]
                
                if len_data == 0 or len_data > 1024*1024: # Sanity check (max 1MB)
                     # Corrupted? Reset.
                    print(f"[SHM-GET] Corrupted data len={len_data}. Resetting.", flush=True)
                    self._set_header(self.HEADER_SIZE, self.HEADER_SIZE, 0)
                    return None

                data = self._mm.read(len_data)
                new_head = self._mm.tell()
                
                new_count = count - 1
                if new_count <= 0:
                    # 다 비웠으면 포인터 초기화 (Fragmentation 방지)
                    new_head = self.HEADER_SIZE
                    new_tail = self.HEADER_SIZE # 주의: put 도중에 바꾸면 안됨. Lock이 있으니 안전.
                    tail = new_tail 
                    new_count = 0
                
                self._set_header(new_head, tail, new_count)
                
                return QueueEntry.deserialize(data)
            except Exception as e:
                print(f"[SHM] Get error: {e}")
                self._set_header(self.HEADER_SIZE, self.HEADER_SIZE, 0)
                return None

    def size(self) -> int:
        try:
            with FileLock(self.lock_path):
                _, _, count = self._get_header()
                return count
        except:
            return 0

    def is_empty(self) -> bool:
        return self.size() == 0

    def clear(self):
        with FileLock(self.lock_path):
            self._set_header(self.HEADER_SIZE, self.HEADER_SIZE, 0)

    def destroy(self):
        try:
            self._mm.close()
            os.close(self._fd)
            if os.path.exists(self.shm_path): os.remove(self.shm_path)
            if os.path.exists(self.lock_path): os.remove(self.lock_path)
        except:
            pass


# ========================================================================================
# Queue Manager - 모든 Queue 관리
# ========================================================================================

class QueueManager:
    """
    노드 내 모든 Queue 관리
    
    규칙: SQ는 DMA Manager가 write, CQ는 DMA Manager가 read
    
    구조:
    - worker_sq: DMA Manager → Worker (Worker가 읽음)
    - worker_cq: Worker → DMA Manager (DMA Manager가 읽음)
    - sidecar_sq: DMA Manager → Sidecar (Sidecar가 읽음)
    - sidecar_cq: Sidecar → DMA Manager (DMA Manager가 읽음)
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self):
        self._worker_sqs: Dict[str, SharedMemoryQueue] = {}  # DMA → Worker
        self._worker_cqs: Dict[str, SharedMemoryQueue] = {}  # Worker → DMA
        self._sidecar_sq: Optional[SharedMemoryQueue] = None  # DMA → Sidecar
        self._sidecar_cq: Optional[SharedMemoryQueue] = None  # Sidecar → DMA
    
    @classmethod
    def get_instance(cls) -> 'QueueManager':
        """싱글톤 인스턴스"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def create_worker_queues(self, worker_id: str) -> tuple:
        """Worker용 SQ/CQ 생성"""
        sq_name = f"worker_{worker_id}_sq"
        cq_name = f"worker_{worker_id}_cq"
        
        sq = SharedMemoryQueue(sq_name, create=True)
        cq = SharedMemoryQueue(cq_name, create=True)
        
        self._worker_sqs[worker_id] = sq
        self._worker_cqs[worker_id] = cq
        
        return sq, cq
    
    def get_worker_queues(self, worker_id: str) -> tuple:
        """Worker용 SQ/CQ 가져오기 (연결)"""
        sq_name = f"worker_{worker_id}_sq"
        cq_name = f"worker_{worker_id}_cq"
        
        if worker_id not in self._worker_sqs:
            self._worker_sqs[worker_id] = SharedMemoryQueue(sq_name, create=False)
        if worker_id not in self._worker_cqs:
            self._worker_cqs[worker_id] = SharedMemoryQueue(cq_name, create=False)
        
        return self._worker_sqs[worker_id], self._worker_cqs[worker_id]
    
    def create_sidecar_queues(self) -> tuple:
        """Sidecar용 SQ/CQ 생성"""
        self._sidecar_sq = SharedMemoryQueue("sidecar_sq", create=True)
        self._sidecar_cq = SharedMemoryQueue("sidecar_cq", create=True)
        return self._sidecar_sq, self._sidecar_cq
    
    def get_sidecar_queues(self) -> tuple:
        """Sidecar용 SQ/CQ 가져오기 (연결)"""
        if self._sidecar_sq is None:
            self._sidecar_sq = SharedMemoryQueue("sidecar_sq", create=False)
        if self._sidecar_cq is None:
            self._sidecar_cq = SharedMemoryQueue("sidecar_cq", create=False)
        return self._sidecar_sq, self._sidecar_cq
    
    def get_all_worker_ids(self) -> list:
        """등록된 모든 Worker ID"""
        return list(self._worker_sqs.keys())


# ========================================================================================
# Configuration
# ========================================================================================

# 공유 메모리 경로
SHM_BASE_PATH = "/dev/shm/dpumesh"

# 환경 변수
ENV_NODE_NAME = 'NODE_NAME'
ENV_WORKER_ID = 'WORKER_ID'
ENV_NAMESPACE = 'NAMESPACE'

# 기본 포트 (HTTP 통신용 - 다른 노드 간)
HTTP_PROXY_PORT = 5005