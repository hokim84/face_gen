# 게임 이미지 제작실

## 검수에서 일괄 삭제

검수 카드의 체크박스로 항목을 선택하거나 **현재 목록 전체 선택**을 누른 뒤 **선택 항목 삭제 → 영구 삭제**를 실행합니다. 검색·유형 필터에 보이는 목록을 기준으로 전체 선택하며, 삭제 대상 수와 이름을 확인할 수 있습니다.

삭제하면 CSV의 항목, `output/<id>/`의 원본·표정·메타데이터, 해당 항목의 레이어 결과와 이전 레이어·중단된 분리 결과, 연결된 파츠 소스 및 보관된 파츠 파일을 실제로 제거합니다. Unity로 내보낸 사본과 ComfyUI 내부 저장 파일은 대상에 포함하지 않습니다. 대기·생성 중인 항목과 연결된 파츠는 작업 종료 또는 취소 후 삭제할 수 있습니다. 삭제 실패 항목은 별도로 표시하고 재시도할 수 있습니다. API는 `POST /api/portraits/delete {"ids": ["..."]}`이며, 응답의 `deleted`와 `failed`로 결과를 구분합니다.

## 이미지 유형별 작업

상단에서 **주인공/조연 · NPC · 몬스터**를 선택합니다. 각 화면에서 이미지를 먼저 생성하고 검수한 뒤 필요한 후처리를 실행합니다.

| 유형 | 작업 | 입력·포즈 |
|---|---|---|
| 주인공/조연 | 이미지, 표정 오버레이, 기본 이미지 레이어 분리 | 외형·성격·표정, 유형 기본 포즈 또는 개별 포즈 |
| NPC | 이미지, 기본 이미지 레이어 분리 | 외형, 유형 기본 포즈 또는 개별 포즈 |
| 몬스터 | 이미지 | 자유 설명·구도 태그, 사람용 포즈·얼굴 검출 없음 |

- `전체 생성`은 주인공/조연의 이미지→표정→레이어, NPC의 이미지→레이어를 이어서 실행합니다. 레이어 분리에는 ComfyUI-See-through가 필요합니다.
- 레이어는 크롭·정렬 없이 원본 크기와 좌표를 유지하고 `output/_layers/<id>/`에 저장합니다. 검수에서 합성 미리보기와 PSD 다운로드를 제공합니다. 표정 오버레이와 레이어 PSD는 각각의 결과물입니다.
- 기존 파츠 조합 도구는 별도 소스의 얼굴 정렬 등 기존 기능을 유지합니다.
- 기존 CSV의 유형 없는 행은 주인공/조연으로 표시합니다. `image_type` 값은 `character`, `npc`, `monster`이며, 몬스터 설명은 `prompt` 열에 기록합니다. 읽기만으로 CSV를 바꾸지 않고 저장 시 유형을 기록합니다.
- 유형 변경 시 기존 결과는 보존하되 새 유형의 완료·승인 판정에서 제외합니다. 원본 재생성 시 표정·승인과 레이어의 유효 상태가 초기화됩니다. 레이어 재분리 실패 시 이전 성공 결과는 유지하며, 교체된 레이어는 `_layers/.history/`에 보관합니다.
- 주인공/조연·NPC의 기본 포즈는 설정의 `type_poses.character`, `type_poses.npc`에서 선택합니다. 두 유형은 ControlNet을 사용하며, 몬스터는 사용하지 않습니다.
- 설정의 유형별 기본 프롬프트에서 각 유형의 긍정 태그와 네거티브를 편집할 수 있습니다(`type_prompts.<유형>.positive/negative`). 긍정 태그는 공통 품질 태그·개별 외형 설명에, 네거티브는 공통 네거티브에 추가합니다. 빈 문자열로 저장하면 해당 유형의 추가 태그를 끕니다. 기본 몬스터 설정은 비인간형 생물 특징을 강조하고 미소녀·사람형을 억제합니다. 사람형 몬스터를 만들 때는 해당 네거티브를 조정하세요. 모델 편향에 따라 결과가 달라지며, 기존 이미지는 자동으로 다시 생성하지 않습니다.
- 전체 작업 완료 후 승인하면 기존 Unity 내보내기를 이용할 수 있습니다. NPC·몬스터는 표정 목록이 비어 있는 이미지 메타데이터를 내보냅니다.

