# face_sprite_pipeline

이 프로젝트는 게임 NPC(약 1000명)의 미연시식 상반신 초상화와 표정 스프라이트를 ComfyUI로 자동 생성하는 도구입니다. 결과물은 Unity에서 씁니다.

## 현재 상태
- `pipeline.py`: CLI 버전입니다. ComfyUI HTTP API(/prompt, /history, /view, /upload/image)를 직접 호출합니다.
  - 흐름: base 생성(txt2img) → (머리가 위쪽 가장자리에 닿으면 아래로 내리고 위쪽 아웃페인트, config `headroom`) → 얼굴 검출(YOLO) → 얼굴만 크롭·확대해 표정별로 인페인트(seed 고정) → 오버레이 PNG와 meta.json 출력
  - ComfyUI 기본 노드만 사용합니다(커스텀 노드 없음).
- `config.json`: 생성 설정, `expressions.json`: 표정 태그와 성격별 표정 프리셋, `characters.csv`: 캐릭터 정의표
- `unity/NpcPortrait.cs`: Unity에서 base 위에 오버레이를 표시하는 컴포넌트
- `tests/fake_comfy.py`: ComfyUI 모의 서버(포트 8000)입니다. 워크플로우 그래프를 검증하고 더미 이미지를 돌려줍니다. GPU가 없어도 전체 흐름을 테스트할 수 있습니다.
  - `Pipeline` 클래스: `generate_base(row, seed)`, `generate_expression(cid, name, denoise, seed)`, `run_character(...)`. 진행 이벤트는 `on_progress(dict)`, 일시정지/취소는 `control`(wait/cancelled)로 받습니다. 얼굴 정보는 `output/<id>/work.json`에 저장합니다.
