# DramaKorean — 그로스 루프 로그

---

## 2026-07-29 06:11 UTC — 루프 2

**⚠ 데이터 판독 2연속 실패: egress 차단 지속 (조직 정책 403, 우회 불가).**

프록시 상태 엔드포인트(`/__agentproxy/status`)로 원인 확정 — TLS/설정 문제가 아니라
게이트웨이가 CONNECT에 403으로 답하는 **정책 거부**. README 지침상 재시도·우회 금지 대상.

- `pjvjweurelmosugwptdl.supabase.co` → 403 (퍼널 판독 불가)
- `business-api.tiktok.com` → 403 (지출 확인·광고그룹 생성 불가)
- `next-epic-dev.github.io` → 403 (라이브 HTML 검증 불가)
- git(origin)은 로컬 프록시로 정상

### 데이터 스냅샷
새 데이터 없음(3루프 연속). 베이스라인 그대로: 지출 $24.29 / 클릭 266 / 랜딩 249세션 /
scene_play 0. 광고그룹 1871949778453250은 스케줄 종료 상태 = **현재 집행 중 광고 없음**
→ $30/일 한도 초과 위험 없음(단, API로 확인은 못 했으므로 다음 루프 최우선 재확인).

### 배포 상태 확인 (루프 1 미검증 항목 해소)
루프 시작 시 HEAD가 detached였고 로컬 `origin/master` ref가 b2c8560에 멈춰 있어
"루프 1이 푸시 안 됨"으로 보였다. `git fetch` 후 실제 원격 확인 결과 **루프 1의 3개
커밋은 정상 푸시돼 있었다**(원격 master = 1313792). 로컬 ref만 낡았던 것.
0.69MB 페이로드 + head 최상단 계측은 이미 라이브에 반영된 상태.

### 결정과 근거
데이터 0·신규 세션 0이므로 규율상 큰 변수(가격·타겟·훅) 변경 금지.
광고 점화(백로그 4)는 API 차단으로 계속 보류. → **백로그 3) 재생/오디오 코드 검토**를 실행.

코드를 읽어보니 실제 버그가 있었다. `startPlay()`는 `au.play()`를 호출하기 **전에**
커버와 재생 버튼을 이미 숨긴다. 그래서 인앱 웹뷰(틱톡·인스타)가 자동재생 정책으로
`play()`를 reject하면 사용자는 **소리도 안 나고 다시 누를 버튼도 없는 죽은 화면**에 갇힌다.
게다가 `scene_play`는 성공 여부와 무관하게 찍혀서, 재생 실패가 성공으로 기록된다.
우리 트래픽 전량이 틱톡 인앱 웹뷰라는 걸 감안하면 이건 퍼널 최상단의 치명적 실패 모드.

### 실행 내용 (커밋 1개 `1f90d5c`, 되돌리기 쉬움)
- `playFailed()` 신설 — 실패 시 커버·재생 버튼 복구 + 재시도 힌트 노출(두 번째 탭은 대개 성공)
- 2초 워치독 — `play()`가 resolve됐는데도 재생이 전혀 진행 안 되는 웹뷰 케이스 포착
- audio `error` 이벤트도 같은 복구 경로로 통합
- **신규 `scene_audio_ok` 이벤트** — 실제 재생 진행(currentTime>0.15) 시 1회.
  이제 `scene_play`(=탭했다)와 실제로 소리를 들었다를 구분 가능
- 진행바 `au.duration` NaN 가드

실패 경로에서만 동작이 바뀐다(현재 그 경로는 막다른 길). 카피·가격·타겟 변경 없음.

### 검증 (헤드리스 모바일 Chromium, 틱톡 인앱 UA / 390×844)
A) 정상 재생, B) `play()`를 강제 reject시킨 웹뷰 시뮬레이션 — **11개 체크 전부 PASS**:
- A: `scene_play` → `scene_audio_ok` 발화, 오디오 실제 진행, 복구 UI 미노출, JS 에러 0
- B: `play_error{NotAllowedError}` 발화, `scene_audio_ok` 미발화(정확), 커버·버튼·힌트 복구, JS 에러 0

