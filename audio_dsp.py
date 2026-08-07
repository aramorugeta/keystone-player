#!/usr/bin/env python3
"""
사운드 보정 (볼륨 평준화 + 최대 출력 제한)

PipeWire filter-chain 으로 가상 싱크를 하나 만들고,
Keystone Player 가 내는 오디오 스트림만 그 싱크로 옮겨서 처리한다.

  입력 → SC4 컴프레서 → Fast Lookahead 리미터 → 출력 게인(볼륨) → 기본 출력장치

- 컴프레서: 조용한 대사는 올리고 시끄러운 액션은 눌러서 체감 볼륨을 일정하게
- 리미터  : 설정한 dB 이상은 절대 나가지 않게 하는 하드 실링
- 출력 게인: 리미터 뒤에 있으므로 볼륨을 올려도 실링을 넘지 못한다

시스템 기본 출력이나 다른 앱의 소리는 건드리지 않는다.
앱이 종료되면 가상 싱크도 같이 사라지고 스트림은 기본 출력으로 되돌아간다.
"""

import ctypes
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer

PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """부모가 사라지면 커널이 이 프로세스를 죽이도록 표시한다.

    앱이 kill -9 로 죽거나 크래시하면 Qt 의 정리 코드가 돌지 않아 헬퍼가 살아남는다.
    그러면 유령 싱크가 이름을 계속 점유한 채 스피커로 소리를 흘린다.
    """
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except OSError:
        pass


LADSPA_DIRS = ["/usr/lib64/ladspa", "/usr/lib/ladspa", "/usr/local/lib/ladspa"]
COMP_PLUGIN = "sc4_1882"
COMP_LABEL = "sc4"
LIMIT_PLUGIN = "fast_lookahead_limiter_1913"
LIMIT_LABEL = "fastLookaheadLimiter"

SINK_NAME = "keystone_dsp"
SINK_DESC = "Keystone Player DSP"
RTP_SINK_NAME = "keystone_rtp"

# RTP 송출 설정 — 안드로이드 수신 앱과 반드시 일치해야 한다 (net_audio.py 와 동일)
RTP_FORMAT = "S16BE"
RTP_RATE = 48000
RTP_CHANNELS = 2
RTP_PACKET_MS = 5
RTP_LATENCY_MS = 20  # PC 쪽 송출 버퍼. 지터 흡수는 폰이 담당한다

# 폰 송출 때는 평준화를 빼기 때문에 컴프레서의 makeup gain 도 같이 사라진다.
# 그만큼 조용해지므로 고정 게인으로 되돌려준다. 미세 조정은 폰에서 한다.
RTP_BOOST_DB = 15.0
# 게인을 올리면 큰 소리에서 0 dBFS 를 넘어 깨진다. 이 리미터는 평준화용이 아니라
# 그 피크만 막는 안전장치라서, 넘지 않는 구간에서는 아무 일도 하지 않는다.
RTP_SAFETY_CEILING_DB = -0.5

# 강도 프리셋: (threshold dB, ratio, attack ms, release ms, makeup gain dB)
PRESETS = {
    "light": ("약 (은은하게)", -20.0, 2.5, 25.0, 500.0, 4.0),
    "medium": ("중 (권장)", -26.0, 4.0, 15.0, 400.0, 8.0),
    "strong": ("강 (야간/심야)", -32.0, 8.0, 8.0, 300.0, 12.0),
}
DEFAULT_PRESET = "medium"
DEFAULT_CEILING = -3.0


def _ladspa_path(plugin: str) -> str | None:
    for d in LADSPA_DIRS:
        p = os.path.join(d, plugin + ".so")
        if os.path.isfile(p):
            return p
    return None


def check_requirements() -> str | None:
    """사용 가능하면 None, 아니면 사용자에게 보여줄 안내 문구를 반환."""
    for tool in ("pipewire", "pw-dump"):
        if shutil.which(tool) is None:
            return f"{tool} 명령을 찾을 수 없습니다 (PipeWire 필요)"
    if _ladspa_path(COMP_PLUGIN) is None or _ladspa_path(LIMIT_PLUGIN) is None:
        return "LADSPA 플러그인 필요: sudo dnf install ladspa-swh-plugins"
    return None