CLI도 CSV 유형을 따릅니다. `--base-only`는 이미지 생성, `--expressions-only`는 주인공/조연 표정 생성, `--layers`는 기존 이미지 레이어 분리입니다. 옵션 없이 실행하면 유형별 전체 작업을 수행합니다.

```powershell
python pipeline.py --only npc_0001 --base-only
python pipeline.py --only npc_0001 --expressions-only
python pipeline.py --only npc_0001 --layers
```

아래는 표정 오버레이 및 기존 기능의 상세 설명입니다.

이 파이프라인은 캐릭터 표(`characters.csv`)를 읽어서 ComfyUI로 **상반신 base 이미지와 표정 오버레이**를 자동으로 만들고, Unity에서 바로 쓸 수 있는 형태로 출력합니다.

```
characters.csv ─▶ base 생성 (txt2img) ─▶ 얼굴 검출 ─▶ 표정별 얼굴 인페인트 (seed 고정)
                                                     └▶ 오버레이 PNG + meta.json ─▶ Unity NpcPortrait
```

- 표정이 바뀌어도 얼굴 밖(머리·옷·몸)은 **픽셀 단위로 같은 이미지**입니다. 그래서 Unity에서는 base 위에 오버레이 한 장만 바꿔 끼우면 됩니다.
- ComfyUI **기본 노드만** 사용하므로 커스텀 노드를 설치할 필요가 없습니다.
- 성격(명랑/냉정/다혈질/소심/오만/진지)마다 만들 표정 세트가 정해져 있습니다(`expressions.json`).

## 1. 준비 (Windows, VRAM 16GB 기준)

