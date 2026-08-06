#!/usr/bin/env python3
"""
지연 기록 분석

폰이 보고한 값을 모아둔 latency-log.csv 를 읽어서, 재생 세션마다
버퍼가 한쪽으로 계속 밀렸는지(클럭 드리프트) 판정한다.

PC 와 폰의 48000 Hz 는 정확히 같지 않다. 어긋난 만큼 폰의 지터 버퍼가
서서히 차거나 마르는데, 그 기울기를 ppm 으로 환산해서 보여준다.

사용법:
    python3 tools/latency_report.py [로그파일]
"""

import csv
import statistics
import os
import sys
from pathlib import Path

DEFAULT_LOG = os.path.join(
    Path.home(), ".local", "share", "keystone-player", "latency-log.csv"
)
# 소리가 이만큼 늦으면 사람이 알아채기 시작한다 (ITU-R BT.1359).
# 이 아래로 밀리는 드리프트는 있어도 없는 것과 같으므로 무시한다.
PERCEPTIBLE_MS = 45
FILM_HOURS = 2
DELAY_CAP_MS = 500


def robust_trend(rows: list, key: str) -> float:
    """시간당 변화량 (ms/시간).

    최소제곱은 쓰지 않는다. 실측해보니 폰 지연은 몇 분 주기로 오르내리는데,
    직선을 맞추면 그 배회를 드리프트로 오판한다. 앞 1/3 과 뒤 1/3 의 중앙값을
    비교하면 중간의 출렁임에 휘둘리지 않고 순 변화만 남는다.
    """
    n = len(rows)
    if n < 6:
        return 0.0
    third = n // 3
    head, tail = rows[:third], rows[-third:]
    dt = (
        statistics.median([r["t"] for r in tail])
        - statistics.median([r["t"] for r in head])
    )
    if dt <= 0:
        return 0.0
    delta = (
        statistics.median([r[key] for r in tail])
        - statistics.median([r[key] for r in head])
    )
    return delta / dt * 3600


def direction_changes(rows: list, key: str, buckets: int = 6) -> int:
    """구간 중앙값이 오르내린 횟수.

    클럭 드리프트는 한 방향으로만 쌓인다. 방향이 여러 번 바뀌면 그건 드리프트가
    아니라 배회이고, 앞뒤만 비교하면 배회의 반 주기를 드리프트로 오해하게 된다.
    """
    if len(rows) < buckets * 3:
        return 0
    span = rows[-1]["t"] - rows[0]["t"]
    if span <= 0:
        return 0
    groups: dict = {}
    for r in rows:
        idx = min(buckets - 1, int((r["t"] - rows[0]["t"]) / span * buckets))
        groups.setdefault(idx, []).append(r[key])
    mids = [statistics.median(groups[i]) for i in sorted(groups)]
    diffs = [b - a for a, b in zip(mids, mids[1:])
             if abs(b - a) >= PERCEPTIBLE_MS / 4]  # 미세한 흔들림은 방향으로 안 침
    return sum(1 for a, b in zip(diffs, diffs[1:]) if (a > 0) != (b > 0))


def load(path: str) -> dict:
    sessions: dict = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                sessions.setdefault(row["session"], []).append({
                    "t": float(row["elapsed_s"]),
                    "buffer": float(row["buffer_ms"]),
                    "output": float(row["output_ms"]),
                    "one_way": float(row["one_way_ms"]),
                    "total": float(row["total_ms"]),
                })
            except (KeyError, ValueError):
                continue
    return sessions


def report(name: str, rows: list):
    rows.sort(key=lambda r: r["t"])
    duration = rows[-1]["t"] - rows[0]["t"]
    buffers = [r["buffer"] for r in rows]
    totals = [r["total"] for r in rows]

    print(f"\n■ {name}  ({len(rows)}개 보고, {duration / 60:.1f}분)")
    print(f"   버퍼   {min(buffers):5.0f} ~ {max(buffers):5.0f} ms "
          f"(평균 {sum(buffers) / len(buffers):.0f})")
    print(f"   총지연 {min(totals):5.0f} ~ {max(totals):5.0f} ms "
          f"(평균 {sum(totals) / len(totals):.0f})")
    print(f"   출력   평균 {sum(r['output'] for r in rows) / len(rows):.0f} ms"
          f" | 편도 평균 {sum(r['one_way'] for r in rows) / len(rows):.0f} ms")

    capped = sum(1 for t in totals if t >= DELAY_CAP_MS)
    if capped:
        print(f"   ⚠ 총지연이 상한 {DELAY_CAP_MS}ms 에 닿은 보고 {capped}회 "
              f"— 이 구간은 립싱크가 어긋났을 것")

    if duration < 300:
        print("   (5분 미만이라 드리프트 판정은 신뢰하기 어려움)")
        return

    # 버퍼 기울기 → ppm. 1시간에 3.6ms 밀리면 1ppm.
    # 폰이 버퍼를 항상 0 으로 보고하는 구현도 있어서, 실제로 영상 지연을 움직이는
    # 총지연으로 판정한다.
    per_hour = robust_trend(rows, "total")
    film = abs(per_hour) * FILM_HOURS
    print(f"   총지연 추세 {per_hour:+.0f} ms/시간 → 클럭 차이 약 {per_hour / 3.6:+.1f} ppm")
    print(f"   {FILM_HOURS}시간 누적 {film:.0f}ms (지각 한계 {PERCEPTIBLE_MS}ms)")

    # 한쪽으로 미는 것과 제자리에서 출렁이는 것은 대처가 다르다
    mids = [r["total"] for r in rows]
    wander = statistics.median(
        [abs(b - a) for a, b in zip(mids, mids[1:])]
    ) if len(mids) > 1 else 0
    spread = max(mids) - min(mids)
    print(f"   배회 폭 {spread:.0f}ms (보고 간 중앙 변화 {wander:.0f}ms)")

    # 드리프트는 한쪽으로만 민다. 오르내리면 배회지 드리프트가 아니다.
    turns = direction_changes(rows, "total")
    if turns >= 2:
        print(f"   구간별로 방향이 {turns}번 바뀜 → 한쪽으로 미는 것이 아니라 배회")
        print("   → 드리프트라 볼 수 없다. 판정하려면 더 긴 세션(영화 한 편)이 필요하다.")
    elif film < PERCEPTIBLE_MS:
        print("   → 드리프트 없음. 적응형 리샘플링 불필요.")
    else:
        print("   → 드리프트 있음. 폰에서 버퍼 수위에 맞춘 적응형 리샘플링이 필요하다.")

    if spread >= PERCEPTIBLE_MS:
        print("      배회 폭이 지각 한계를 넘으므로 영상 지연이 가끔 재조정된다.")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG
    if not os.path.exists(path):
        print(f"기록이 없습니다: {path}")
        print("폰을 연결하고 재생하면 자동으로 쌓입니다.")
        return 1

    sessions = load(path)
    if not sessions:
        print("기록이 비어 있습니다.")
        return 1

    print(f"기록: {path}")
    for name, rows in sorted(sessions.items()):
        if len(rows) >= 2:
            report(name, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
