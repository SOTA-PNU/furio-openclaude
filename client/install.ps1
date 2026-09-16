# ───────────────────────────────────────────────────────────────────────────
# furio 설치기 (Windows PowerShell) — Claude Code 같은 코딩 에이전트(openclaude) + 서버 NPU
#
# 사용 (PowerShell). SDI_SERVER = 라우터 주소:
#   원격(집/외부): 먼저 SSH 터널 후 http://127.0.0.1:8400 (외부 입구는 SSH 10022뿐)
#       터미널①:  ssh -p 10022 -N -L 8400:localhost:8400 jun@164.125.19.138
#       터미널②:  $env:SDI_SERVER="http://127.0.0.1:8400"   # 인증ON이면 $env:SDI_API_KEY="<키>"
#   사내 LAN: $env:SDI_SERVER="http://10.125.19.138:8400"
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# 설치 후:  furio            # Claude 같은 코딩 에이전트 TUI
#           furio -p "..."   # 비대화형 한 줄
# ───────────────────────────────────────────────────────────────────────────
$ErrorActionPreference = "Stop"
if (-not $env:SDI_SERVER) { throw "SDI_SERVER 를 지정하세요 (예: http://127.0.0.1:8400)" }
$server = $env:SDI_SERVER.TrimEnd('/')
$key    = if ($env:SDI_API_KEY) { $env:SDI_API_KEY } else { '' }
if ($key -and ($key -notmatch '^[A-Za-z0-9._-]+$')) { throw "SDI_API_KEY 에 허용 안 되는 문자(영숫자와 . _ - 만)" }
$cmd    = if ($env:FURIO_CMD)        { $env:FURIO_CMD }        else { 'furio' }
$model  = if ($env:FURIO_MODEL)      { $env:FURIO_MODEL }      else { 'gpt-oss-120b' }
$maxout = if ($env:FURIO_MAX_OUTPUT) { $env:FURIO_MAX_OUTPUT } else { '8192' }
$auto   = if ($env:FURIO_AUTO)       { $env:FURIO_AUTO }       else { '' }   # 완전자동 기본값(빈값=확인모드)
$HomeDir = Join-Path $env:USERPROFILE ".$cmd"
$BinDir  = Join-Path $HomeDir "bin"
New-Item -ItemType Directory -Force -Path $HomeDir,$BinDir | Out-Null

Write-Host "[1/4] Node >=22 확인 (openclaude 요구)"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "node 없음 — Node >=22 설치 후 재실행 (https://nodejs.org)" }
$nodeMajor = [int](node -p "process.versions.node.split('.')[0]")
if ($nodeMajor -lt 22) { throw "node $(node -v) — openclaude 는 Node >=22 필요. 최신 Node 설치 후 재실행." }

Write-Host "[2/4] openclaude 설치 (격리 prefix: $HomeDir)"
npm install -g '@gitlawb/openclaude@latest' --prefix $HomeDir | Out-Null
$ocBin = Join-Path $HomeDir "openclaude.cmd"     # Windows npm --prefix 는 prefix 루트에 .cmd 생성
if (-not (Test-Path $ocBin)) { $ocBin = Join-Path $BinDir "openclaude.cmd" }
if (-not (Test-Path $ocBin)) { throw "openclaude 바이너리 없음 ($HomeDir)" }

Write-Host "[3/4] 서버 도달 확인: $server"
$headers = if ($key) { @{ Authorization = "Bearer $key" } } else { @{} }
try { Invoke-RestMethod -Uri "$server/v1/models" -Headers $headers -TimeoutSec 10 | Out-Null; Write-Host "      [ok] /v1/models 응답" }
catch { Write-Host "[fail] 서버 도달 실패 $server/v1/models — SSH 터널이 떠 있나요? (ssh -p 10022 -N -L 8400:localhost:8400 ...)"; exit 1 }

# 키는 파일에(있을 때) + 사용자 전용 잠금. 래퍼 텍스트엔 비밀 없음.
$keyFile = Join-Path $HomeDir "key"
if ($key) { [System.IO.File]::WriteAllText($keyFile, $key, (New-Object System.Text.ASCIIEncoding)); icacls $keyFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null }
elseif (Test-Path $keyFile) { Remove-Item $keyFile }