- `server.py`: 웹 UI(FastAPI, 포트 7870). 작업 큐는 스레드 1개, 진행 상황은 SSE(`/api/events`). 프론트엔드는 `static/`(빌드 없음). Windows 실행은 `start_ui.bat`.
- `tests/fake_comfy.py`: `/object_info`, `/interrupt`, `/queue`도 흉내 냅니다. `--delay 0.5` 또는 `POST /_fake/config {"delay":0.5}`로 이미지당 지연을 줄 수 있습니다(일시정지/취소 테스트용).
- `tests/test_headroom.py`: 머리 위 여백(아웃페인트) 테스트. `python tests/test_headroom.py`
- `tests/test_web.py`: 웹 서버 종단 간 테스트(임시 폴더 사용, `output/`을 건드리지 않음). `python tests/test_web.py`
- 실제 GPU·모델 검증: base 생성 + 머리 위 여백 아웃페인트만 확인했습니다(2026-10-14). 표정 인페인트·웹 UI 전체 흐름은 아직 모의 서버로만 테스트했습니다.
- **체크포인트(boleromixWAINSFW_v10)의 내장 VAE가 망가져 있습니다.** 인코드→디코드만 해도 뿌옇게 변해서(평균 오차 26), 인페인트/아웃페인트 부분이 회색 막처럼 나옵니다. 그래서 config `vae`에 `sdxl_vae_fp16_fix.safetensors`(C:\AI\ComfyUI\modelsae)를 지정해 씁니다. 체크포인트를 바꾸면 이 설정도 다시 확인하세요.
- `parts_tool.py` + `parts.json`: 소모성 NPC 용 파츠 조합 실험(본 파이프라인과 별개, 결과는 `parts_out/<id>/`).
  - 웹 UI [파츠] 탭: 소스 목록 편집(`PUT /api/parts/sources`), ①소스 생성·②레이어 분리(`POST /api/parts/jobs` → 같은 작업 큐, mode `parts_gen`/`parts_split`), 소스 미리보기(check/sheet), 조합 미리보기(`GET /api/parts/compose?skin=&eyes=&hair=&outfit=&glasses=`). 서버 인자 `--parts`.
  - 큐는 SDXL ↔ SeeThrough 작업이 바뀔 때 `/free` 로 VRAM 을 비웁니다(`JobQueue._switch_engine`).
  - 모의 서버는 SeeThrough 노드·`/free` 도 흉내 냅니다. 테스트: `tests/test_web.py` 의 `test_24_parts`.
  - `gen`(1단계): cowboy shot 으로 찍고 얼굴 기준으로 잘라 키운 뒤 img2img 0.4 → 모든 소스의 얼굴 위치·크기가 같음.
  - 생성 이미지 가져오기: [파츠] 탭 [생성 이미지에서 가져오기] → `POST /api/parts/import {ids, align, split}`. 소스에 `"from": 캐릭터 id`, `"align"` 이 붙고, `gen`(CLI·웹 모두) 이 이런 소스는 새로 그리지 않고 `output/<id>/base_src.png` 를 다시 가져옴(`import_source`, GPU 안 씀, 얼굴은 work.json 것 사용). align 이면 `align_crop` 으로 gen 소스와 구도를 맞춤(img2img 다듬기는 안 함). 소스 id 는 캐릭터 id, 겹치면 `_2`. 테스트: `test_25_parts_import`.
  - 소스 삭제: 목록 저장 시 빠진 소스와 새 id 에 남아 있던 예전 결과 폴더를 `parts_out/.deleted/<id>-<시각>` 으로 옮김(`retire_parts_dirs`, 지우지 않음). 같은 id 로 다시 만들어도 예전 그림이 안 나오게. 테스트: `test_26_parts_delete_moves_results`.
  - `split`(2단계): SeeThrough 커스텀 노드(ComfyUI-See-through)로 레이어 분리. 이 단계만 커스텀 노드를 씁니다. 좌표는 source.png 기준으로 되돌려 저장합니다.
  - SeeThrough LayerDiff 를 NF4 + group_offload 로 쓰면 CUDA illegal memory access 로 ComfyUI 가 죽습니다. group_offload 는 끕니다. 최대 VRAM 약 8.7GB, 장당 약 4~5분.
  - 단계마다 시작할 때 `/free` 로 이전 단계 모델을 내립니다.
  - `preview`: GPU 없이 저장된 레이어로 check.png / sheet.png 만 다시 그림.
  - PSD 2종(레이어 / 깊이): SeeThrough 노드의 [Download PSD] 버튼은 ComfyUI 화면(브라우저)에서만 동작하므로 직접 만듦(`make_psd`, 의존성 없는 PSD 작성기). 웹 UI 소스 미리보기 아래 버튼(`GET /api/parts/psd/{id}?kind=rgba|depth`), CLI `python parts_tool.py psd`. 깊이 레이어는 분리할 때 `layers/<이름>_depth.png` 로 저장하고, 그 전에 분리한 소스는 첫 다운로드 때 ComfyUI output 에서 받아 채움.
  - 조합 시 얼굴·머리 소스가 다르면 `adapt_face`: face 레이어에 남은 원래 앞머리 그림자를 걷고 머리 소스 얼굴의 음영을 입힘, 눈높이 위쪽 윤곽은 머리 소스 얼굴에 맞춰 자름, 머리 소스 얼굴을 밑에 깖. 귀는 머리 그룹.
  - SeeThrough 가 머리카락 덩어리를 nose 등으로 잘못 분류할 때가 있음(s05 nose 336×350). `layer_issues` 가 비정상 크기 이목구비를 찾아 정상 위치만 남김(읽을 때 적용, 웹 UI 에 ⚠ 표시).
  - 목/깃 마스크(`collar_mask`): 뒷깃·후드 안쪽은 목 뒤, 앞깃은 목 앞이라 순서 하나로 안 됨 → SeeThrough 깊이 레이어를 픽셀마다 비교해 '목 뒤로 가는 상의 부분'을 구하고, 목을 상의 바로 앞에 그리며 마스크를 씌움(레이어를 새로 나누지 않음). 가장자리 깊이는 부정확해서 안쪽에서만 판정하고 나머지는 가장 가까운 판정을 따름. 조합에서는 상의 소스의 마스크를 씀. PSD 에서는 목 레이어의 레이어 마스크. 깊이 레이어가 없는 예전 소스는 적용 안 됨(`python parts_tool.py psd` 로 채움). 테스트: `python tests/test_collar.py`.
  - 레이어 그리는 순서는 `DRAW_ORDER` 로 고정합니다. SeeThrough 깊이 중앙값은 순서가 뒤집혀서(흰자가 홍채 앞, 트윈테일 back hair 가 얼굴 앞) 쓸 수 없습니다.
