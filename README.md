# Keystone Player

프로젝터용 좌우 키스톤(사다리꼴) 보정 영상 플레이어.  
프로젝터가 비스듬히 설치된 경우 소프트웨어로 화면 왜곡을 보정합니다.

## 기능

- **파일 재생** — mpv를 통한 영상 파일 재생 + FFmpeg perspective 필터로 키스톤 보정
- **브라우저 재생** — 내장 브라우저로 웹 콘텐츠 표시 (YouTube, Netflix 등)
- **Netflix DRM 지원** — Chromium의 Widevine CDM을 활용한 DRM 콘텐츠 재생
- **에뮬레이터** — 프로젝터 없이도 키스톤 보정 결과를 미리보기
- **프로젝터 출력** — 에뮬레이터 창에서 버튼 하나로 프로젝터 화면에 전체화면 복제 출력
- **메인 모니터 보호** — 프로젝터 출력 대상에서 메인 모니터 자동 제외
- **사운드 보정** — 볼륨 평준화(대화 크게 / 액션 작게) + 설정한 dB 이상 절대 안 나가는 최대 출력 제한
- **설정 저장** — 키스톤 보정값, 브라우저 로그인 세션 영구 저장

## 요구 사항

- Python 3.10+
- PySide6 (`pip install PySide6`)
- mpv (파일 재생용)
- Chromium 설치 (Netflix DRM용, Widevine CDM 필요)
- PipeWire + `ladspa-swh-plugins` (사운드 보정용, 없으면 해당 기능만 비활성화)

### Fedora 설치 예시

```bash
sudo dnf install mpv chromium ladspa-swh-plugins
pip install PySide6
```

## 사운드 보정

집에서 볼 때 대사는 안 들리고 액션 장면만 시끄러운 문제를 잡아준다.

PipeWire filter-chain 으로 가상 싱크를 하나 만들고 **이 앱이 내는 소리만** 거기로 옮겨서 처리한다.
시스템 기본 출력이나 다른 앱 소리는 건드리지 않고, 앱을 끄면 원래대로 돌아간다.

```
앱 오디오 → SC4 컴프레서 → Fast Lookahead 리미터 → 출력 게인 → 기본 출력장치
```

- **볼륨 평준화** — 조용한 대사는 올리고 큰 소리는 눌러서 체감 볼륨을 일정하게 유지
  - 강도: 약 / 중(권장) / 강(야간)
- **최대 출력 제한** — 설정한 dBFS 를 절대 넘지 않는 하드 리미터. 볼륨 슬라이더는 리미터
  *뒤*에 걸리므로 볼륨을 올려도 이 한계를 넘지 못한다.
- 파일 재생과 브라우저(YouTube/Netflix) 재생 양쪽 모두에 적용되고, 볼륨 슬라이더 하나로
  두 경우 모두 조절된다.

실측 (1kHz 사인파, 강도=중, 제한=-3dB):

| 입력 | 보정 없을 때 | 실제 출력 |
|---|---|---|
| 조용한 대사 -30 dBFS | -33 dBFS | **-25 dBFS** |
| 시끄러운 액션 -6 dBFS | -1 dBFS | **-14 dBFS** |

입력 24 dB 차이 → 출력 11 dB 차이, 피크는 -3 dBFS 를 넘지 않음.

## 영상 지연 보정 (립싱크)

소리를 블루투스나 네트워크로 다른 기기에 보내면 100~300ms 늦게 도착해서 입모양이 어긋난다.
Qt 는 오디오 출력 지연을 영상 동기에 반영하지 않는다 — 오디오 버퍼를 341ms 로 키워도
영상은 37ms 만 움직이는 것을 실측으로 확인했다. 그래서 영상 쪽을 직접 늦춘다.

`FrameDelay` 가 QMediaPlayer 의 프레임을 받아 큐에 담아뒀다가 설정한 시간 뒤에 화면으로
넘긴다. 지연 0 이면 큐를 거치지 않고 그대로 통과한다.

실측 (30fps):

| 설정 | 실제 지연 | 지터 |
|---|---|---|
| 0 ms | 기준 | ±1.1 ms |
| 200 ms | 196 ms | ±2.8 ms |
| 400 ms | 391 ms | ±2.7 ms |

**파일 재생에만 적용된다.** 브라우저 모드는 Chromium 이 내부에서 직접 렌더링하므로
프레임을 가로챌 수 없다.

## 폰으로 소리 보내기

