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
import os
import sys
from pathlib import Path

DEFAULT_LOG = os.path.join(
    Path.home(), ".local", "share", "keystone-player", "latency-log.csv"
)
# 이 이상 기울면 긴 영화에서 눈에 띄게 밀린다고 본다
DRIFT_PPM_THRESHOLD = 5.0
DELAY_CAP_MS = 500


def slope(xs, ys) -> float:
    """최소제곱 기울기 (y 단위 / x 단위)."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


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
    per_hour = slope([r["t"] for r in rows], buffers) * 3600
    ppm = per_hour / 3.6
    direction = "차오름" if per_hour > 0 else "마름"
    print(f"   버퍼 추세 {per_hour:+.0f} ms/시간 → 클럭 차이 약 {ppm:+.1f} ppm ({direction})")

    if abs(ppm) < DRIFT_PPM_THRESHOLD:
        print("   → 드리프트 없음. 지금 구조로 충분하다.")
    else:
        two_hours = abs(per_hour) * 2
        print(f"   → 드리프트 있음. 2시간 영화면 약 {two_hours:.0f}ms 밀린다.")
        print("      폰에서 버퍼 수위에 맞춘 적응형 리샘플링이 필요하다.")


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
