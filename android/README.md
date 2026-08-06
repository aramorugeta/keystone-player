# Keystone Receiver (Android)

리눅스 PC 의 `keystone-player` 가 쏘는 RTP 오디오를 받아 재생하고,
자기 재생 지연을 PC 로 보고해서 립싱크 보정을 돕는 안드로이드 앱.

프로토콜 상세는 `PROMPT.md` 참고.

## 빌드

1. Android Studio (Iguana 이상 권장) 로 이 폴더를 연다.
2. 처음 열 때 Gradle Sync 가 돌면서 `gradle-wrapper.jar` 등 필요한 파일이 자동으로 생성된다.
   - 명령줄에서만 빌드하려면 시스템에 설치된 Gradle 로 한 번 `gradle wrapper` 를 실행해 wrapper 를 만든 뒤
     `./gradlew assembleDebug` 로 APK 를 뽑을 수 있다.
3. 폰을 USB 로 연결하고 실행. `minSdk = 26` (Android 8.0) 이상.

## 사용

1. PC 에서 `python3 main.py` 실행
2. 「사운드 보정」의 **볼륨 평준화** 체크
3. 「폰으로 소리 보내기」의 **네트워크 출력** 체크
4. PC 화면에 표시된 주소(예: `192.168.0.42 : 46001`) 에서 IP 만 앱에 입력
5. 「접속」 누르면 hello → ready 왕복 후 오디오가 흐른다.
6. 화면이 꺼져도 재생은 포그라운드 서비스로 유지된다.
   기기 설정에서 **배터리 최적화 예외** 로 이 앱을 등록해야 심야에 Doze 로 죽지 않는다.

## 구조

```
app/src/main/kotlin/com/keystone/receiver/
├── MainActivity.kt         # Compose UI: 주소 입력, 상태/지연 표시
├── ReceiverService.kt      # 포그라운드 서비스, WiFi/Wake lock, 1초마다 지연 보고
├── ReceiverState.kt        # UI 로 노출되는 StateFlow 모음
├── net/
│   ├── ControlClient.kt    # TCP 46001 JSON 채널 (hello/ping/pong/latency/bye)
│   └── RtpReceiver.kt      # UDP 46000 RTP 수신 (v2, PT 127, S16BE)
└── audio/
    ├── JitterBuffer.kt     # 적응형 60/20/200ms, 시퀀스 재정렬, 언더런 카운트
    └── AudioPlayer.kt      # AudioTrack LOW_LATENCY, getTimestamp 로 출력 지연 측정
```

## 만들지 않은 것

프롬프트대로: 오디오 코덱, 자체 지연 보정, 재생 속도 조절 없음.