1. **ComfyUI**: ComfyUI Desktop 기준입니다. Desktop의 기본 포트는 **8000**이고, `config.json`의 `comfy_url`도 8000으로 맞춰 두었습니다. 포트는 Desktop 설정의 Server-Config에서 확인할 수 있습니다. portable 버전을 쓴다면 8188로 바꾸세요.
2. **모델 받기**: Illustrious 계열 체크포인트를 받아 Desktop 설치 때 지정한 폴더(기본값 `문서\ComfyUI`) 안의 `models\checkpoints\`에 넣습니다.
   - 추천: Civitai의 **WAI-illustrious-SDXL**(최신 버전). 그 밖의 Illustrious 파인튜닝 모델도 됩니다.
   - 받은 파일 이름을 `config.json`의 `checkpoint` 값에 그대로 적습니다.
   - 상업적으로 출시할 계획이면 모델 페이지의 라이선스를 꼭 확인하세요.
3. **스크립트 의존성 설치** (ComfyUI와 별개의 Python 3.10 이상):
   ```
   pip install -r requirements.txt
   ```
   - `ultralytics`는 얼굴 검출용입니다. 처음 실행할 때 `face_yolov8m.pt`를 자동으로 받습니다.
   - `rembg`는 배경 제거용입니다. 없어도 동작하지만, 그 경우 흰 배경이 그대로 남습니다.

## 2. 실행

ComfyUI가 켜져 있는 상태에서 실행합니다.

```
python pipeline.py --base-only        # ① base 이미지만 먼저 뽑아 검수
python pipeline.py                    # ② 표정까지 전부 생성
python pipeline.py --only npc_0003    # 특정 캐릭터만 다시
python pipeline.py --only npc_0003 --force   # base부터 완전히 다시
python pipeline.py --export-workflows # ComfyUI에서 열어볼 워크플로우 JSON 저장 (workflows/)
python pipeline.py --make-pose npc_0003      # 이 캐릭터 base 의 자세를 ControlNet 포즈 템플릿으로 저장 (poses/)
```

**추천 작업 순서**

1. `--base-only`로 base 이미지를 먼저 뽑습니다.
2. `output/<id>/base_src.png`를 확인합니다. 마음에 들지 않는 캐릭터는 그 파일을 지우거나 CSV에서 `seed`를 바꾼 뒤 다시 돌립니다. 직접 고른 그림으로 `base_src.png`를 교체해도 됩니다.
3. 전체 실행으로 표정을 생성합니다. 이미 끝난 캐릭터는 건너뛰므로, 중간에 끊겨도 다시 실행하면 이어서 진행됩니다.
4. `output/<id>/preview.jpg`(표정 한눈에 보기)로 검수합니다.

## 2-1. 웹 UI

브라우저에서 생성·검수할 수 있습니다. ComfyUI를 켠 상태에서 **`start_ui.bat`을 더블클릭**하거나 다음을 실행합니다.

```
python server.py              # http://127.0.0.1:7870 이 자동으로 열림
python server.py --no-browser --port 7871
```

| 탭 | 할 수 있는 일 |
|---|---|
| 캐릭터 | `characters.csv` 표 편집: 칸을 누르면 편집(Enter 확정, Tab 다음 칸, Esc 취소), 추가·복제·삭제, 성격은 드롭다운. 고친 칸은 노란색, 오류는 빨간색으로 표시되고 [저장](Ctrl+S)을 눌러야 파일에 기록됩니다. CSV 가져오기(UTF-8 / CP949, 합치기 또는 전부 바꾸기)와 내보내기. 선택한 캐릭터의 [base 생성] [전체 생성]. 상태 배지를 누르면 검수 화면이 열립니다 |
| 생성 큐 | 진행률, 장당 평균 시간, 남은 예상 시간, 일시정지/재개/취소, 실시간 로그, 방금 생성된 이미지 |
| 검수 | 상태·성격 필터, 카드를 누르면 게임과 같은 방식(base + 오버레이)으로 미리보기. ←/→ 표정 전환, ↑/↓ 이전·다음 캐릭터, 1~9 표정 바로 선택. **[승인]**(단축키 A, 승인 후 다음 캐릭터로 자동 이동), **[이 표정 다시 만들기]**(denoise 조절, 새 seed 선택), **[base 다시 뽑기]**(두 번 눌러 실행, 표정까지 이어서 생성 가능). **대화창 미리보기**(T): 대사를 입력하면 게임 대화창처럼 보여 주고, 줄 앞에 `smile:`을 쓰면 그 줄에서 표정이 바뀝니다(Space/클릭으로 넘김). **디버그**(D): 합성 마스크·얼굴 박스·크롭·오버레이 위치 표시. **[Unity로 내보내기]**: 승인된 캐릭터만 Unity 폴더로 복사 |
| 설정 | `config.json` 폼 편집(체크포인트·LoRA·샘플러 드롭다운은 ComfyUI에서 목록을 받아옴), `expressions.json` 표정 태그·성격 프리셋 편집, ComfyUI 연결 테스트 |

- 생성은 한 번에 하나씩 처리합니다. 일시정지는 지금 만들고 있는 이미지까지만 끝내고 멈춥니다. 취소하면 ComfyUI에 `/interrupt`를 보냅니다.
- ComfyUI가 꺼져 있으면 큐가 자동으로 일시정지됩니다. ComfyUI를 켠 뒤 [재개]를 누르세요.
- 설정을 저장하면 이전 파일이 `config.json.bak` / `expressions.json.bak`으로 남습니다.
- base를 새 seed로 다시 뽑으면 그 seed가 `characters.csv`의 seed 칸에 자동으로 기록됩니다. 그래서 나중에 CLI로 `--force`를 해도 같은 그림이 나옵니다. 이때 기존 표정과 승인은 지워집니다.
- 표정을 다시 만들면 그 캐릭터의 승인은 풀립니다. 승인은 `meta.json`의 `"approved": true`(와 `approved_at`)로 기록됩니다.
- 표를 저장하면 이전 파일이 `characters.csv.bak`으로 남고, 엑셀에서 한글이 깨지지 않게 UTF-8(BOM)으로 저장합니다. 엑셀에서 파일을 열어 둔 채로는 저장할 수 없습니다.
- 배경 제거가 얼굴까지 지워 버리면(드물게 인물을 배경으로 오인) 배경 제거를 건너뛰고 경고를 남깁니다.
- 큐 목록은 메모리에만 있어서 서버를 끄면 사라집니다. 결과 파일은 그대로 남고, 다시 [전체 생성]을 누르면 완료된 캐릭터는 건너뜁니다.

## 3. 출력 구조

```
output/npc_0001/
  base_src.png      원본 생성 이미지 (재사용됨)
  base.png          배경 제거된 base (Unity용)
  expr/smile.png    표정 오버레이 (투명 PNG, 얼굴 부분만)
  meta.json         각 오버레이의 좌표 (x, y, w, h — 좌상단 기준 픽셀)
  preview.jpg       검수용 시트
  work.json         base seed, 얼굴 박스·크롭 정보 (표정 하나만 다시 만들 때 재검출하지 않으려고 저장)
