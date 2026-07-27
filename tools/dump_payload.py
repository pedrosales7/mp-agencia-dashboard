#!/usr/bin/env python3
"""Dump do payload REAL da análise IA, a partir do cache versionado no repo.

Reconstrói o que `weekly_refresh.main()` entrega para `ai_analysis.build_payload()`
usando `data/cache/historical_data.json` — sem tocar no Metabase. Serve para
alimentar o playground de prompts com dados de verdade em vez do mock.

  python3 tools/dump_payload.py                      # cutoff = último dia do cache
  python3 tools/dump_payload.py 2026-07-15 2026-07-08 2026-07-01

Saída: tools/payloads/payload_<cutoff>.json
"""
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ai_analysis  # noqa: E402

CACHE_PATH = os.path.join(ROOT, "data", "cache", "historical_data.json")
OUT_DIR = os.path.join(ROOT, "tools", "payloads")

# espelha weekly_refresh.VALID_PARTNERS (importar o módulo exige env vars do Metabase)
VALID_PARTNERS = ["loga-internet", "the fiber internet", "interplus internet", "direct internet",
                  "ativa-telecom", "enove-fibra", "ultranet-network", "unifique"]


def main(argv):
    with open(CACHE_PATH) as f:
        cache = json.load(f)

    daily = cache["daily_snapshot"]
    dfg = cache["daily_funnel_google"]
    dfm = cache["daily_funnel_meta"]

    cutoffs = argv[1:] or [max(r["dia"] for r in daily)]
    os.makedirs(OUT_DIR, exist_ok=True)

    for c in cutoffs:
        cutoff_dt = date.fromisoformat(c)
        payload = ai_analysis.build_payload(
            [r for r in daily if r["dia"] <= c],
            [r for r in dfg if r["dia"] <= c],
            [r for r in dfm if r["dia"] <= c],
            cutoff_dt, VALID_PARTNERS)
        path = os.path.join(OUT_DIR, f"payload_{c}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        print(f"{path}  {os.path.getsize(path) // 1024}KB")


if __name__ == "__main__":
    main(sys.argv)