심야에 프로젝터로 보면서 소리는 폰에 꽂은 이어폰으로 듣기 위한 기능.
블루투스와 달리 **무압축 PCM** 을 그대로 보내므로 음질 손실이 없고, 방을 건너도 끊기지 않는다.

```
앱 오디오 → DSP → RTP(UDP 46000) → WiFi → 안드로이드 수신 앱 → 이어폰
                     ↑
        컨트롤 채널(TCP 46001) ← 폰이 자기 재생 지연을 보고
                     ↓
             영상 지연 자동 설정 → 립싱크 유지
```

- 폰이 컨트롤 채널에 접속하면 PC 가 소켓에서 **폰 IP 를 알아내서** 그쪽으로 송출을 시작한다.
  사용자는 폰에 PC 주소만 입력하면 된다 (PC 화면에 표시됨)
- 폰이 1 초마다 자기 출력 지연과 지터 버퍼 크기를 보고하고, PC 는 여기에 네트워크 편도
  지연(ping/pong 왕복의 최솟값 절반)을 더해 **영상 지연을 자동으로 맞춘다**
- 연결이 끊기면 자동으로 로컬 스피커로 되돌아간다

송출 포맷: RTP payload-type 127, S16BE 48kHz 스테레오, 972 바이트 패킷(5 ms), 약 1.5 Mbps.

안드로이드 수신 앱은 별도 프로젝트다. 프로토콜 명세와 개발 프롬프트는
[android/PROMPT.md](android/PROMPT.md) 참고.

### 평준화는 로컬 재생에서만 걸린다

볼륨 평준화는 스피커로 들을 때 필요한 것이고, 이어폰으로 들을 때는 원본 그대로가 낫다.
그래서 폰이 접속하면 **체크박스 상태와 무관하게 컴프레서·리미터가 그래프에서 빠지고**
볼륨 게인만 남는다 (볼륨 100% 면 ×1.0 이라 무손실 통과). 연결이 끊기면 다시 돌아온다.

| 상태 | 그래프 | 출력 |
|---|---|---|
| 로컬 + 평준화 ON | comp → lim → vol | 시스템 기본 출력 |
| 폰 접속 중 | vol | RTP → 폰 |
| 폰 끊김 | comp → lim → vol | 시스템 기본 출력 |

볼륨 게인만 남기는 이유는 브라우저 재생의 볼륨 조절을 유지하기 위해서다.
QWebEngine 은 자체 볼륨 제어가 없어서 이 게인이 유일한 조절 수단이다.

## 실행

```bash
python main.py
```

### 시스템에 설치 (앱 메뉴 등록)

```bash
# 실행 파일 링크
chmod +x main.py
ln -s $(pwd)/main.py ~/.local/bin/keystone-player

# 아이콘 설치
mkdir -p ~/.local/share/icons/hicolor/scalable/apps/
cp icon.svg ~/.local/share/icons/hicolor/scalable/apps/keystone-player.svg

# .desktop 파일 생성
cat > ~/.local/share/applications/keystone-player.desktop << 'EOF'
[Desktop Entry]
Name=Keystone Player
Comment=Projector keystone correction player
Exec=keystone-player
Type=Application
Categories=AudioVideo;Video;Player;
Icon=keystone-player
Terminal=false
EOF

update-desktop-database ~/.local/share/applications/
```

## 사용법

### 파일 모드
1. 모드를 "파일 (mpv)"로 선택
2. "열기"로 영상 파일 선택
3. "재생" 클릭 → mpv 창에서 키스톤 보정된 영상 재생

### 브라우저 모드
1. 모드를 "브라우저 (Web)"으로 선택
2. URL 입력 후 "이동" 클릭
3. 에뮬레이터 창에 키스톤 보정된 웹 콘텐츠 표시

### 프로젝터 출력
1. 에뮬레이터 체크박스로 미리보기 창 열기
2. 에뮬레이터 상단 툴바에서 출력 대상 화면 선택
3. "프로젝터 출력" 버튼으로 전체화면 출력 시작/정지

### 키스톤 보정
- 슬라이더 또는 미세 조정 버튼(-5, -1, +1, +5)으로 조절
- 보정값은 자동 저장되어 다음 실행 시 복원

## 데이터 저장 위치

| 항목 | 경로 |
|------|------|
| 설정 (키스톤 값) | `~/.local/share/keystone-player/settings.json` |
| 브라우저 쿠키/세션 | `~/.local/share/keystone-player/Cookies` |
| Widevine CDM | `~/.local/share/keystone-player/WidevineCdm` (심볼릭 링크) |

## 라이선스

MIT
