# NPU 패치 — openclaude 를 furio 클라이언트로

furio 의 화면은 [openclaude](https://github.com/Gitlawb/openclaude)(Claude Code 계열 TUI)다.
`openclaude-npu.patch` 는 거기에 NPU 연동을 더한 커밋 23개다.

- 상태줄에 모델별 LED — 라우터의 `/router/status` 를 폴링해 올라오는 중(노랑)/사용 가능(초록)을 보여준다
- `/model` 창에서 tp·dp·pp 를 따로 고른다(라우터가 주는 변형 목록 기준)
- 모델 설명을 라우터에서 받아 보여준다 — "tp8·2장·ctx 40k" 처럼 카드 점유가 보인다
- 백그라운드 호출(요약·제목 생성)이 실행 중 바꾼 모델을 따라간다 — 안 그러면 켤 때 모델을
  다시 올리느라 카드가 왕복한다
- `--max-turns` 를 대화 세션에도 걸리게 연결

기준 커밋: `4bb94d01e440d231bd5aa22cc0b9bae82f9bbc99` (Gitlawb/openclaude, 2026-07-22)

## 언제 필요한가

라우터를 쓰는 사용자는 **필요 없다**. 서버가 빌드해 둔 결과물을 설치기가 받아 간다.
직접 빌드가 필요한 경우는 두 가지다 — 서버 운영자가 클라이언트를 갱신할 때,
그리고 서버 없이 개인 PC 에서 NPU 기능까지 갖춘 클라이언트를 만들 때.

## 빌드

```bash
git clone https://github.com/Gitlawb/openclaude.git
cd openclaude
git checkout 4bb94d01e440d231bd5aa22cc0b9bae82f9bbc99
git am /path/to/furio-openclaude/openclaude-npu/openclaude-npu.patch   # 커밋 23개

bun install
bun run build          # → dist/cli.mjs, dist/sdk.mjs
```

빌드에는 [bun](https://bun.sh) 이 필요하다(openclaude 자체 빌드 도구).

## 적용

```bash
# 개인 PC — 설치기에 dist 를 직접 준다(서버 없이도 NPU 기능이 들어간다)
FURIO_CLIENT_DIST=$PWD/dist SDI_SERVER=http://127.0.0.1:8400 bash client/install.sh

# 서버 — 라우터가 이 dist 를 사용자에게 배포한다
FURIO_CLIENT_DIST=$PWD/dist bash server/serve-router.sh
```

설치기는 npm 에서 `@gitlawb/openclaude` 를 같은 버전으로 깐 뒤 `dist/cli.mjs`·`dist/sdk.mjs`
두 파일만 덮어쓴다. 덮어쓰기가 실패해도 설치는 성공하고, NPU 위젯만 빠진 상태로 동작한다.