# 모델별 '진짜' 컨텍스트 창을 라우터에서 받아 ctx.json 저장(단일 출처 = 서버 REGISTRY).
# ⚠️ 없으면 openclaude 는 처음 보는 우리 모델 id 를 전부 128000 으로 가정한다(실제 40960~262144).
$ctxFile = Join-Path $HomeDir "ctx.json"
try {
  $rm = Invoke-RestMethod -Uri "$server/router/models" -Headers $headers -TimeoutSec 10
  $map = @{}
  foreach ($m in $rm.data) { if ($m.id -and $m.context) { $map[$m.id] = [int]$m.context } }
  if ($map.Count -gt 0) {
    [System.IO.File]::WriteAllText($ctxFile, ($map | ConvertTo-Json -Compress), (New-Object System.Text.ASCIIEncoding))
    Write-Host "      [ok] 모델별 컨텍스트 $($map.Count)개 기록 (ctx.json)"
  } else { if (Test-Path $ctxFile) { Remove-Item $ctxFile }; Write-Host "      [warn] /router/models 비어있음 — 모델별 ctx 미설정" }
} catch {
  if (Test-Path $ctxFile) { Remove-Item $ctxFile }
  Write-Host "      [warn] /router/models 응답 없음 — 모델별 ctx 미설정(openclaude 가 128000 으로 가정할 수 있음)"
}

# 안전 자동모드(FURIO_AUTO=safe)용 규칙 파일. 이미 있으면 사용자가 고친 것이므로 건드리지 않는다.
# ⚠️ Windows 는 규칙을 .cmd 에 '구워' 넣으므로, 규칙을 고친 뒤에는 install.ps1 을 다시 실행해야 반영됩니다.
$allowFile = Join-Path $HomeDir "auto-allow.txt"
$denyFile  = Join-Path $HomeDir "auto-deny.txt"
if (-not (Test-Path $allowFile)) {
  @'
# 자동 승인(묻지 않음) — 읽기/조회/테스트처럼 되돌릴 수 있는 것만.
# 문법: 도구이름 또는 도구이름(명령 접두사:*)   예) Bash(git status:*)
Read
Glob
Grep
TodoWrite
Bash(ls:*)
Bash(pwd:*)
Bash(cat:*)
Bash(head:*)
Bash(tail:*)
Bash(wc:*)
Bash(file:*)
Bash(stat:*)
Bash(du:*)
Bash(df:*)
Bash(tree:*)
Bash(date:*)
Bash(echo:*)
Bash(which:*)
Bash(find:*)
Bash(grep:*)
Bash(rg:*)
Bash(diff:*)
Bash(git status:*)
Bash(git diff:*)
Bash(git log:*)
Bash(git show:*)
Bash(git branch:*)
Bash(git remote:*)
Bash(npm test:*)
Bash(npm run:*)
Bash(pytest:*)
Bash(make:*)
Bash(cargo test:*)
Bash(cargo build:*)
Bash(go test:*)
Bash(go build:*)
'@ | Set-Content -Path $allowFile -Encoding ASCII
}
if (-not (Test-Path $denyFile)) {
  @'
# 차단(묻지도 않고 거부) — 파괴적이거나 되돌릴 수 없거나 밖으로 나가는 것.
Bash(rm -rf:*)
Bash(rm -fr:*)
Bash(sudo:*)
Bash(su:*)
Bash(dd:*)
Bash(mkfs:*)
Bash(fdisk:*)
Bash(parted:*)
Bash(shutdown:*)
Bash(reboot:*)
Bash(halt:*)
Bash(poweroff:*)
Bash(chown:*)
Bash(chmod 777:*)
Bash(kill:*)
Bash(pkill:*)
Bash(killall:*)
Bash(curl:*)
Bash(wget:*)
Bash(git push:*)
Bash(git reset --hard:*)
Bash(git clean:*)
Bash(crontab:*)
Bash(npm publish:*)
Bash(ssh:*)
Bash(scp:*)
'@ | Set-Content -Path $denyFile -Encoding ASCII
}
# 규칙을 콤마로 이어 붙인다(openclaude 파서가 괄호를 인식해 괄호 안 공백/콤마는 안전).
function Get-Rules([string]$p) {
  if (-not (Test-Path $p)) { return '' }
  ($(Get-Content $p | Where-Object { $_.Trim() -ne '' -and -not $_.TrimStart().StartsWith('#') }) -join ',')
}
$allowJoined = Get-Rules $allowFile
$denyJoined  = Get-Rules $denyFile