- ControlNet 자세 고정(config `controlnet`, 기본 꺼짐): base txt2img 에만 `ControlNetLoader` → `SetUnionControlNetType` → `ControlNetApplyAdvanced`(기본 노드)를 겁니다. 포즈 템플릿은 `poses/*.png`(OpenPose 뼈대), 캐릭터별로는 CSV `pose` 칸. 템플릿은 `python pipeline.py --make-pose <id>` 로 base 에서 뽑습니다(이것만 comfyui_controlnet_aux 의 DWPreprocessor 사용). DWPreprocessor 의 인물 검출 yolox_l.onnx 는 이 PC 에서 애니 그림(전신 포함)의 사람을 하나도 못 찾아 빈 뼈대를 냄 → `bbox_detector="None"`(그림 전체를 한 사람으로)으로 씀. 빈 결과면 `make_pose` 가 저장하지 않고 오류. 기본 포즈 파일이 없으면 `ConfigError`(큐 멈춤). parts_tool 의 cowboy shot 에는 걸지 않습니다. 모델은 `diffusion_pytorch_model.safetensors`(xinsir union promax, SDXL). 실제 GPU 검증(2026-10-31, 템플릿 npc_0010): 같은 seed 로 끔/켬 비교 시 0.6/0.45 는 전신→상반신은 잡지만 강한 클로즈업은 못 바꿈 → 0.8/0.6 으로 클로즈업 3명 중 2명이 템플릿 크기로 맞음(1.0/0.7 은 차이 없음). npc_0001 seed 는 1.0 에서도 클로즈업. 테스트: `python tests/test_controlnet.py`.
- 아웃페인트는 빈칸을 배경색으로 채우고, 부드러운 마스크 + DifferentialDiffusion(기본 노드)로 한 번에 그립니다. 원본 윗줄을 늘려 채우거나 여러 번 나눠 그리면 줄무늬·겹침이 생깁니다.

## 사용 환경
- Windows, NVIDIA 16GB VRAM
- **ComfyUI Desktop, 포트 8000**
- 모델: Illustrious 계열 SDXL(WAI-illustrious-SDXL)
- 사용자는 한국어를 씁니다. UI 문구와 메시지는 한국어로 작성합니다.

## 규칙
- 유료 외부 API를 추가하지 마세요. 모든 처리는 로컬에서 합니다.
- `python pipeline.py` CLI는 계속 동작해야 합니다.
- 출력 폴더 구조(`output/<id>/base.png, expr/*.png, meta.json`)와 meta.json 스키마는 Unity 스크립트가 의존하므로 바꾸지 마세요. 필드 추가만 허용합니다.
- 테스트는 `python tests/fake_comfy.py`를 백그라운드로 띄운 뒤 실행합니다.
  - 사용자가 ComfyUI Desktop을 8000에서 켜 두는 경우가 있습니다. 그때는 모의 서버를 `--port 8001`로 띄우고 `FAKE_COMFY_URL=http://127.0.0.1:8001 python tests/test_web.py`로 실행합니다. 실제 ComfyUI로 테스트 생성을 보내지 마세요.

## 다음 작업
웹 UI를 구현합니다. 명세는 `docs/WEB_UI_SPEC.md`에 있습니다.
- Phase 1 완료: server.py 뼈대, 설정 탭, 생성 큐(base/full), SSE 로그, 검수 갤러리.
- Phase 2 완료: 캐릭터 표 편집(PUT /api/characters, X-Version/X-Base-Version 으로 충돌 감지, import/export), 상세 화면의 [승인]·[이 표정 다시 만들기]·[base 다시 뽑기]. 새 seed 로 다시 뽑은 base 의 seed 는 CSV seed 칸에 자동 기록합니다(SSE `characters` 이벤트).
- Phase 3 완료: 대화창 미리보기(상세 화면 T), 디버그 오버레이(D, `/api/portraits/{id}/mask.png` + SVG), 승인된 것만 Unity 폴더로 내보내기(`/api/export/unity`, 백그라운드 스레드 + SSE `export` 이벤트, config `unity_export_dir`).
- 명세(docs/WEB_UI_SPEC.md)의 세 단계는 모두 구현했습니다. 남은 것은 실제 GPU·모델로 끝까지 돌려 보며 품질을 조정하는 일입니다.