전체 퍼널 회귀도 확인(내가 `timeupdate`를 건드렸으므로):
`page_view → perf → first_tap → scene_play → scene_audio_ok → scene_complete → scroll →
quiz_done → teaser_view → offer_view`, JS 에러 0, 결제 링크 $49 그대로 살아있음.

**미검증:** 라이브 HTML 재확인은 egress 차단으로 여전히 불가(로컬 파일 실측으로 대체).
집행 중 광고가 없어 이 배포로 깨질 라이브 트래픽은 없음.

### 다음 루프 제안
1. **egress부터 확인.** 계속 막혀 있으면 광고·데이터는 전부 보류. 사용자 조치 필요 —
   환경 네트워크 정책에 위 3개 호스트 허용이 있어야 이 루프가 본래 일을 할 수 있다.
2. 뚫리면 즉시: ① TikTok 오늘 지출 확인(>$30이면 광고그룹 정지) ② `perf`/`leave`/
   `first_tap`/`play_error`/`scene_audio_ok` 판독 — 루프1·2 배포 이후 데이터만 유효
3. 그 다음 백로그 4) 라운드2 점화: 새 광고그룹(미국 / 25-54 / K-drama·Korean culture·
   language learning / 틱톡 지면만, Pangle 제외 / $30 일예산), 광고 1871949837237713 재사용
4. 판독 기준선: `scene_play`는 있는데 `scene_audio_ok`가 적으면 → 인앱 웹뷰 오디오 차단이
   진범(그럼 무음 자막 우선 모드 검토). `leave`의 sec가 3초 미만에 몰리면 → 로드/기대 불일치.
   offer_view는 나오는데 checkout_click이 0이면 → 그때 가격 $29 테스트.
5. 규율 유지: 새 실사용 세션 30개 미만이면 큰 변수 손대지 말 것.

---

## 2026-07-29 05:13 UTC — 루프 1

**⚠ 데이터 판독 실패: 이 세션의 외부 egress가 차단됨 (정책 403).**

- `pjvjweurelmosugwptdl.supabase.co` → CONNECT 403 (퍼널 데이터 읽기 불가)
- `business-api.tiktok.com` → CONNECT 403 (오늘 지출 확인 불가, 광고그룹 생성 불가)
- `next-epic-dev.github.io` → CONNECT 403 (라이브 HTML 검증 불가)
- git(origin)만 로컬 프록시로 통함 → 커밋·푸시는 가능

### 데이터 스냅샷
새 데이터 없음. 베이스라인(라운드1) 그대로 사용:
지출 $24.29 / 클릭 266 (CTR 5%, CPC $0.09) / 랜딩 249세션 / scene_play **0** / 이하 전 단계 0.

지출 확인은 불가했으나 **$30 한도 초과 위험 낮음** — 유일한 광고그룹 1871949778453250은
스케줄 종료 상태라 현재 집행 중인 광고가 없음. 다음 루프에서 최우선으로 재확인할 것.

### 결정과 근거
데이터를 못 읽고 새 실사용 세션도 0개이므로 규율상 큰 변수(가격·타겟·훅) 변경 금지.
백로그 1)·2) 위생 수리만 실행. 광고 라운드2 점화(백로그 4)는 API 차단으로 **보류**.

먼저 "scene_play가 정확히 0"이 사람 행동인지 버그인지부터 갈랐다.
헤드리스 모바일 Chromium(iPhone UA, 390×844)으로 실제 페이지를 돌려 확인:

- `startPlay`는 정상 함수, 커버 숨김 → 오디오 재생(18.68초) → 자막 싱크 → 6줄 렌더 정상
- 이벤트도 정상 발화: `page_view → scene_play → scene_complete → quiz_done → teaser_view → offer_view`
- JS 에러 0건, 오퍼의 스트라이프 링크 살아있음
- `SUPABASE_URL/KEY`는 68행 const 체인 끝에 정의돼 있어 track()도 정상

