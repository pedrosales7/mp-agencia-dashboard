#!/usr/bin/env python3
"""
Refresh semanal do dashboard MP Agência — versão standalone (GitHub Actions).

Substitui o fluxo manual que rodava dentro do Claude/Cowork:
  1. Loga no Metabase via API (usuário/senha) em vez do conector MCP.
  2. Roda as queries direto via HTTP (sem o limite de 500 linhas do MCP).
  3. Aplica a mesma lógica de merge/compactação do refresh_step3.py original.
  4. Escreve o resultado em docs/index.html (in-place — o histórico já fica no git).
  5. Posta o resultado no Slack via Incoming Webhook (não precisa de sessão interativa).

Notas importantes herdadas do pipeline original (ver SKILL.md / structures.md):
  - As queries SNAPSHOT e PREV_SNAPSHOT do queries.sql NÃO são usadas — o
    refresh_step3.py original já as derivava agregando DAILY_SNAPSHOT em
    Python (agg_daily). Replicado aqui do mesmo jeito.
  - PREV_FUNNEL_GOOGLE/META (fix 2026-07-12): recalculados a cada refresh, mesmas
    janelas de prev_windows (alinhadas com FUNNEL_GOOGLE/META). Antes ficavam
    congeladas com o que estivesse no HTML — comparação de leads/vendas "vs período
    anterior" no dashboard ficava cada vez mais errada com o tempo.
  - FUNNEL_GOOGLE/META rodam 4x (janelas 7d/30d/90d/mês corrente).
  - label_map do FUNNEL_META usa DISTINCT id_mp_canon (armadilha crítica).
"""
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, timedelta

import requests

import ai_analysis

# ── configuração ──────────────────────────────────────────────────────────

METABASE_URL = os.environ["METABASE_URL"].rstrip("/")
METABASE_USERNAME = os.environ["METABASE_USERNAME"]
METABASE_PASSWORD = os.environ["METABASE_PASSWORD"]
PAGES_URL = os.environ.get("PAGES_URL", "")
SLACK_MENTION_ON_ERROR = os.environ.get("SLACK_MENTION_ON_ERROR", "")  # ex: <@U0753LAQU1F>

# Modo teste: manda tudo só pro DM do Pedro, sem @channel. Só liga via input
# manual do workflow_dispatch — o cron de segunda nunca ativa isso.
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() == "true"
if TEST_MODE:
    SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL_TEST"]
else:
    SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

DATABASE_ID = 69

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
HTML_PATH = os.path.join(REPO_ROOT, "docs", "index.html")
ANALYSIS_PATH = os.path.join(REPO_ROOT, "docs", "analise.html")
CACHE_PATH = os.path.join(REPO_ROOT, "data", "cache", "historical_data.json")
QUERIES_PATH = os.path.join(SCRIPT_DIR, "queries.sql")

VALID_PARTNERS = ["loga-internet", "the fiber internet", "interplus internet", "direct internet",
                   "enove-fibra", "unifique", "ultranet-network", "ativa-telecom"]
PIDX = {p: i for i, p in enumerate(VALID_PARTNERS)}
ROLLING_KEYS = {"7d", "30d", "90d"}


# ── Metabase client ──────────────────────────────────────────────────────

