# 웹 UI 명세 (v0.1)

## 목표
`pipeline.py`를 브라우저에서 조작하는 **로컬 웹 앱**을 만듭니다. 웹 앱에서 할 일은 네 가지입니다.

- 캐릭터 표 편집
- 생성 실행과 진행 상황 확인
- 결과 검수와 재생성
- 설정 편집

## 아키텍처
```
브라우저 (http://127.0.0.1:7870)
   │  REST + SSE
FastAPI 서버 (server.py) ── pipeline.py 함수 재사용
   │  HTTP
ComfyUI Desktop (http://127.0.0.1:8000)
```
- 브라우저가 ComfyUI를 직접 부르지 않습니다. CORS 문제가 있고 파일 저장도 필요하기 때문에 백엔드가 중계합니다.
- 프론트엔드는 **빌드 단계 없이** 동작하게 만듭니다. `static/` 폴더에 HTML, JS, CSS를 두고 FastAPI가 서빙합니다. 사용자가 Node를 설치하지 않아도 되게 하려는 것입니다.
- 실행은 `python server.py` 한 줄로 끝나야 하고, 실행하면 브라우저가 자동으로 열려야 합니다. Windows용 `start_ui.bat`도 함께 둡니다.
- 생성 작업은 백그라운드 스레드 1개와 큐로 처리합니다. GPU가 하나뿐이라 동시에 하나씩만 돌립니다.

## pipeline.py 리팩터링
- `process_character`를 두 함수로 나눕니다.
  - `generate_base(row, seed)`
  - `generate_expression(cid, name, denoise=None, seed=None)`: 표정 1개만 다시 만들 수 있어야 합니다.
- 진행 콜백 `on_progress(event: dict)`를 추가합니다. 서버는 이 콜백으로 SSE 이벤트를 보냅니다.
- 얼굴 박스와 크롭 정보를 `output/<id>/work.json`에 저장합니다. 표정 하나만 재생성할 때 다시 검출하지 않기 위해서입니다.
- 기존 CLI 동작은 유지합니다.

## 화면 (탭 4개)

### 1. 캐릭터
- `characters.csv`를 편집 가능한 표로 보여줍니다(추가, 삭제, 복제, 검색).
- 성격은 `expressions.json`의 personalities에서 고르는 드롭다운으로 합니다.
- CSV 가져오기와 내보내기를 지원합니다. UTF-8과 CP949를 모두 읽을 수 있어야 합니다.
- 각 행에 상태 배지(미생성 / base만 / 완료 / 승인됨)를 표시합니다.
- 선택한 행에 대해 [base 생성], [전체 생성] 버튼을 둡니다.

### 2. 생성 큐
- 대기, 진행 중, 완료, 실패 목록을 보여줍니다.
- 전체 진행률, 장당 평균 시간, 남은 예상 시간을 표시합니다.
- 일시정지, 재개, 취소 버튼을 둡니다. 취소는 ComfyUI `/interrupt`를 호출합니다.
- 실시간 로그(SSE)와 방금 생성된 이미지 썸네일을 보여줍니다.

### 3. 검수 갤러리 (가장 중요)
- 캐릭터 카드 그리드를 보여줍니다(base 썸네일 + 이름 + 상태). 필터는 상태와 성격 두 가지입니다.
- 카드를 클릭하면 상세 화면이 열립니다.
  - base 위에 오버레이를 겹쳐서 **게임과 같은 방식으로 미리보기**합니다(meta.json 좌표 사용).
  - 표정 버튼을 누르면 표정이 즉시 바뀝니다. 키보드 ←/→로도 전환할 수 있습니다.
  - [base 다시 뽑기]: 새 seed로 base를 다시 만들고, 기존 표정은 무효화합니다.
  - [이 표정 다시 만들기]: denoise 슬라이더와 seed 변경 옵션을 줍니다.
  - [승인] 버튼으로 meta.json에 `"approved": true`를 기록합니다.
  - 마스크와 얼굴 박스를 겹쳐 보는 디버그 토글을 둡니다.
- 대화창 미리보기를 지원합니다. 하단 대화창 목업에 대사를 입력하면 초상화와 함께 보여줍니다.

### 4. 설정
- `config.json`을 폼으로 편집합니다(체크포인트, LoRA, 샘플러, denoise 등).
- 체크포인트와 LoRA 목록은 ComfyUI `/object_info/CheckpointLoaderSimple`과 `/object_info/LoraLoader`에서 받아 드롭다운으로 보여줍니다.
- `expressions.json`의 표정 태그와 성격 프리셋도 편집할 수 있게 합니다.
- [ComfyUI 연결 테스트] 버튼을 둡니다.

## 백엔드 API (초안)
```
GET  /api/status                  ComfyUI 연결 상태, 큐 상태
GET/PUT /api/config               config.json
GET/PUT /api/expressions          expressions.json
GET/PUT /api/characters           characters.csv (JSON 배열로 주고받음)
POST /api/characters/import       CSV 업로드
GET  /api/characters/export       CSV 다운로드
GET  /api/comfy/models            체크포인트/LoRA 목록
POST /api/jobs                    {ids:[], mode:"base"|"full"|"expression", expression?, denoise?, new_seed?}
POST /api/jobs/pause | resume | cancel
GET  /api/events                  SSE 진행 이벤트
GET  /api/portraits/{id}          meta.json + 상태
POST /api/portraits/{id}/approve
GET  /output/...                  결과 이미지 정적 서빙 (캐시 무효화를 위해 ?v=mtime)
```

## 구현 단계
1. **Phase 1**: server.py 뼈대, 설정 탭, 생성 큐(base/full), SSE 로그, 검수 갤러리 보기 전용
2. **Phase 2**: 캐릭터 표 편집, base 다시 뽑기, 표정 하나만 재생성, 승인
3. **Phase 3**: 대화창 미리보기, 디버그 오버레이, "승인된 것만 Unity 폴더로 내보내기" 버튼(경로 설정)

단계마다 `tests/fake_comfy.py`로 끝까지 동작하는지 확인합니다.

## 디자인
- 어두운 테마(장시간 검수용), 한국어 UI
- 갤러리 카드 비율은 832:1216입니다.