def _descendant_pids(root: int) -> set[int]:
    """root 프로세스와 그 하위 프로세스 전체 (QtWebEngine 자식 포함)."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                fields = f.read().rpartition(")")[2].split()
            ppid = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))

    seen = {root}
    stack = [root]
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


class AudioDSP(QObject):
    """가상 DSP 싱크의 생명주기 + 파라미터 제어."""

    def __init__(self, storage_dir: str, parent=None):
        super().__init__(parent)
        self.storage_dir = storage_dir
        self.conf_path = os.path.join(storage_dir, "audio-dsp.conf")
        self.process: subprocess.Popen | None = None
        self._sink_id: int | None = None
        self._preset = DEFAULT_PRESET
        self._ceiling_db = DEFAULT_CEILING
        self._volume = 100
        self._moved: set[int] = set()
        self._net_target: tuple[str, int] | None = None
        self._leveling = True

        # 새로 생긴 스트림을 주기적으로 DSP 싱크로 옮긴다
        self._move_timer = QTimer(self)
        self._move_timer.timeout.connect(self._move_streams)

    # ---- 상태 ----

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    # ---- 시작/정지 ----

    def start(self) -> str | None:
        """DSP 싱크를 띄운다. 실패하면 오류 문구를 반환."""
        problem = check_requirements()
        if problem:
            return problem
        if self.is_running():
            return None

        self._kill_stale()
        self._write_conf()

        try:
            self.process = subprocess.Popen(
                ["pipewire", "-c", self.conf_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=_die_with_parent,
            )
        except OSError as exc:
            self.process = None
            return f"pipewire 프로세스를 시작하지 못했습니다: {exc}"

        # 싱크가 그래프에 등록될 때까지 잠깐 기다린다
        for _ in range(30):
            self._sink_id = self._find_sink_id()
            if self._sink_id is not None:
                break
            if self.process.poll() is not None:
                self.process = None
                return "DSP 싱크 생성 실패 (pipewire 가 곧바로 종료됨)"
            time.sleep(0.1)

        if self._sink_id is None:
            self.stop()
            return "DSP 싱크를 찾을 수 없습니다"

        self.apply_params()
        self._move_streams()
        self._move_timer.start(1000)
        return None

    def stop(self):
        self._move_timer.stop()
        # 옮겨놨던 스트림은 기본 출력으로 되돌린다
        for node_id in self._moved:
            self._set_target(node_id, None)
        self._moved.clear()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            self.process = None
        self._sink_id = None

    def _kill_stale(self):
        """앱이 비정상 종료돼 남아있는 이전 DSP 프로세스 정리."""
        try:
            subprocess.run(
                ["pkill", "-f", f"pipewire -c {self.conf_path}"],
                capture_output=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---- 파라미터 ----

    def set_leveling(self, enabled: bool):
        """볼륨 평준화 사용 여부. 폰 송출 중에는 이 값과 무관하게 꺼진다."""
        enabled = bool(enabled)
        if enabled == self._leveling:
            return
        self._leveling = enabled
        self._restart_if_running()

    def leveling_active(self) -> bool:
        """실제로 압축이 걸리고 있는가.

        폰으로 소리를 보낼 때는 이어폰으로 듣는 상황이라 평준화가 필요 없고,
        원본 그대로 보내는 편이 낫다. 체크박스 상태는 로컬 재생용으로 유지된다.
        """
        return self._leveling and self._net_target is None

    def set_network_target(self, ip: str, port: int):
        """DSP 출력을 이 주소로 RTP 송출한다 (로컬 스피커 대신)."""
        target = (ip, int(port))
        if target == self._net_target:
            return
        self._net_target = target
        self._restart_if_running()

    def clear_network_target(self):
        """로컬 출력으로 되돌린다."""
        if self._net_target is None:
            return
        self._net_target = None
        self._restart_if_running()

    def network_target(self) -> tuple[str, int] | None:
        return self._net_target

    def _restart_if_running(self):
        """출력 경로가 바뀌면 체인을 다시 띄운다 (짧은 끊김 발생)."""
        if self.is_running():
            self.stop()
            self.start()

    def set_preset(self, name: str):
        if name in PRESETS:
            self._preset = name
            self.apply_params()

    def set_ceiling_db(self, value: float):
        self._ceiling_db = float(value)
        self.apply_params()

    def set_volume(self, percent: int):
        """0-100. 리미터 뒤에 걸리는 출력 게인."""
        self._volume = max(0, min(100, int(percent)))
        if self.is_running():
            self._set_props(self._volume_params())

    def _output_gain(self) -> float:
        """폰 송출 중에는 고정 레벨로 내보낸다.

        볼륨은 폰에서 조절하므로 PC 쪽 슬라이더가 끼어들 이유가 없다.
        평준화가 빠지면서 사라진 makeup gain 만큼을 게인으로 되돌려 보낸다.
        """
        if self._net_target is not None:
            return 10.0 ** (RTP_BOOST_DB / 20.0)
        return (self._volume / 100.0) ** 3  # 체감에 맞춘 3제곱 커브

    def _volume_params(self) -> list:
        gain = self._output_gain()
        return ['"vol_l:Mult"', f"{gain:.6f}", '"vol_r:Mult"', f"{gain:.6f}"]

    def apply_params(self):
        if not self.is_running():
            return
        params = []
        if self.leveling_active():
            # 평준화가 꺼진 그래프에는 comp 노드가 아예 없으므로 보내면 안 된다
            _, thr, ratio, attack, release, makeup = PRESETS[self._preset]
            params = [
                '"comp:Threshold level (dB)"', f"{thr}",
                '"comp:Ratio (1:n)"', f"{ratio}",
                '"comp:Attack time (ms)"', f"{attack}",
                '"comp:Release time (ms)"', f"{release}",
                '"comp:Makeup gain (dB)"', f"{makeup}",
                '"lim:Limit (dB)"', f"{self._ceiling_db}",
            ]
        elif self._net_target is not None:
            params = ['"lim:Limit (dB)"', f"{RTP_SAFETY_CEILING_DB}"]
        self._set_props(params + self._volume_params())

    def _set_props(self, params: list):
        if self._sink_id is None:
            return
        payload = "{ params = [ " + " ".join(params) + " ] }"
        try:
            subprocess.run(
                ["pw-cli", "set-param", str(self._sink_id), "Props", payload],
                capture_output=True, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---- PipeWire 조회/라우팅 ----

    @staticmethod
    def _pw_dump() -> list:
        try:
            out = subprocess.run(
                ["pw-dump"], capture_output=True, timeout=5, text=True,
            ).stdout
            return json.loads(out) if out else []
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return []

    def _find_sink_id(self) -> int | None:
        for obj in self._pw_dump():
            props = (obj.get("info") or {}).get("props") or {}
            if props.get("node.name") == SINK_NAME:
                return obj.get("id")
        return None

    def _move_streams(self):
        """이 앱(과 자식 프로세스)이 만든 오디오 스트림만 DSP 싱크로 옮긴다."""
        if not self.is_running():
            return
        if self._sink_id is None:
            self._sink_id = self._find_sink_id()
            if self._sink_id is None:
                return

        pids = _descendant_pids(os.getpid())
        dump = self._pw_dump()

        # 클라이언트 → pid (네이티브 PipeWire 클라이언트는 노드에 pid 가 없다)
        client_pid: dict[int, int] = {}
        for obj in dump:
            if not str(obj.get("type", "")).endswith("Client"):
                continue
            props = (obj.get("info") or {}).get("props") or {}
            for key in ("pipewire.sec.pid", "application.process.id"):
                try:
                    client_pid[obj["id"]] = int(props[key])
                    break
                except (KeyError, TypeError, ValueError):
                    continue

        alive = set()
        for obj in dump:
            props = (obj.get("info") or {}).get("props") or {}
            if props.get("media.class") != "Stream/Output/Audio":
                continue
            if str(props.get("node.name", "")).startswith(SINK_NAME):
                continue

            pid = None
            try:
                pid = int(props["application.process.id"])
            except (KeyError, TypeError, ValueError):
                pid = client_pid.get(props.get("client.id"))
            if pid not in pids:
                continue

            node_id = obj.get("id")
            alive.add(node_id)
            if node_id not in self._moved:
                self._moved.add(node_id)
                self._set_target(node_id, SINK_NAME)

        self._moved &= alive  # 사라진 스트림은 추적 목록에서 제거

    def _set_target(self, node_id, target: str | None):
        if node_id is None:
            return
        cmd = ["pw-metadata", str(node_id), "target.object"]
        cmd += [target] if target else ["null"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---- 설정 파일 ----

    def _rtp_module(self) -> str:
        """네트워크 출력이 켜져 있으면 RTP 송출 싱크를 만드는 모듈 블록."""
        if self._net_target is None:
            return ""
        ip, port = self._net_target
        return f"""    {{ name = libpipewire-module-rtp-sink
        args = {{
            destination.ip   = "{ip}"
            destination.port = {port}
            net.mtu          = 1280
            sess.min-ptime   = {RTP_PACKET_MS}
            sess.max-ptime   = {RTP_PACKET_MS}
            sess.latency.msec = {RTP_LATENCY_MS}
            audio.format     = "{RTP_FORMAT}"
            audio.rate       = {RTP_RATE}
            audio.channels   = {RTP_CHANNELS}
            audio.position   = [ FL FR ]
            stream.props = {{
                node.name        = "{RTP_SINK_NAME}"
                node.description = "Keystone RTP ({ip})"
                media.class      = Audio/Sink
            }}
        }}
    }}