class Metabase:
    def __init__(self, base_url, username, password):
        self.base_url = base_url
        resp = requests.post(f"{base_url}/api/session",
                              json={"username": username, "password": password}, timeout=30)
        resp.raise_for_status()
        self.token = resp.json()["id"]

    def query(self, sql):
        resp = requests.post(
            f"{self.base_url}/api/dataset",
            json={"database": DATABASE_ID, "type": "native", "native": {"query": sql}},
            headers={"X-Metabase-Session": self.token},
            timeout=180,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(f"Metabase query error: {body['error']}")
        data = body["data"]
        cols = [c["name"] for c in data["cols"]]
        return [dict(zip(cols, row)) for row in data["rows"]]


def load_queries(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    queries = {}
    for m in re.finditer(r"-- \[QUERY:(\w+)\]\n(.*?)-- \[/QUERY:\1\]", text, re.S):
        queries[m.group(1)] = m.group(2).strip()
    return queries


PERIODO_RE = re.compile(
    r"(WITH periodo AS \(\s*(?:--[^\n]*\n\s*)*)"
    r"SELECT DATE_TRUNC\('month', '\{\{CUTOFF\}\}'::date\)::date AS d_ini,\s*"
    r"'\{\{CUTOFF\}\}'::date AS d_fim(\s*\),)"
)


def with_window(sql, d_ini, d_fim):
    """Substitui a janela da CTE `periodo` das queries FUNNEL_* por um range fixo."""
    new_sql, n = PERIODO_RE.subn(
        rf"\1SELECT '{d_ini}'::date AS d_ini, '{d_fim}'::date AS d_fim\2", sql, count=1)
    if n != 1:
        raise RuntimeError("Não encontrei a CTE `periodo` esperada na query — formato mudou?")
    return new_sql


# ── helpers portados do refresh_step3.py original ───────────────────────

def norm_date(d):
    return str(d)[:10]


def sub_array(html, var, data):
    j = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return re.sub(rf"const {var} = \[[\s\S]*?\];", f"const {var} = {j};", html)


def sub_obj(html, var, data):
    j = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return re.sub(rf"const {var} = \{{[\s\S]*?\}};", f"const {var} = {j};", html)


def extract_js_array(html, var):
    m = re.search(rf"const {var} = (\[[\s\S]*?\]);", html)
    if not m:
        return []
    s = m.group(1)
    s = re.sub(r"([{,]\s*)([a-zA-Z_]\w*)\s*:", r'\1"\2":', s)
    try:
        return json.loads(s)
    except Exception as e:
        print(f"Aviso: não foi possível parsear {var}: {e}")
        return []


def build_compact_daily(rows):
    merged = {}
    for r in rows:
        id_mp = r["id_mp"]
        if id_mp not in PIDX:
            continue
        key = (r["dia"], id_mp)
        d = merged.setdefault(key, {"bruto_g": 0, "cashback_g": 0, "liquido_g": 0, "leads_g": 0, "vendas_g": 0,
                                     "bruto_m": 0, "cashback_m": 0, "liquido_m": 0, "leads_m": 0, "vendas_m": 0})
        suf = "g" if r["canal"] == "google" else "m"
        d[f"bruto_{suf}"] += round(r.get("bruto") or 0)
        d[f"cashback_{suf}"] += round(r.get("cashback") or 0)
        d[f"liquido_{suf}"] += round(r.get("liquido") or 0)
        d[f"leads_{suf}"] += r.get("leads") or 0
        d[f"vendas_{suf}"] += r.get("vendas") or 0
    compact = []
    for (dia, id_mp), d in sorted(merged.items()):
        compact.append([dia, PIDX[id_mp],
            d["bruto_g"], d["cashback_g"], d["liquido_g"], d["leads_g"], d["vendas_g"],
            d["bruto_m"], d["cashback_m"], d["liquido_m"], d["leads_m"], d["vendas_m"]])
    return compact


def build_compact_daily_funnel(fg_rows, fm_rows):
    """Compacta as etapas diárias de funil por (dia, partner), no mesmo estilo
    posicional do DAILY_SNAPSHOT — alimenta o funil completo quando o dashboard
    usa período customizado.

    leads/vendas aqui usam a MESMA atribuição por campanha do FUNNEL_GOOGLE/META
    (utm_campaign para Google, wa_chat_start+7d para Meta) — não a atribuição por
    partner_id_partner do DAILY_SNAPSHOT/SNAPSHOT. As duas metodologias já convivem
    no dashboard (SNAPSHOT usa uma, FUNNEL_GOOGLE/META usa outra); usar a errada
    aqui faria a tabela de funil customizado mostrar leads/vendas divergentes dos
    mesmos partners nos períodos fixos (7d/30d/90d/mês), que usam a de campanha."""
    merged = {}

    def row(dia, id_mp):
        return merged.setdefault((dia, id_mp), {"cliques_g": 0, "sessoes_g": 0, "clickoff_g": 0, "redirect_g": 0,
                                                  "leads_g": 0, "vendas_g": 0,
                                                  "cliques_m": 0, "chat_start_m": 0, "zip_search_m": 0, "redirect_m": 0,
                                                  "leads_m": 0, "vendas_m": 0})

    for r in fg_rows:
        if r["id_mp"] not in PIDX:
            continue
        d = row(r["dia"], r["id_mp"])
        d["cliques_g"] += r.get("cliques") or 0
        d["sessoes_g"] += r.get("sessoes") or 0
        d["clickoff_g"] += r.get("clickoff") or 0
        d["redirect_g"] += r.get("redirect") or 0
        d["leads_g"] += r.get("leads") or 0
        d["vendas_g"] += r.get("vendas") or 0
    for r in fm_rows:
        if r["id_mp"] not in PIDX:
            continue
        d = row(r["dia"], r["id_mp"])
        d["cliques_m"] += r.get("cliques") or 0
        d["chat_start_m"] += r.get("chat_start") or 0
        d["zip_search_m"] += r.get("zip_search") or 0
        d["redirect_m"] += r.get("redirect") or 0
        d["leads_m"] += r.get("leads") or 0
        d["vendas_m"] += r.get("vendas") or 0

    compact = []
    for (dia, id_mp), d in sorted(merged.items()):
        compact.append([dia, PIDX[id_mp],
            d["cliques_g"], d["sessoes_g"], d["clickoff_g"], d["redirect_g"], d["leads_g"], d["vendas_g"],
            d["cliques_m"], d["chat_start_m"], d["zip_search_m"], d["redirect_m"], d["leads_m"], d["vendas_m"]])
    return compact


def ensure_ds_partners(html):
    block = "const DS_PARTNERS = " + json.dumps(VALID_PARTNERS, ensure_ascii=False) + ";\n"
    if "const DS_PARTNERS" in html:
        return re.sub(r"const DS_PARTNERS = \[[\s\S]*?\];\n?", block, html)
    return re.sub(r"(const DAILY_SNAPSHOT = )", block + r"\1", html, count=1)


def normalize_fg_row(r):
    r = dict(r)
    if "clickoffs" in r:
        r["clickoff"] = r.pop("clickoffs")
    if "redirects" in r:
        r["redirect"] = r.pop("redirects")
    return r


def normalize_fm_row(r):
    r = dict(r)
    if "conversas" in r:
        r["chat_start"] = r.pop("conversas")
    if "redirects" in r:
        r["redirect"] = r.pop("redirects")
    return r


def agg_daily(rows, d_ini, d_fim, pkey):
    agg = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if d_ini <= r["dia"] <= d_fim:
            k = (r["id_mp"], r["canal"])
            agg[k]["bruto"] += int(r.get("bruto") or 0)
            agg[k]["cashback"] += int(r.get("cashback") or 0)
            agg[k]["liquido"] += int(r.get("liquido") or 0)
    return [{"period_key": pkey, "id_mp": k[0], "canal": k[1],
             "bruto": v["bruto"], "cashback": v["cashback"], "liquido": v["liquido"]}
            for k, v in agg.items() if v["bruto"] != 0 or v["liquido"] != 0]


# ── Slack ─────────────────────────────────────────────────────────────────

def slack_post(text):
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=30)
    resp.raise_for_status()


# ── main ──────────────────────────────────────────────────────────────────

def main():
    today = date.today()
    cutoff_dt = today - timedelta(days=1)  # nunca usar o dia atual — dados parciais
    cutoff = cutoff_dt.isoformat()
    cover = cutoff_dt.strftime("%d/%m/%y")
    curr_month_key = cutoff_dt.strftime("%Y-%m")
    weekly_start = (cutoff_dt - timedelta(days=70)).isoformat()

    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)

    last_run_month = cache.get("last_run_month")
    prev_key = (cutoff_dt.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    prev_is_frozen = bool(last_run_month and last_run_month != curr_month_key)

    queries = load_queries(QUERIES_PATH)
    mb = Metabase(METABASE_URL, METABASE_USERNAME, METABASE_PASSWORD)

    def run(name, sql):
        print(f"Rodando query {name}...")
        return mb.query(sql)

    # DAILY_SNAPSHOT
    daily_sql = queries["DAILY_SNAPSHOT"].replace("{{CUTOFF}}", cutoff)
    fresh_daily = [dict(r, dia=norm_date(r["dia"])) for r in run("DAILY_SNAPSHOT", daily_sql)]

    # DAILY_FUNNEL_GOOGLE / DAILY_FUNNEL_META — alimentam o funil completo no período customizado
    dfg_sql = queries["DAILY_FUNNEL_GOOGLE"].replace("{{CUTOFF}}", cutoff)
    fresh_dfg = [dict(r, dia=norm_date(r["dia"])) for r in run("DAILY_FUNNEL_GOOGLE", dfg_sql)]
    dfm_sql = queries["DAILY_FUNNEL_META"].replace("{{CUTOFF}}", cutoff)
    fresh_dfm = [dict(r, dia=norm_date(r["dia"])) for r in run("DAILY_FUNNEL_META", dfm_sql)]

    # FUNNEL_GOOGLE / FUNNEL_META — 4 janelas rolling + todos os meses históricos
    # já presentes no HTML. Recomputar tudo a cada refresh corrige o congelamento
    # que deixava meses fechados com a foto do dia da virada (leads sold em julho
    # não voltavam ao snapshot de junho, cliques usavam a coluna antiga, etc).
    with open(HTML_PATH, "r", encoding="utf-8") as _f:
        _html_for_keys = _f.read()
    existing_month_keys = {
        r.get("p_key") for r in extract_js_array(_html_for_keys, "FUNNEL_META") + extract_js_array(_html_for_keys, "FUNNEL_GOOGLE")
        if isinstance(r.get("p_key"), str) and re.fullmatch(r"\d{4}-\d{2}", r["p_key"])
    }
    existing_month_keys.add(prev_key)  # sempre inclui o mês anterior mesmo em HTML ainda sem dados
    windows = {
        "7d": ((cutoff_dt - timedelta(days=6)).isoformat(), cutoff),
        "30d": ((cutoff_dt - timedelta(days=29)).isoformat(), cutoff),
        "90d": ((cutoff_dt - timedelta(days=89)).isoformat(), cutoff),
        curr_month_key: (cutoff_dt.replace(day=1).isoformat(), cutoff),
    }
    for mk in sorted(existing_month_keys):
        if mk == curr_month_key:
            continue
        m_ini = date(int(mk[:4]), int(mk[5:]), 1)
        # último dia do mês: primeiro dia do mês seguinte - 1
        next_m = date(m_ini.year + (1 if m_ini.month == 12 else 0), 1 if m_ini.month == 12 else m_ini.month + 1, 1)
        m_fim = next_m - timedelta(days=1)
        windows[mk] = (m_ini.isoformat(), m_fim.isoformat())

    # janelas *_prev: mesmo tamanho de 7d/30d/90d, período imediatamente anterior.
    # Alinhadas 1:1 com `windows` acima e recomputadas a cada refresh igual as demais
    # (fix 2026-07-12: antes ficavam congeladas com o que estivesse no HTML — ver
    # PREV_FUNNEL_GOOGLE/META mais abaixo).
    prev_windows = {
        "7d_prev": ((cutoff_dt - timedelta(days=13)).isoformat(), (cutoff_dt - timedelta(days=7)).isoformat()),
        "30d_prev": ((cutoff_dt - timedelta(days=59)).isoformat(), (cutoff_dt - timedelta(days=30)).isoformat()),
        "90d_prev": ((cutoff_dt - timedelta(days=179)).isoformat(), (cutoff_dt - timedelta(days=90)).isoformat()),
    }

    def fetch_funnel(pkey, d_ini, d_fim):
        fg_sql = with_window(queries["FUNNEL_GOOGLE"], d_ini, d_fim).replace("{{CUTOFF}}", cutoff)
        fg = [dict(normalize_fg_row(r), p_key=pkey) for r in run(f"FUNNEL_GOOGLE[{pkey}]", fg_sql)]
        fm_sql = with_window(queries["FUNNEL_META"], d_ini, d_fim).replace("{{CUTOFF}}", cutoff)
        fm = [dict(normalize_fm_row(r), p_key=pkey) for r in run(f"FUNNEL_META[{pkey}]", fm_sql)]
        return fg, fm

    fresh_fg_all, fresh_fm_all = [], []
    for pkey, (d_ini, d_fim) in windows.items():
        fg, fm = fetch_funnel(pkey, d_ini, d_fim)
        fresh_fg_all.extend(fg)
        fresh_fm_all.extend(fm)

    fresh_fg_month = [r for r in fresh_fg_all if r.get("p_key") == curr_month_key]
    fresh_fm_month = [r for r in fresh_fm_all if r.get("p_key") == curr_month_key]

    fresh_prev_fg, fresh_prev_fm = [], []
    for pkey, (d_ini, d_fim) in prev_windows.items():
        fg, fm = fetch_funnel(pkey, d_ini, d_fim)
        fresh_prev_fg.extend(fg)
        fresh_prev_fm.extend(fm)

    # CREDIT_TIMESERIES
    credit_sql = queries["CREDIT_TIMESERIES"].replace("{{CUTOFF}}", cutoff)
    fresh_credit = [dict(r, semana=norm_date(r["semana"])) for r in run("CREDIT_TIMESERIES", credit_sql)]

    # PARTNER_WEEKLY
    weekly_sql = (queries["PARTNER_WEEKLY"]
                  .replace("{{CUTOFF}}", cutoff)
                  .replace("{{WEEKLY_START}}", weekly_start))
    fresh_weekly = [dict(r, semana=norm_date(r["semana"])) for r in run("PARTNER_WEEKLY", weekly_sql)]

    # ── merge (idêntico ao refresh_step3.py original) ─────────────────────

    fresh_daily_keys = {(r["dia"], r["id_mp"], r["canal"]) for r in fresh_daily}
    old_daily = [r for r in cache.get("daily_snapshot", [])
                 if (r["dia"], r["id_mp"], r["canal"]) not in fresh_daily_keys]
    all_daily = sorted(old_daily + fresh_daily, key=lambda x: x["dia"])
    prune_daily = (cutoff_dt - timedelta(days=180)).isoformat()
    all_daily = [r for r in all_daily if r["dia"] >= prune_daily]

    fresh_dfg_keys = {(r["dia"], r["id_mp"]) for r in fresh_dfg}
    old_dfg = [r for r in cache.get("daily_funnel_google", []) if (r["dia"], r["id_mp"]) not in fresh_dfg_keys]
    all_dfg = sorted(old_dfg + fresh_dfg, key=lambda x: x["dia"])
    all_dfg = [r for r in all_dfg if r["dia"] >= prune_daily]

    fresh_dfm_keys = {(r["dia"], r["id_mp"]) for r in fresh_dfm}
    old_dfm = [r for r in cache.get("daily_funnel_meta", []) if (r["dia"], r["id_mp"]) not in fresh_dfm_keys]
    all_dfm = sorted(old_dfm + fresh_dfm, key=lambda x: x["dia"])
    all_dfm = [r for r in all_dfm if r["dia"] >= prune_daily]

    daily_funnel_compact = build_compact_daily_funnel(all_dfg, all_dfm)

    d = cutoff_dt
    curr_month_ini = date(d.year, d.month, 1).isoformat()
    # Fix 2026-07-12: 7d/30d/90d aqui reusam `windows`/`prev_windows` (em vez de
    # recalcular com tamanho próprio) — antes cobriam 8/31/91 dias enquanto
    # FUNNEL_GOOGLE/META (mesmo period_key) cobriam 7/30/90, distorcendo CPL/CAC
    # no dashboard (SNAPSHOT e FUNNEL_* são combinados pelo mesmo period_key).
    periods_snap = {
        "7d": windows["7d"],
        "30d": windows["30d"],
        "90d": windows["90d"],
        curr_month_key: (curr_month_ini, d.isoformat()),
        **prev_windows,
    }
    # Recomputar também todos os meses fechados presentes no HTML (mesma motivação
    # descrita em `windows` acima — não deixar mês antigo com foto do dia da virada).
    for mk, (m_ini, m_fim) in windows.items():
        if mk in periods_snap or not re.fullmatch(r"\d{4}-\d{2}", mk):
            continue
        periods_snap[mk] = (m_ini, m_fim)
    prev_keys = {"7d_prev", "30d_prev", "90d_prev"}
    snap_fresh, prev_snap = [], []
    for pkey, (d_ini, d_fim) in periods_snap.items():
        rows = agg_daily(all_daily, d_ini, d_fim, pkey)
        (prev_snap if pkey in prev_keys else snap_fresh).extend(rows)

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # Excluir das listas históricas todas as chaves que foram recomputadas neste
    # refresh (rolling + mês corrente + todos os meses fechados de `windows`), para
    # o fresh sobrescrever. Ver comentário em `windows` sobre o fix de congelamento.
    refreshed_keys = ROLLING_KEYS | set(windows.keys())
    existing_snap = extract_js_array(html, "SNAPSHOT")
    historical_snap = [r for r in existing_snap if r.get("period_key") not in refreshed_keys]
    full_snapshot = historical_snap + snap_fresh

    existing_fg = extract_js_array(html, "FUNNEL_GOOGLE")
    existing_fm = extract_js_array(html, "FUNNEL_META")
    hist_fg = [r for r in existing_fg if r.get("p_key") not in refreshed_keys]
    hist_fm = [r for r in existing_fm if r.get("p_key") not in refreshed_keys]
    full_fg = hist_fg + fresh_fg_all
    full_fm = hist_fm + fresh_fm_all

    cutoff_70 = (cutoff_dt - timedelta(days=70)).isoformat()

    fresh_weekly_keys = {(r["semana"], r["id_mp"]) for r in fresh_weekly}
    old_weekly = [r for r in cache.get("partner_weekly", [])
                  if (r["semana"], r["id_mp"]) not in fresh_weekly_keys]
    all_weekly = sorted(old_weekly + fresh_weekly, key=lambda x: x["semana"])
    all_weekly_70 = [r for r in all_weekly if r["semana"] >= cutoff_70]

    partner_weekly_dict = {}
    for row in all_weekly_70:
        partner_weekly_dict.setdefault(row["id_mp"], []).append({
            "ws": row["semana"],
            "bruto": round(row.get("bruto") or 0),
            "cashback": round(row.get("cashback") or 0),
            "liquido": round(row.get("liquido") or 0),
            "cliques_g": row.get("cliques_g", 0) or 0,
            "cliques_m": row.get("cliques_m", 0) or 0,
            "clickoff_g": row.get("clickoff_g", 0) or 0,
            "chat_start_m": row.get("chat_start_m", 0) or 0,
            "leads_g": row.get("leads_g", 0) or 0,
            "vendas_g": row.get("vendas_g", 0) or 0,
            "leads_m": row.get("leads_m", 0) or 0,
            "vendas_m": row.get("vendas_m", 0) or 0,
        })

    fresh_credit_keys = {(r["semana"], r["id_mp"]) for r in fresh_credit}
    old_credit = [r for r in cache.get("credit_timeseries", [])
                  if (r["semana"], r["id_mp"]) not in fresh_credit_keys]
    all_credit = sorted(old_credit + fresh_credit, key=lambda x: x["semana"])
    all_credit_70 = [r for r in all_credit if r["semana"] >= cutoff_70]

    credit_dict = {}
    for row in all_credit_70:
        credit_dict.setdefault(row["id_mp"], []).append({
            "dia": row["semana"],
            "credito": round(row.get("credito") or 0),
            "total": round(row.get("total") or 0),
        })

    daily_compact = build_compact_daily(all_daily)

    html = sub_array(html, "SNAPSHOT", full_snapshot)
    html = sub_array(html, "PREV_SNAPSHOT", prev_snap)
    html = sub_array(html, "FUNNEL_GOOGLE", full_fg)
    html = sub_array(html, "FUNNEL_META", full_fm)
    html = sub_array(html, "PREV_FUNNEL_GOOGLE", fresh_prev_fg)
    html = sub_array(html, "PREV_FUNNEL_META", fresh_prev_fm)
    html = ensure_ds_partners(html)
    html = sub_array(html, "DAILY_SNAPSHOT", daily_compact)
    html = sub_array(html, "DAILY_FUNNEL", daily_funnel_compact)
    html = sub_obj(html, "PARTNER_WEEKLY", partner_weekly_dict)
    html = sub_obj(html, "CREDIT_TIMESERIES", credit_dict)
    html = re.sub(r'const SNAPSHOT_ISO\s*=\s*"[^"]*"',
                  f'const SNAPSHOT_ISO   = "{cutoff}T08:00:00-03:00"', html)
    html = re.sub(r'const SNAPSHOT_COVER\s*=\s*"[^"]*"',
                  f'const SNAPSHOT_COVER = "{cover}"', html)
    html = re.sub(r"ℹ Snapshot [^<\"]+", f"ℹ Snapshot {cover}", html)

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    # ── cache ─────────────────────────────────────────────────────────────

    prune_before_80 = (cutoff_dt - timedelta(days=80)).isoformat()
    cache["daily_snapshot"] = [r for r in all_daily if r["dia"] >= prune_daily]
    cache["daily_funnel_google"] = all_dfg
    cache["daily_funnel_meta"] = all_dfm

    months = cache.get("months", {})
    months.setdefault(curr_month_key, {})
    months[curr_month_key]["funnel_google"] = fresh_fg_month
    months[curr_month_key]["funnel_meta"] = fresh_fm_month

    # Antes marcávamos o mês anterior como "frozen" aqui — agora ele é recomputado
    # a cada refresh (ver windows/periods_snap), então não faz sentido travar.
    cache["months"] = months

    cache["partner_weekly"] = [r for r in all_weekly if r["semana"] >= prune_before_80]
    cache["credit_timeseries"] = [r for r in all_credit if r["semana"] >= prune_before_80]
    cache["last_run_month"] = curr_month_key

    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))

    print(f"OK snap={len(full_snapshot)}rows fg={len(full_fg)}rows fm={len(full_fm)}rows "
          f"daily_compact={len(daily_compact)}rows html={len(html)//1024}KB")

    # ── análise IA (opcional — nunca derruba o refresh) ──────────────────

    analysis_block = ""
    if ai_analysis.enabled():
        try:
            payload = ai_analysis.build_payload(
                all_daily, all_dfg, all_dfm, partner_weekly_dict,
                cutoff_dt, VALID_PARTNERS)
            result = ai_analysis.generate(payload, cover)
            with open(ANALYSIS_PATH, "w", encoding="utf-8") as f:
                f.write(ai_analysis.render_page(result["relatorio_html"], cover, cutoff,
                                                model=result.get("_model")))
            analysis_url = (f"{PAGES_URL.rstrip('/')}/analise.html?v={cutoff}"
                            if PAGES_URL else "analise.html")
            analysis_block = (f"\n🤖 *Análise IA da semana:*\n{result['resumo_slack']}\n"
                              f"📄 Relatório completo: {analysis_url}\n")
            print(f"Análise IA ok ({len(result['relatorio_html'])//1024}KB de relatório).")
        except Exception as e:
            print(f"Aviso: análise IA falhou ({type(e).__name__}: {e}) — refresh segue sem ela.",
                  file=sys.stderr)
            analysis_block = "\n_⚠️ Análise IA indisponível nesta semana._\n"
    else:
        print("LLM_API_KEY ausente — análise IA pulada.")

    # ── Slack ─────────────────────────────────────────────────────────────

    dashboard_url = f"{PAGES_URL}?v={cutoff}" if PAGES_URL else None
    link_line = f"\n{dashboard_url}\n" if dashboard_url else "\n"
    prefix = "🧪 [TESTE — só você vê isso]\n" if TEST_MODE else ""
    channel_mention = "" if TEST_MODE else "\n<!channel>"
    slack_post(
        f"{prefix}📊 Dashboard MP Agência — Funil Ads-to-Sale ({cover})\n"
        f"Dados atualizados com snapshot de {cover}. Acesse o dashboard interativo:"
        f"{link_line}{analysis_block}{channel_mention}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            prefix = "🧪 [TESTE] " if TEST_MODE else ""
            slack_post(f"{prefix}⚠️ Problema no refresh automático do dashboard MP Agência: {e} "
                       f"{SLACK_MENTION_ON_ERROR} verifica?")
        except Exception:
            pass
        print(f"ERRO: {e}", file=sys.stderr)
        sys.exit(1)
