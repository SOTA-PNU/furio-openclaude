# NPU 패치 — openclaude 를 furio 클라이언트로

furio 의 화면은 [openclaude](https://github.com/Twigpine/openclaude)(Claude Code 계열 TUI)다.
`openclaude-npu.patch` 는 거기에 NPU 연동을 더한 커밋 31개다.

- 상태줄에 모델별 LED — 라우터의 `/router/status` 를 폴링해 올라오는 중(노랑)/사용 가능(초록)을 보여준다
- `/model` 창에서 tp·dp·pp 를 따로 고른다(라우터가 주는 변형 목록 기준)
- 모델 설명을 라우터에서 받아 보여준다 — "tp8·2장·ctx 40k" 처럼 카드 점유가 보인다
- 백그라운드 호출(요약·제목 생성)이 실행 중 바꾼 모델을 따라간다 — 안 그러면 켤 때 모델을
  다시 올리느라 카드가 왕복한다
- `/survey` 로 터미널 안에서 사용 경험 설문을 받는다 — 화살표로 고르고 Enter, 한 문항에
  한 화면. 응답은 먼저 설정 폴더에 적고 그다음 라우터로 보내므로 서버가 꺼져 있어도 답이
  사라지지 않는다. 보낼 곳은 이미 쓰고 있는 `OPENAI_BASE_URL` 에서 끌어내니 학생 쪽에
  설정할 것이 없다. 커밋이나 테스트가 통과한 턴 끝에 한 줄로만 권한다(하루 한 번)
- `furio agents` 로 이 컴퓨터에서 하던 세션 목록을 claude agents 와 같은 구성(프로젝트
  폴더별 묶음, 한 줄에 제목·요약·나이, 아래 "새 세션" 입력상자)으로 열고, 다른 폴더 세션도 그
  자리에서 이어 쓴다 — 고르면 그 폴더에서 furio 를 다시 띄우고 터미널을 넘긴다. 세션 안에서는
  빈 프롬프트에서 `←` 를 누르거나 `/switch` 로 같은 화면을 연다 — 대화 위에 따로 뜨는
  대체 화면(ctrl+o 전사와 같은 방식)이라 들어갔다 나와도 자리가 같고, `Esc`·`ctrl+c` 로
  닫으면 대화가 그대로 돌아온다. 세션의 설정·
  CLAUDE.md·신뢰 결정·MCP 서버는 시작할 때 그 폴더 기준으로 읽히니 중간에 디렉터리만
  바꿔서는 가져올 수 없다. 물려받은 피커는 현재 저장소 세션만 보여줬고, Ctrl+A 로 펼쳐
  다른 폴더 것을 고르면 재개하지 않고 `cd … && claude --resume <id>` 를 클립보드에 넣고
  끝났다(이름도 claude 로 틀려 있었다)
- 시작 화면과 환영 상자를 연구실 표식(SOTA / furio)으로 바꾸고, 시작 화면은 로고·한 줄 소개·
  버전만 남긴다 — Provider/Model/Endpoint 박스는 상태줄과 `/model` 에 이미 있는 내용이라 걷어냈다
- 로고를 글자 단위로 칠한다 — 아홉 글자(SOTA 4 + FURIO 5)가 청록에서 분홍까지 한 색씩
  나눠 가진다(`sota` 팔레트, 기본값). 물려받은 방식은 한 줄 안에서 색이 팔레트의 절반만큼
  움직여 같은 단어의 첫 글자와 끝 글자가 따로 놀았고, 색도 주황이라 Claude 쪽 표식과
  닮아 있었다. 글자 폭이 제각각이라(FURIO 의 I 만 3칸) 칸 너비를 그림 옆에 데이터로 둔다.
  업스트림 팔레트 5종은 그대로라 `/logo` 로 언제든 바꿀 수 있다

기준: 업스트림 태그 `v0.33.0` (Twigpine/openclaude, 2026-10-15)

업스트림이 `--max-turns` 를 대화 세션에 거는 기능을 직접 넣어서(#2086) 같은 일을 하던
우리 커밋은 뺐다. 업스트림 구현이 더 꼼꼼하다(인자 검사·경고·이월 예산 처리).

## 언제 필요한가

라우터를 쓰는 사용자는 **필요 없다**. 서버가 빌드해 둔 결과물을 설치기가 받아 간다.
직접 빌드가 필요한 경우는 두 가지다 — 서버 운영자가 클라이언트를 갱신할 때,
그리고 서버 없이 개인 PC 에서 NPU 기능까지 갖춘 클라이언트를 만들 때.

## 빌드

```bash
git clone https://github.com/Twigpine/openclaude.git
cd openclaude
git checkout v0.33.0
git am /path/to/furio-openclaude/openclaude-npu/openclaude-npu.patch   # 커밋 31개

bun install
bun run build          # → dist/cli.mjs, dist/sdk.mjs   (일반판 = lab, 설문 없음)
FURIO_EDITION=class FURIO_OUTDIR=./dist-class bun run build   # 수업판: /model·/provider 등 13개 명령 제거, /survey 켬
```

에디션은 번들에 박힌다(`scripts/build.ts` 의 `define`, `src/furio/edition.ts`) — 실행 때 환경변수로 못 바꾼다.
수업판 제거 목록은 `src/furio/edition.ts` 한 곳이다.

빌드에는 [bun](https://bun.sh) 이 필요하다(openclaude 자체 빌드 도구).

## 적용

```bash
# 개인 PC — 설치기에 dist 를 직접 준다(서버 없이도 NPU 기능이 들어간다)
FURIO_CLIENT_DIST=$PWD/dist SDI_SERVER=http://127.0.0.1:8400 bash client/install.sh

# 서버 — 라우터가 이 dist 를 사용자에게 배포한다
FURIO_CLIENT_DIST=$PWD/dist bash server/serve-router.sh
```

설치기는 npm 에서 `@twigpine/openclaude` 를 같은 버전으로 깐 뒤 `dist/cli.mjs`·`dist/sdk.mjs`
두 파일만 덮어쓴다. 덮어쓰기가 실패해도 설치는 성공하고, NPU 위젯만 빠진 상태로 동작한다.
