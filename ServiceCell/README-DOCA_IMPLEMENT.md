# DOCA Implementation Guide for DPUmesh

이 문서는 Python 기반 DPUmesh 시뮬레이션 아키텍처를 NVIDIA BlueField-3 DPU의 DOCA(Data Center Infrastructure-on-a-Chip Architecture) SDK를 사용하여 실제 하드웨어로 이식하기 위한 기술 명세 및 수정 사항을 정의합니다.

## 아키텍처 전환 원칙

* **데이터 구조**: 시뮬레이션 전용 메타데이터를 제거하고 하드웨어 친화적인 바이너리 구조체로 복원합니다.
* **실행 모델**: 동기식 Busy-polling을 제거하고 DOCA Progress Engine 기반의 비동기 Callback 모델을 채택합니다.
* **메모리 관리**: 공유 메모리(/dev/shm) 파일을 `doca_mmap` 및 `doca_buf` 기반의 하드웨어 메모리 리전으로 대체합니다.

---

## 1. Descriptor 명세 (Message Structure)

시뮬레이션 코드의 `SwDescriptor` 클래스에서 `src_`로 시작하는 모든 상태 추적 필드를 제거하고, `README-DPUmesh.md`에서 정의한 표준 C 구조체로 일대일 매핑합니다.

```c
struct sw_descriptor {
    uint64_t header_buf_addr;  /* 실제 PCIe 가상 주소 */
    uint32_t header_len;
    uint64_t body_buf_addr;    /* 실제 PCIe 가상 주소 */
    uint32_t body_len;
    uint32_t req_id;           /* ExecutionContext 식별자 */
    uint32_t step_id;          /* EgressStep 식별자 */
    union {
        struct {
            uint8_t  ip[16];
            uint16_t port;
        } external;            /* Case 1: 외부 노드 */
        uint32_t pod_id;       /* Case 2/3: 목적지 워커 식별자 */
    } dst;
    uint32_t flags;            /* OpFlag | CaseFlag */
    uint32_t valid;            /* 하드웨어 유효성 비트 */
};
```

**수정 사항:**
* `src_body_pool_type`, `src_body_buf_slot` 등 시뮬레이션 필드 제거.
* 주소 지정 방식을 `Slot Index`에서 `uint64_t Address`로 변경.
* 완료 통지는 디스크립터 내부의 기록이 아닌 DOCA CQ(Completion Queue)를 통해 처리.

---

## 2. PodRegistry 전환 (Management Structure)

JSON 파일 기반의 레지스트리를 DPA(RISC-V)가 즉시 참조할 수 있도록 DPU DRAM 상의 고정 오프셋 바이너리 룩업 테이블(Lookup Table)로 교체합니다.

```c
struct pod_registry_entry {
    uint64_t rx_header_mkey;   /* 목적지 Pod의 Header 버퍼 원격 접근 키 */
    uint64_t rx_body_mkey;     /* 목적지 Pod의 Body 버퍼 원격 접근 키 */
    uint64_t rx_sq_doorbell;   /* 목적지 Pod의 RX SQ를 깨우기 위한 Doorbell 주소 */
    uint32_t is_active;        /* 워커 생존 여부 */
    uint32_t padding;
};

/* DPU DRAM의 특정 주소에 위치하는 고정 크기 배열 */
struct pod_registry_entry pod_lookup_table[MAX_PODS];
```

**동작 로직:**
* DPA는 디스크립터의 `dst.pod_id`를 인덱스로 사용하여 `pod_lookup_table[pod_id]`에 즉시 접근합니다.
* 테이블에서 획득한 `mkey`와 주소를 조합하여 `doca_dpa_dev_comch_producer_dma_copy`를 실행합니다.

---

## 3. 기능 블록별 이식 가이드

### Buffer Pool → DOCA Memory Management
* **Python**: `BufferPool` 클래스 및 `/dev/shm` 파일.
* **DOCA**: `doca_mmap` 객체를 생성하고 호스트의 거대 페이지(Hugepages)를 등록합니다.
* **관리**: `doca_buf_inventory`를 사용하여 슬롯 할당/해제를 수행합니다. `PoolType` 대신 각 리전에 할당된 `mkey`를 사용합니다.

### SQ/CQ → DOCA Work Queue
* **Python**: `DescriptorRing` 클래스 (원형 큐).
* **DOCA**: `doca_workq` 또는 `doca_comch`의 내부 큐를 사용합니다.
* **데이터 이동**: `put()`/`get()` 메서드는 `doca_workq_submit()` 및 `doca_pe_progress()`로 치환됩니다.

### DMA Manager → DPA Firmware
* **Python**: `dpa_daemon.py`의 `DMAManager` (Busy-polling + Memcpy).
* **DOCA**: BlueField-3 DPA 내부에서 실행되는 전용 RISC-V 펌웨어 코드로 작성합니다.
* **복사 연산**: `DMASimulator.copy_buffer` (CPU 복사)를 `doca_dpa_dev_comch_producer_dma_copy` 하드웨어 명령어로 교체합니다.

### Server Event Loop → DOCA Progress Engine
* **Python**: `server.py`의 `DPUmeshServer.run` (1ms 간격 Polling).
* **DOCA**: `doca_pe_progress(pe)` 기반의 이벤트 루프를 구성합니다.
* **깨우기**: `wait_interrupt()` 대신 하드웨어가 생성하는 Completion Event를 기다리며, 데이터 도착 시 미리 등록된 `callback` 함수가 호출되도록 설계합니다.

---

## 4. 데이터 흐름 (DOCA 기반)

### Case 3: Pod A → Pod B (Direct DMA)
1. **Host A**: 데이터를 버퍼에 쓰고 `doca_workq_submit`을 통해 디스크립터 전송.
2. **DPA (1차)**: Host A의 TX SQ에서 디스크립터 수신. `header_buf_addr`를 사용하여 Header만 DPU DRAM으로 복사.
3. **ARM**: DPU DRAM의 Header를 읽어 라우팅 결정 후 `pod_lookup_table`에서 Pod B의 `mkey` 추출.
4. **DPA (2차)**: 획득한 `mkey`를 사용하여 Host A의 Body 버퍼에서 Host B의 RX 버퍼로 **직접 PCIe DMA** 실행.
5. **Host B**: DMA 완료 후 발생하는 `imm data` 인터럽트를 통해 `doca_pe_progress` 루프가 깨어나며 Callback 실행.

## 5. 성능 최적화 핵심 사항

* **Zero-copy**: 모든 데이터 이동은 CPU의 개입 없이 DMA 엔진에 의해 수행되어야 합니다.
* **Mkey 분리**: 서비스(Pod) 단위로 별도의 `mmap`과 `mkey`를 관리하여 보안 및 자원 격리를 구현합니다.
* **Asynchronous Chaining**: DMA 복사 완료와 디스크립터 전달을 하나의 하드웨어 태스크 체인으로 묶어 지연시간을 최소화합니다.