**결론: 페이지는 안 깨졌다.** scene_play 0의 진짜 용의자는 로드 이탈 + 계측 실명(失明):
`track('page_view')`가 3.87MB 중 약 97% 지점(라인 73)에 있어서, **다 받기 전에 나간 사람은
애초에 데이터에 안 찍힌다.** 즉 249 page_view는 "끝까지 받은 사람" 수이고, 그 앞단 이탈은
지금까지 통째로 안 보였다. 눈부터 뜨는 게 순서.

### 실행 내용 (커밋 2개, 되돌리기 쉬움)

1. `7a5405f` — 커버 이미지 인라인 PNG 압축
   - 원본은 768×1376인데 PNG로 3.36MB(base64)로 박혀 있었음 — 해상도 문제가 아니라 인코딩 낭비
   - 원본 해상도 유지, JPEG q=74 progressive → **182KB** (목표 200KB 이하 충족)
   - 어차피 어두운 그라디언트 오버레이 아래라 아티팩트 안 보임 (스크린샷 확인)
   - **페이지 총량 3.87MB → 0.69MB (-82%)**

2. `ac83d07` — 계측 수리
   - 추적 코어(SUPABASE 상수·DK_SID/PID/UTM·track·`page_view`)를 `<head>` 최상단으로 이동
     → 이제 3.3MB 페이로드 **이전에** page_view가 찍힘. 조기 이탈이 처음으로 보이게 됨
   - 신규 이벤트:
     - `perf` — {ttfb, dcl, load, kb} (Navigation Timing, load 시 1회)
     - `first_tap` — {ms, target} 첫 탭까지 걸린 시간
     - `scroll` — 10% 이상 첫 스크롤 시 {ms}
     - `leave` — pagehide/visibilitychange hidden 시 {sec 체류, max_scroll, played, stage}
     - `play_error` — `au.play()` promise reject + audio `error` 이벤트 (인앱브라우저 자동재생
       차단을 이걸로 잡는다 → 백로그 3의 절반 선반영)

### 검증
헤드리스 모바일 Chromium 실측 — 신·구 이벤트 전부 발화 확인, JS 에러 0:

```
page_view
perf {"ttfb":0,"dcl":32,"load":63,"kb":679}
first_tap {"ms":595,"target":"btn"}
scene_play
scene_complete
scroll {"ms":1555}
quiz_done {"correct":1,"total":3}
teaser_view
offer_view
leave {"sec":2,"max_scroll":100,"played":0,"stage":"stage-scene"}
```

**미검증 항목:** 라이브 HTML 재확인(배포 후 track 생존 확인)은 egress 차단으로 못 함.
로컬 파일 실행 검증으로 대체했고, Pages는 master 파일을 그대로 서빙하므로 위험은 낮다고 판단.
현재 집행 중인 광고가 없어 이 배포로 깨질 라이브 트래픽도 없음. **다음 루프에서 최우선 확인.**

### 다음 루프 제안
1. **egress 복구 여부부터 확인.** 막혀 있으면 광고·데이터 관련은 전부 보류하고 위생 수리만.
2. 뚫렸으면 즉시: ① TikTok `report/integrated/get`으로 오늘 지출 확인(> $30이면 광고그룹 정지)
   ② Supabase에서 `perf`/`leave`/`first_tap` 판독 — **이번 배포 이후 데이터만** 의미 있음
3. 그 다음 백로그 4) 라운드2 점화: 새 광고그룹(미국 / 25-54 / K-drama·Korean culture·language
   learning 관심사 / 틱톡 지면만, Pangle 제외 / $30 일예산), 광고 1871949837237713 재사용.
   0.69MB 페이지 + 조기 page_view가 깔린 상태라 이번엔 앞단 이탈이 처음으로 측정된다.
4. 판독 기준선: page_view 대비 scene_play 비율이 낮으면 → 첫 화면 훅 문제.
   `leave`의 sec가 3초 미만에 몰리면 → 여전히 로드/기대 불일치.
   offer_view는 나오는데 checkout_click이 0이면 → 그때 가격 $29 테스트.
5. 규율 유지: 새 실사용 세션 30개 미만이면 큰 변수 손대지 말 것.