"""

    def _playback_props(self) -> str:
        """네트워크 출력이면 RTP 싱크로, 아니면 시스템 기본 출력으로 보낸다."""
        if self._net_target is not None:
            return f"""node.name     = "{SINK_NAME}_out"
                node.passive  = false
                target.object = "{RTP_SINK_NAME}"
                audio.position = [ FL FR ]"""
        return f"""node.name    = "{SINK_NAME}_out"
                node.passive = true
                audio.position = [ FL FR ]"""

    def _limiter_node(self, ceiling_db: float) -> str:
        return f"""                    {{
                        type   = ladspa
                        name   = lim
                        plugin = {LIMIT_PLUGIN}
                        label  = {LIMIT_LABEL}
                        control = {{
                            "Input gain (dB)"   = 0.0
                            "Limit (dB)"        = {ceiling_db}
                            "Release time (s)"  = 0.25
                        }}
                    }}"""

    def _filter_graph(self) -> str:
        """평준화가 켜져 있으면 컴프레서+리미터+볼륨, 아니면 볼륨 게인만."""
        gain = self._output_gain()
        vol_nodes = f"""                    {{ type = builtin name = vol_l label = linear
                       control = {{ "Mult" = {gain:.6f} "Add" = 0.0 }} }}
                    {{ type = builtin name = vol_r label = linear
                       control = {{ "Mult" = {gain:.6f} "Add" = 0.0 }} }}"""

        if not self.leveling_active():
            # 폰 송출: 게인 → 클리핑 방지 리미터. 압축은 하지 않는다.
            return f"""                nodes = [
{vol_nodes}
{self._limiter_node(RTP_SAFETY_CEILING_DB)}
                ]
                links = [
                    {{ output = "vol_l:Out" input = "lim:Input 1" }}
                    {{ output = "vol_r:Out" input = "lim:Input 2" }}
                ]
                inputs  = [ "vol_l:In" "vol_r:In" ]
                outputs = [ "lim:Output 1" "lim:Output 2" ]"""

        _, thr, ratio, attack, release, makeup = PRESETS[self._preset]
        return f"""                nodes = [
                    {{
                        type   = ladspa
                        name   = comp
                        plugin = {COMP_PLUGIN}
                        label  = {COMP_LABEL}
                        control = {{
                            "RMS/peak"             = 0.0
                            "Attack time (ms)"     = {attack}
                            "Release time (ms)"    = {release}
                            "Threshold level (dB)" = {thr}
                            "Ratio (1:n)"          = {ratio}
                            "Knee radius (dB)"     = 3.0
                            "Makeup gain (dB)"     = {makeup}
                        }}
                    }}
{self._limiter_node(self._ceiling_db)}
{vol_nodes}
                ]
                links = [
                    {{ output = "comp:Left output"  input = "lim:Input 1" }}
                    {{ output = "comp:Right output" input = "lim:Input 2" }}
                    {{ output = "lim:Output 1"      input = "vol_l:In" }}
                    {{ output = "lim:Output 2"      input = "vol_r:In" }}
                ]
                inputs  = [ "comp:Left input" "comp:Right input" ]
                outputs = [ "vol_l:Out" "vol_r:Out" ]"""

    def _write_conf(self):
        os.makedirs(self.storage_dir, exist_ok=True)
        conf = f"""# Keystone Player 사운드 DSP (자동 생성 - 직접 편집하지 마세요)
context.properties = {{ log.level = 0 }}
context.spa-libs = {{ audio.convert.* = audioconvert/libspa-audioconvert }}
context.modules = [
    {{ name = libpipewire-module-rt args = {{}} flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
{self._rtp_module()}    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = "{SINK_DESC}"
            media.name       = "{SINK_DESC}"
            filter.graph = {{
{self._filter_graph()}
            }}
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name   = "{SINK_NAME}"
                media.class = Audio/Sink
                audio.position = [ FL FR ]
            }}
            playback.props = {{
                {self._playback_props()}
            }}
        }}
    }}
]
"""
        with open(self.conf_path, "w") as f:
            f.write(conf)


def default_storage_dir() -> str:
    return os.path.join(Path.home(), ".local", "share", "keystone-player")
