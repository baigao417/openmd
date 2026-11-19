<div align="center">

# Openmd

**Everything to Markdown — just beginning.**

**보이지만 꺼낼 수 없던 콘텐츠를, 에이전트가 쓸 수 있는 Markdown으로.**

[![PyPI](https://img.shields.io/pypi/v/openmd-cli?color=3b5bdb)](https://pypi.org/project/openmd-cli/)
[![Python](https://img.shields.io/pypi/pyversions/openmd-cli)](https://pypi.org/project/openmd-cli/)
[![CI](https://github.com/baigao417/openmd/actions/workflows/ci.yml/badge.svg)](https://github.com/baigao417/openmd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/baigao417/openmd/blob/main/LICENSE)

[English](https://github.com/baigao417/openmd/blob/main/README.md) · [简体中文](https://github.com/baigao417/openmd/blob/main/README.zh-CN.md) · [日本語](https://github.com/baigao417/openmd/blob/main/README.ja.md) · 한국어

</div>

AI 시대에 모델은 점점 똑똑해지고 있습니다. 하지만 모델이 읽을 수 있는 것은 여전히 여러분이 무엇을 건네줄 수 있느냐에 달려 있습니다.

정말 가치 있는 정보의 상당수는 각 플랫폼 안에 갇혀 있습니다. 로그인해야 볼 수 있는 글, 영상 속 발언, 팟캐스트 대담, AI와 오후 내내 나눈 대화, 단체 채팅방의 토론. 볼 수는 있지만 꺼낼 수는 없습니다.

Openmd가 지향하는 것은 하나의 주춧돌입니다. 이런 콘텐츠를 개방적이고 가지고 다닐 수 있으며 **사람과 에이전트 모두가 바로 읽고 활용할 수 있는** Markdown으로 바꿉니다. 웹 글, 영상 플랫폼, 오피스 문서, 채팅 대화까지, 그 모든 것이 여러분만의 전문 지식 베이스의 재료가 될 수 있습니다.

우리는 무엇보다 AI 네이티브 환경을 염두에 둡니다. 원문은 고치지 않고 그대로 두고, 출처와 완전성을 밝히는 영수증을 함께 남기며, 오류 페이지와 로그인 벽은 단호히 거부합니다. 에이전트에게는 "이 자료를 믿을 수 있는가"가 "이 자료가 있는가"만큼 중요하기 때문입니다.

모든 것을 Markdown으로. 이제 막 시작입니다.

---

## 빠른 시작

Openmd는 명령줄 도구와 Python 모듈 `openmd`를 제공합니다(패키지 이름은 `openmd-cli`). Python 3.10 이상이 필요하며 Windows, macOS, Linux에서 동작합니다.

```bash
# 기본 패키지 설치 (pipx install openmd-cli / uv tool install openmd-cli 도 가능)
pip install openmd-cli

# 로컬 실행 환경과 선택 구성 요소 상태 확인
openmd doctor

# 링크를 붙여 넣고 Enter, 저장 위치 선택
openmd

# 웹 페이지 하나를 지정한 폴더에 바로 저장
openmd "https://example.com/article" -o ./notes

# 에이전트·스크립트용: 묻지 않고 실행하며, 기계가 읽을 수 있는 JSON 영수증 출력
openmd "https://example.com/article" -o ./notes --json

# 링크 목록(한 줄에 링크 하나) 일괄 처리. batch 명령은 --out 으로 폴더 지정
openmd batch links.txt --out ./notes
```

각 링크는 선택한 폴더에 `제목.md`로 저장됩니다. 실행 로그는 `~/.openmd/runs/`에 기록되며 노트에 섞이지 않습니다.

로컬 오피스 문서를 처리하거나 로컬에서 음성·영상을 전사하려면 필요에 따라 추가 의존성을 설치하세요.

```bash
# 문서와 이미지 분석
pip install "openmd-cli[document]"

# 이미지와 스캔 문서 OCR
pip install "openmd-cli[ocr]"

# 선택적 Pandoc 변환 폴백
pip install "openmd-cli[pandoc]"

# 로컬 음성 인식(ASR)으로 음성·영상 전사
pip install "openmd-cli[asr]"
```

> 참고: 문서 분석, OCR, 로컬 ASR에는 Python 패키지 외에 FFmpeg나 로컬 모델이 필요할 수 있습니다. 준비 상태는 `openmd doctor`로 확인할 수 있습니다.

Openmd를 여러분의 에이전트나 자동화 도구 체인에 연결하려면 [도구 연동 가이드](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)를 참고하세요.

## 에이전트용 MCP

셸을 사용할 수 있으면 CLI + SKILL.md를, 도구 검색과 형식이 지정된 인수 및 클라이언트의 도구별 승인이 필요하면 MCP를 사용하세요. 선택 extra를 설치한 뒤 stdio 서버를 시작합니다. Claude Code:

```bash
pip install "openmd-cli[mcp]"
claude mcp add --transport stdio openmd -- openmd mcp
```

사전 설치가 필요 없는 `uvx` 한 줄 설정:

```bash
uvx --from "openmd-cli[mcp]" openmd mcp
```

Cursor: **Cursor Settings → MCP → Add new MCP Server**에서 같은 stdio 서버를 추가하세요:

```json
{
  "mcpServers": {
    "openmd": {"command": "openmd", "args": ["mcp"]}
  }
}
```

Windows: 실행 파일은 절대 경로를 사용하고 필요하면 UTF-8 출력을 지정하세요:

```json
{
  "mcpServers": {
    "openmd": {"command": "C:\\path\\to\\openmd.exe", "args": ["mcp"], "env": {"PYTHONIOENCODING": "utf-8"}}
  }
}
```

Claude Desktop: MCP 설정에 다음을 추가하세요. 필요한 경우 실행 파일을 절대 경로로 지정하세요.

```json
{
  "mcpServers": {
    "openmd": {"command": "openmd", "args": ["mcp"]}
  }
}
```

도구는 `openmd_save`, `openmd_save_page`, `openmd_job`, `openmd_doctor` 네 개입니다. `openmd_save`는 하나 또는 최대 20개의 `sources`를 받고, `openmd_job(action="cancel")`로 작업을 취소할 수 있습니다. `progressToken`을 보내는 클라이언트에는 진행률을 알리며 폴링도 사용할 수 있습니다. 완료된 Markdown은 `openmd://jobs` 및 `openmd://job/{job_id}` resources로 노출되고 읽기 크기는 제한됩니다. 출력 폴더는 절대 경로로 명시해야 합니다. HTML 스냅샷은 문자열 또는 절대 파일 경로를 받으며 UTF-8 기준 최대 2 MiB입니다. 다섯 권한은 기본적으로 꺼져 있습니다. `network`, `browser`, `media`, `asr`, `cloud`를 명시적으로 허용하세요. CLI의 권한 플래그를 재사용하지만 MCP는 출력 폴더에서 일상 CLI의 자동 권한을 추론하지 않고 저장 위치도 기억하지 않습니다. stdio는 로컬 프로세스를 시작할 수 있는 클라이언트용입니다. 원격 connector에는 별도의 전송 브리지가 필요합니다.

---

## AI와 지식 베이스의 입구로 적합한 이유

Openmd 자체는 지식 베이스나 RAG 검색 시스템이 아닙니다. "정보를 꺼내고 정리하는" 기초 단계에 집중합니다.

1. **원문을 보존하고, 해석은 다음 단계에 맡깁니다**
   기본적으로 원문, 자막, 전사본을 온전히 보존하고 문단과 공백만 정리합니다. 요약, 번역, 고쳐 쓰기는 자동으로 하지 않습니다. 사람은 원래 표현을 확인할 수 있고, AI 컨텍스트에 들어가기 전에 중요한 세부 사항이 사라지지 않습니다.

2. **출처, 메타데이터, 타임스탬프 포함**
   생성되는 Markdown에는 제목, 작성자, 원본 링크 같은 메타데이터를 담을 수 있습니다(출처가 실제로 제공하는 범위 안에서). 음성·영상 전사본에는 타임스탬프와 출처 및 완전성 설명이 붙습니다. 하위 도구를 위해 선택적으로 구조화된 패키지도 출력할 수 있습니다.

3. **잘못된 페이지는 저장하지 않고 거부합니다**
   알려진 오류 페이지나 로그인 벽을 만나면 Openmd는 이를 거부하고 영수증으로 알려 줍니다. 깨진 페이지를 멀쩡해 보이는 노트로 조용히 저장하지 않습니다(웹 페이지의 형태가 다양해 모든 비정상 페이지를 잡아낸다고 약속할 수는 없습니다).

---

## 현재 지원 범위

| 분류 | 출처 | 상태 | 설명 |
| :--- | :--- | :--- | :--- |
| **웹과 커뮤니티** | 일반 웹 글 | 사용 가능 | 설치 후 바로 사용 |
| | WeChat 공식 계정 글, X 게시물 | 사용 가능 | Openmd 브라우저 확장 프로그램과 이미 가진 접근 권한이 필요 |
| | 현재 페이지 저장, 로그인 알림 | 사용 가능 | 브라우저 확장 프로그램 필요 |
| **음성·영상과 전사본** | YouTube(자막 있음), 샤오위저우(Xiaoyuzhou) | 사용 가능 | 샤오위저우는 처음 사용할 때 약 20 MB의 읽기 구성 요소를 자동으로 준비합니다 |
| | bilibili, 샤오어퉁(Xiaoe-tech) | 사용 가능 | 브라우저 확장 프로그램 필요. bilibili AI 자막은 로그인도 필요 |
| | 자막 없는 음성·영상, 로컬 미디어 | 추가 의존성 필요 | `[asr]` 의존성, FFmpeg, 로컬 음성 모델 필요 |
| **로컬 문서와 이미지** | PDF, DOCX, PPTX, XLSX, 이미지 | 추가 의존성 필요 | `[document]` 의존성 필요. 복잡한 레이아웃과 스캔 문서는 추가 OCR 경로를 거칠 수 있습니다 |
| **업무 문서** | Feishu / Lark 문서 | 일부 지원 | 로컬에 `lark-cli`를 설치하고 로그인한 뒤, 명령에 `--enable-network-backends`를 붙여 네트워크 읽기를 명시적으로 허용해야 합니다 |
| **AI 대화** | Doubao, Gemini, Qwen, Yuanbao, Grok, DeepSeek(열려 있는 대화 페이지) | 일부 지원 | 브라우저 확장 필요: 대화 페이지에서 클릭하면 이미 로드된 내용을 저장하고, 에이전트는 `--as-snapshot`으로 넘길 수 있습니다. 현재 보이는 분기만 포함하며 사고 과정은 기본 생략 |
| | ChatGPT 대화 페이지; ChatGPT / Claude 공식 내보내기 ZIP 또는 JSON | 실험적 | 사용자 질문이 표시되지 않는 ChatGPT 페이지는 partial로 저장; 공식 내보내기는 브라우저 없이 로컬에서 가져옵니다 |
| **채팅 기록** | QQ 그룹 내보내기 파일(JSON / HTML / TXT) | 일시 중지 | 내보내기 파일 분석은 구현되어 있지만 아직 안정 기능으로 공개하지 않았습니다. Openmd는 채팅 데이터베이스를 읽거나 복호화하지 않습니다 |
| **실험적 지원** | 더우인(Douyin), 샤오훙수(Xiaohongshu) | 실험적 | 브라우저 확장 프로그램과 로그인 필요. 이 사이트들은 자동 읽기를 자주 차단하므로 안정성과 전체 수집을 약속하지 않습니다 |

### 로컬 문서 형식

`[document]`는 로컬 문서 분석의 기본 의존성입니다. 다음 표는 새로 설치한 환경에서 측정했으며 원시 텍스트 상태를 partial까지 포함해 보여 줍니다. 이미지와 스캔은 `[ocr]`, 선택적 변환 폴백은 `[pandoc]`를 설치하세요. 자세한 내용은 [형식 체인과 제한](https://github.com/baigao417/openmd/blob/main/docs/subsystems/document-formats.generated.md)을 참고하세요.

| 형식 | `[document]` 상태 | 선택 사항 / 제한 |
|---|---|---|
| 기본 텍스트가 있는 PDF | 사용 가능 | 스캔은 로컬 OCR 필요; 표 구조는 인증하지 않음 |
| DOCX | 일부 지원 | 텍스트는 얻지만 모든 샘플의 표 구조를 인증하지 않음 |
| PPTX, XLSX, XLS | 사용 가능 | 포함 이미지, 노트, 수식 캐시, 바이너리 이미지 제한을 보고함 |
| HTML, HTM, EPUB, RTF, ODT, ODS, ODP, FB2 | 사용 가능 또는 일부 지원 | 로컬 파일; 스크립트는 실행하지 않으며 복잡한 레이아웃은 partial일 수 있음 |
| CSV, TSV, JSON, XML, IPYNB, MSG | 사용 가능 | 큰 표는 잘릴 수 있음; 노트북은 실행하지 않으며 MSG 첨부는 누락으로 보고함 |
| DOC, PPT | 부분 텍스트 폴백 | 권장 변환에는 LibreOffice 필요; 레이아웃과 이전 바이너리 PPT 활성 슬라이드 순서는 인증하지 않음 |
| ZIP | 항목별 상태로 일부 지원 | 혼합 미디어는 별도 ASR/OCR 필요; 아카이브 자원 제한 적용 |
| MOBI, AZW3 | Calibre 필요 | 도구가 없으면 `engine_unavailable`; 암호화 전자책은 거부하고 복호화하지 않음 |

우클릭 변환은 `openmd shell-menu install`로 현재 사용자에게 설치합니다. 지원 문서 옆에 `.md`를 만들고, 폴더는 하위 폴더 구조를 유지하는 `<folder>-md` 미러로 변환합니다. 다시 실행하면 변경되지 않은 입력은 건너뛰고 사용자가 편집한 Markdown을 보호합니다. 스캔 PDF에는 로컬 OCR이 필요합니다. Windows 11에서는 기존 HKCU 메뉴가 '더 많은 옵션 표시' 아래에 나타날 수 있습니다. 이 경량 설치 프로그램은 패키지 앱이 필요한 최상위 메뉴 확장을 등록하지 않습니다. `openmd shell-menu uninstall`로 제거합니다.

여러분의 컴퓨터에서 무엇이 준비되었는지는 `openmd doctor`로 확인할 수 있습니다. 링크 처리에 실패하면 Openmd가 원인과 함께, [버그 리포트](https://github.com/baigao417/openmd/blob/main/.github/ISSUE_TEMPLATE/link-failure.yml)에 그대로 붙여 넣을 수 있는 한 줄을 출력합니다.

---

## 출력 예시

```markdown
---
title: "독서등을 어떻게 만들었나"
author: "Example Studio"
url: "https://www.bilibili.com/video/BV..."
published: "2026-05-24"
platform: "bilibili"
---

# 독서등을 어떻게 만들었나

**00:00:00**

오늘은 우리가 석 달 동안 만든 이 램프와 처음 두 버전이 왜 실패했는지 보여 드리려고 합니다

**00:00:31**

첫 시제품은 밤에 너무 밝아서 갓 아래에 두 번째 광원을 추가했습니다

*来源：自动字幕；识别错误保持原样。*
```

- 맨 앞은 YAML front matter로, Obsidian이나 Logseq 같은 노트 앱과 스크립트가 바로 읽을 수 있습니다.
- 전사본은 약 30초, 약 450자, 또는 자연스러운 쉼마다 문단으로 나뉘고, 각 문단은 타임스탬프로 시작합니다. 사람은 빠르게 훑어볼 수 있고, 에이전트는 특정 순간을 인용할 수 있습니다.
- 마지막 줄에는 텍스트의 출처(원문, 플랫폼 자막, 자동 자막, 음성 인식 등)와 내용의 완전성 여부가 적힙니다. 이 라벨은 현재 중국어로만 제공됩니다.

---

## 브라우저 확장 프로그램과 로컬 실행

- **이미 가진 브라우저 접근 권한을 재사용**: WeChat 공식 계정, X, bilibili 등 일부 경로는 Openmd 브라우저 확장 프로그램을 통해 동작하며, 일부 콘텐츠는 브라우저에서 로그인되어 있어야 합니다. 확장 프로그램은 현재 **Windows의 Chrome / Edge**를 지원합니다. Openmd는 여러분이 정당하게 가진 접근 권한만 사용하며, 로그인 제한, 유료 장벽, DRM 및 기타 접근 제어의 우회를 약속하지 않습니다.
- **로컬 우선과 네트워크 경계**: Openmd는 로컬에서 동작하지만, 온라인 콘텐츠를 읽을 때는 반드시 원래 사이트에 접속합니다. 일부 구성 요소는 처음 사용할 때 다운로드될 수 있습니다. 클라우드 서비스가 필요한 기능은 여러분이 명시적으로 켜야만 동작합니다.

확장 프로그램 설치:

1. `openmd browser-setup`을 실행합니다. 로컬 연결을 등록하고 확장 프로그램 폴더를 엽니다.
2. `chrome://extensions`(또는 `edge://extensions`)를 열고 **개발자 모드**를 켠 뒤 **압축해제된 확장 프로그램을 로드합니다**를 클릭하고 그 폴더를 선택합니다.
3. 아무 페이지에서나 Openmd 아이콘을 클릭하면 저장됩니다. 여러 탭을 선택하면 한 번에 모두 저장할 수 있고, 텍스트를 선택한 뒤 마우스 오른쪽 버튼을 누르면 그 안의 모든 링크를 저장할 수 있습니다.

[Releases](https://github.com/baigao417/openmd/releases)에서 확장 프로그램 ZIP을 내려받아 압축을 풀고, `openmd browser-setup`을 실행한 뒤 그 폴더를 로드할 수도 있습니다.

- **기본 브라우저 동작.** 팝업 없이 기존 창의 비활성 백그라운드 탭에서 읽습니다. Openmd가 소유한 탭은 기본적으로 음소거되고 작업이 끝나면 자동으로 닫힙니다.

Chrome에 "Openmd가 이 브라우저 디버깅을 시작했습니다"라는 브라우저 알림 표시줄이 나타날 수 있습니다. 이 알림은 확장 프로그램이 숨길 수 없습니다.

---

## 관리되는 OpenCLI

OpenCLI는 Openmd가 관리하는 브라우저·플랫폼 구성 요소입니다. `openmd opencli list`와
`openmd opencli <site> <command> [args]`는 읽기 전용 명령을 전달 실행하고 출력과 종료 코드를 보존합니다.
쓰기와 알 수 없는 명령은 `--allow-write`를 명시하지 않으면 fail closed입니다. 자격 증명 내용은 출력하지 않습니다.

---

## 설정

| 환경 변수 | 효과 |
|---|---|
| `OPENMD_AUTO_INSTALL_TOOLS=0` | 브라우저 읽기 구성 요소를 자동으로 다운로드하지 않음 |
| `OPENMD_NPM_REGISTRY` | 처음 다운로드할 때 사용할 npm 미러 |
| `OPENMD_SHOW_BROWSER=1` | 문제 해결을 위해 백그라운드 브라우저 창을 계속 보이게 함 |
| `OPENMD_DATA_DIR` | Openmd가 설정, 로그, 도구를 저장하는 위치(기본값 `~/.openmd`) |

더 보기: [도구 연동](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md), [현재 경로](https://github.com/baigao417/openmd/blob/main/docs/routes.generated.md), [아키텍처 맵](https://github.com/baigao417/openmd/blob/main/docs/architecture-map.html).

---

## 앞으로의 방향

**Everything to Markdown**은 장기적인 비전입니다. 아래는 아직 제공되지 않았으며 탐색 중인 방향입니다.

- **웹 AI 대화 내보내기**는 이미 열려 있는 대화 페이지를 읽습니다(확장 클릭, 또는 에이전트 자체 브라우저에서 `--as-snapshot`). 백그라운드에서 링크를 다시 열지 않습니다. 현재 일부 지원이며 로드된 현재 보이는 분기만 저장합니다. ChatGPT / Claude 공식 내보내기는 `openmd go export.zip -o archive --chatgpt-export` / `--claude-export`로 오프라인에서 가져올 수 있습니다(JSON 및 형식 자동 감지 지원). Claude는 `--claude-title`, `--claude-since`, `--claude-until`로 필터링하며, 텍스트가 아닌 블록과 첨부 파일은 자리표시자와 개수만 보존합니다. 반복 가져오기는 사용자 편집을 보호합니다.
- **전자책과 더 많은 텍스트 형식**: EPUB, RTF, MOBI, AZW3 등 지원.
- **더 넓은 문서 분석**: 복잡한 레이아웃 문서의 추출 품질을 꾸준히 높입니다.

이 항목들은 실제 샘플로 검증되기 전까지 제공된 기능으로 보지 않습니다.

---

## 자주 묻는 질문

**Openmd가 텍스트를 요약하거나 다듬나요?**
아니요. 공백과 문단 나눔만 조정합니다. 요약, 번역, 다듬기는 다음에 사용하는 도구에 맡깁니다.

**제 데이터는 어디로 가나요?**
어디로도 가지 않습니다. Openmd는 로컬에서 동작하고, 여러분의 네트워크와 브라우저로 페이지를 읽으며, 선택한 폴더에 파일을 씁니다. 쿠키나 비밀번호는 절대 저장하지 않습니다.

**왜 패키지 이름이 `openmd-cli`인가요?**
PyPI의 `openmd`라는 이름은 관련 없는 프로젝트가 쓰고 있습니다. 설치 후 명령어는 그대로 `openmd`입니다.

**링크가 처리되지 않습니다.**
`--verbose`를 붙여 다시 실행하면 어떤 읽기 경로를 시도했는지 볼 수 있습니다. 그다음 Openmd가 출력한 한 줄을 첨부해 [링크 실패 리포트](https://github.com/baigao417/openmd/issues/new?template=link-failure.yml)를 보내 주세요. 실패한 실제 링크는 가장 쓸모 있는 기여입니다. 이미 열어 둔 페이지(예: AI 대화)가 잘못 저장됐다면 `openmd report feedback <작업 ID> -o <디렉터리>`로 익명화된 문제 패키지(페이지 구조만, 본문은 합성 텍스트)를 만들어 확인한 뒤 함께 첨부해 주세요.

---

## 기여하기

실제 링크가 포함된 버그 리포트, 새 플랫폼 연동, 수정 모두 환영합니다. 대부분의 새 플랫폼은 작은 JSON "도구 카드" 하나면 충분합니다. 자세한 내용은 [플랫폼 추가하기](https://github.com/baigao417/openmd/blob/main/docs/adding-a-platform.md)를 참고하세요. 개발 방법은 Windows, macOS, Linux에서 동일합니다.

```bash
git clone https://github.com/baigao417/openmd && cd openmd
python scripts/dev_setup.py     # .venv 생성, 개발 의존성 설치, ffmpeg / Node 확인
python -m pytest
```

모든 수정에는 수정 전에 실패하던 테스트가 함께 있어야 하고, "이제 된다"는 모든 주장에는 실제로 실행해 본 진짜 링크가 함께 있어야 합니다. Pull Request를 열기 전에 [CONTRIBUTING](https://github.com/baigao417/openmd/blob/main/CONTRIBUTING.md)과 [코드 규칙](https://github.com/baigao417/openmd/blob/main/docs/code-standards.md)을 읽어 주세요.

---

## 감사의 말과 라이선스

Openmd는 [OpenCLI](https://github.com/jackwener/opencli)(브라우저 읽기, 내장), [trafilatura](https://github.com/adbar/trafilatura), [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api), Bilibili-Harvest(Lanbin07), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [MarkItDown](https://github.com/microsoft/markitdown) 등의 오픈소스 프로젝트 위에 세워졌습니다. 자세한 내용과 라이선스는 [THIRD_PARTY_NOTICES](https://github.com/baigao417/openmd/blob/main/THIRD_PARTY_NOTICES.md)를 참고하세요.

이 프로젝트는 [MIT](https://github.com/baigao417/openmd/blob/main/LICENSE) 라이선스로 공개됩니다. 저장하는 콘텐츠의 저작권을 존중해 주세요. Openmd는 개인 보관과 연구를 위한 도구입니다.

**Everything to Markdown — just beginning.**
