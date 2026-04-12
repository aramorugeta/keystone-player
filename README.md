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
- **설정 저장** — 키스톤 보정값, 브라우저 로그인 세션 영구 저장

## 요구 사항

- Python 3.10+
- PySide6 (`pip install PySide6`)
- mpv (파일 재생용)
- Chromium 설치 (Netflix DRM용, Widevine CDM 필요)

### Fedora 설치 예시

```bash
sudo dnf install mpv chromium
pip install PySide6
```

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