Write-Host "[4/4] '$cmd' 명령 설치: $BinDir\$cmd.cmd"
$lines = @(
  '@echo off',
  'set "CLAUDE_CODE_USE_OPENAI=1"',
  "set `"OPENAI_BASE_URL=$server/v1`"",
  "if not defined OPENAI_MODEL set `"OPENAI_MODEL=$model`"",
  "if not defined CLAUDE_CODE_MAX_OUTPUT_TOKENS set `"CLAUDE_CODE_MAX_OUTPUT_TOKENS=$maxout`"",
  "set `"OPENCLAUDE_CONFIG_DIR=$HomeDir\config`"",
  'set "OPENAI_API_KEY=dummy"',
  "if exist `"$keyFile`" set /p OPENAI_API_KEY=<`"$keyFile`"",
  "if not defined CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS if exist `"$ctxFile`" set /p CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS=<`"$ctxFile`"",
  'if not defined API_TIMEOUT_MS set "API_TIMEOUT_MS=900000"',
  'if not defined CLAUDE_STREAM_IDLE_TIMEOUT_MS set "CLAUDE_STREAM_IDLE_TIMEOUT_MS=600000"',
  "if not defined FURIO_AUTO set `"FURIO_AUTO=$auto`"",
  'set "AUTO_ARGS="',
  'if /I "%FURIO_AUTO%"=="1" set "AUTO_ARGS=--dangerously-skip-permissions"',
  'if /I "%FURIO_AUTO%"=="yes" set "AUTO_ARGS=--dangerously-skip-permissions"',
  'if /I "%FURIO_AUTO%"=="on" set "AUTO_ARGS=--dangerously-skip-permissions"',
  'if /I "%FURIO_AUTO%"=="full" set "AUTO_ARGS=--dangerously-skip-permissions"',
  'if /I "%FURIO_AUTO%"=="bypass" set "AUTO_ARGS=--dangerously-skip-permissions"',
  'if /I "%FURIO_AUTO%"=="edits" set "AUTO_ARGS=--permission-mode acceptEdits"',
  'if /I "%FURIO_AUTO%"=="accept" set "AUTO_ARGS=--permission-mode acceptEdits"',
  "if /I `"%FURIO_AUTO%`"==`"safe`" set `"AUTO_ARGS=--permission-mode acceptEdits --allowed-tools $allowJoined --disallowed-tools $denyJoined`"",
  "if /I `"%FURIO_AUTO%`"==`"rules`" set `"AUTO_ARGS=--permission-mode acceptEdits --allowed-tools $allowJoined --disallowed-tools $denyJoined`"",
  'set "TOOL_ARGS="',
  'if defined FURIO_TOOLS set "TOOL_ARGS=--tools %FURIO_TOOLS%"',
  "`"$ocBin`" %AUTO_ARGS% %TOOL_ARGS% %*"
) -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $BinDir "$cmd.cmd"), $lines + "`r`n", (New-Object System.Text.ASCIIEncoding))

# PATH 에 $BinDir 추가 (사용자 환경변수, 세그먼트 단위)
$userPath = [Environment]::GetEnvironmentVariable("Path","User")
$segments = @($userPath -split ';' | Where-Object { $_ -ne '' })
if ($segments -notcontains $BinDir) {
  [Environment]::SetEnvironmentVariable("Path", (@($BinDir) + $segments -join ';'), "User")
  Write-Host "   ⚠️ 새 터미널을 열어야 PATH 가 반영됩니다."
}
Write-Host ""
Write-Host "[OK] 설치 완료. (openclaude: $HomeDir, 명령: $BinDir\$cmd.cmd)"
Write-Host "     실행:  $cmd   /   $cmd -p ""..."" /   $cmd --model gpt-oss-120b"
Write-Host "     ⚠️ 작업은 일반 프로젝트 폴더에서(.claude 등 민감 경로엔 쓰기 차단). 터널 방식이면 furio 쓰는 동안 터널 유지."
Write-Host "     제거:  Remove-Item -Recurse -Force ""$HomeDir""; Remove-Item ""$BinDir\$cmd.cmd"""