```

- base를 새로 만들면(`--force` 또는 새 seed) 이전 base 기준의 `base.png`, `expr/*.png`, `meta.json`, `work.json`, `preview.jpg`는 삭제됩니다. 새 base와 맞지 않기 때문입니다.
- `meta.json`에는 `seed`(base seed)와 표정별 `seed`, `denoise` 필드가 추가되었습니다. 기존 필드는 그대로라 Unity 스크립트는 수정할 필요가 없습니다.

## 4. 캐릭터 표 (`characters.csv`)

| 열 | 설명 | 예 |
|---|---|---|
| id | 고유 ID (폴더 이름) | npc_0001 |
| subject | `1girl` / `1boy` | 1girl |
| age | 선택 | mature female |
| hair / eyes / outfit / extra | Danbooru 태그 (영어) | long hair, blonde hair |
| personality | 성격 프리셋. `명랑\|소심`처럼 여러 개를 섞을 수 있음 | 명랑 |
| expressions | 비워 두면 성격 프리셋을 따름. 직접 지정할 때는 `neutral\|smile\|sad` | |
| pose | ControlNet 포즈 템플릿 이름 (`poses/` 안의 PNG, 확장자 생략 가능). 비우면 기본 포즈 | tilt |
| seed | 비워 두면 id로 고정 seed가 자동 생성됨 | |

- 엑셀에서 저장할 때는 **CSV UTF-8** 형식이 안전합니다. 일반 CSV(CP949)도 읽을 수 있습니다.
- **[AI 자동 추가]**: 캐릭터 탭에서 설명(예: "판타지 마을 주민, 다양한 직업과 외형")과 인원수(기본 5명, 1~50명)를 입력합니다. 이름·외형 태그·성격을 생성해 표 초안에 추가합니다. 검토·편집 후 **[저장]**해야 CSV에 기록되며 이미지는 별도로 생성합니다.
  - 서버를 실행하는 환경의 PATH에 Codex CLI와 Node.js가 있어야 합니다. 먼저 터미널에서 `codex login` 및 `codex exec --help`가 동작하는지 확인하세요. 기존 CLI 로그인·모델 설정을 사용하며 별도 API 키 입력은 없습니다.
  - 생성 중 창을 닫으면 결과를 표에 추가하지 않습니다. 실행 자체는 완료되거나 300초 제한에 도달할 때까지 계속되며, 그동안 새 요청은 받지 않습니다. 실패하면 기존 표는 그대로 유지됩니다.
  - 성격은 현재 프리셋 중 하나를 고르고, 표정 직접 지정과 seed는 비워 둡니다. 사용자 정의 CSV 열도 새 행에서는 빈칸으로 유지합니다.
  - `POST /api/characters/generate`에 `{ "description": "판타지 마을 주민", "count": 5 }`를 보내면 `{ "rows": [...] }`를 반환합니다. ID는 화면에서 기존 ID 및 결과물 폴더와 겹치지 않게 부여합니다. 이 API는 CSV나 이미지 생성 큐를 변경하지 않습니다.
  - GPU와 Codex 로그인 없이 검증: `python -m unittest discover -s tests -p "test_character*.py" -v`, `node tests/test_character_ui.cjs`.

## 5. 품질 튜닝 (`config.json` / `expressions.json`)

### 자세 고정 (ControlNet, `config.json` 의 `controlnet`)
캐릭터마다 어깨선·머리 위치·얼굴 방향이 들쭉날쭉할 때 씁니다. base 생성에만 걸리고, 표정 인페인트·여백 아웃페인트에는 쓰지 않습니다. 기본값은 꺼져 있습니다.

1. SDXL 용 union 모델(xinsir `controlnet-union-sdxl-1.0` 의 promax)을 `ComfyUI/models/controlnet` 에 넣습니다. SD1.5 모델(`control_v11…`)은 쓸 수 없습니다.
2. 자세가 마음에 드는 base 를 하나 골라 템플릿을 만듭니다: `python pipeline.py --make-pose npc_0003` → `poses/upper_front.png`. 이 단계만 `comfyui_controlnet_aux`(DWPreprocessor) 커스텀 노드가 필요합니다. 다른 이름으로 저장하려면 `--pose-name tilt`.
3. 웹 UI 설정 탭 [자세 고정 (ControlNet)]에서 모델을 고르고 켭니다.

| 키 | 기본 | 설명 |
|---|---|---|
| union_type | openpose | union 모델에서 쓸 종류. 단일 용도 모델이면 `""` |
| pose / pose_dir | upper_front.png / poses | 기본 포즈 파일과 폴더. 캐릭터별로는 CSV `pose` 칸 |
| strength | 0.8 | 0.6 이면 전신은 상반신으로 잡히지만 클로즈업은 그대로인 경우가 많음. 1.0 은 0.8 과 거의 같음 |
| start / end | 0 / 0.6 | 전체 스텝 중 이 구간에만 적용. 구도만 잡고 세부는 모델에 맡김 |

그래도 클로즈업으로 나오는 seed 가 있습니다 (예: npc_0001 은 강도 1.0 에서도 그대로). 그런 캐릭터는 [base 다시 뽑기]로 seed 를 바꾸세요.


| 증상 | 조정 |
|---|---|
| 표정 변화가 약함 | `expression.denoise`를 0.55에서 0.6~0.65로 올리거나, 표정별로 `{"tags": ..., "denoise": 0.65}` 지정 |
| 표정 생성 시 얼굴형·눈 색이 바뀜 | denoise를 0.45~0.5로 낮춤 |
| 오버레이 경계가 보임 | `feather`를 0.06에서 0.1로 올림 |
| 앞머리·귀까지 바뀜 | `mask_expand`를 1.25에서 1.1로 낮춤 |
| 얼굴 디테일이 뭉개짐 | `crop_scale`을 2.2에서 1.8로 낮춤 (얼굴이 더 크게 확대됨) |
| 그림체를 통일하고 싶음 | `loras`에 스타일 LoRA 추가: `[{"name": "my_style.safetensors", "strength": 0.6}]` |
| 웹툰 느낌을 원함 | 웹툰/manhwa 스타일 LoRA를 추가하거나 `style_suffix`에 태그 추가 |

## 6. Unity 적용

1. 웹 UI 검수 탭의 **[Unity로 내보내기]** 에 `D:\MyGame\Assets\Resources\Portraits` 같은 경로를 넣고 실행합니다. **승인된 캐릭터만** `base.png`, `expr/*.png`, `meta.json`을 복사합니다(경로는 설정에 저장됨).
   - 바뀌지 않은 파일은 건너뛰므로 다시 내보내도 빠릅니다. 파일을 덮어쓰기 때문에 Unity의 `.meta`(GUID)가 유지되어 씬·프리팹 참조가 끊기지 않습니다.
   - 표정 목록이 바뀌어 필요 없어진 오버레이는 지웁니다. "승인이 풀린 캐릭터 지우기"를 켜면 승인이 풀린 캐릭터 폴더도 지웁니다. 지우는 대상은 이 도구가 내보낸 폴더(`meta.json`의 id가 폴더 이름과 같은 것)뿐입니다.
   - CLI만 쓴다면 `output/` 안의 캐릭터 폴더들을 `Assets/Resources/Portraits/`로 직접 복사해도 됩니다.
2. UI에 RawImage 두 개(Base, 그 자식으로 Overlay)를 만들고, `unity/NpcPortrait.cs`를 붙여 연결합니다.
3. 코드에서 이렇게 호출합니다.
   ```csharp
   portrait.SetCharacter("npc_0001");
   portrait.SetExpression("smile");   // 없는 표정이면 neutral로 대체
   ```
4. 텍스처 Import 설정의 Max Size가 원본(1216)보다 크게 잡혀 있는지 확인하세요. 좌표는 meta.json 기준이라 크기가 줄어도 위치는 맞지만, 화질이 떨어집니다.

## 7. 소요 시간 (대략적인 추정)

- SDXL 1024px 기준, 16GB GPU에서 장당 약 5~8초가 걸립니다.
- NPC 1명은 base 1장과 표정 5~6장입니다.
- 따라서 1000명이면 약 6000~7000장, **10~15시간** 정도로 예상됩니다. 밤새 돌리는 방식을 추천합니다.

전부 로컬에서 실행되므로 생성 비용은 들지 않습니다.
