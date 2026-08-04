#!/usr/bin/env python3
"""Análise IA semanal — pipeline de 3 estágios.

Porte 1:1 do pipeline criado originalmente na versão Streamlit do dashboard
(`mp-agencia-dashboard-streamlit/scripts/ai_analysis.py`, 2026-07) — mesmos
processamentos, mesmos prompts, mesma estrutura de 3 estágios. Adaptado só na
saída: aqui não existe app Streamlit lendo JSON, então o resultado vira HTML
publicado (`docs/analise.html`) + mensagem/thread do Slack, via `render_page()`
e `render_cac_slack()` no fim deste arquivo (que não existem na versão
original). Roda dentro do weekly_refresh (GitHub Actions, terça), nunca em
runtime — é a mesma keep-out-of-request-path da versão Streamlit.

  Estágio 0 — triagem      · Python puro. Limiares são numéricos e absolutos;
                             comparar número com limiar é o que LLM faz pior.
  Estágio 1 — diagnóstico  · LLM com responseSchema. Julgamento estruturado.
  Estágio 2 — redação      · LLM com tags XML. Recebe SÓ o JSON do estágio 1,
                             nunca o payload — sem números crus na mão, recitar
                             o dashboard fica impossível.

O JSON do estágio 1 é persistido em data/diagnosticos/AAAA-MM-DD.json (commitado
pelo workflow, ver weekly-refresh.yml): é a entrada do estágio 2 da semana
seguinte (bloco "recomendei X, não mexeu") e o registro histórico auditável.

Config por env var (secrets do repo):
  LLM_API_KEY  — sem ela a análise é pulada em silêncio, o refresh segue.
  LLM_MODEL    — opcional, default gemini-3.1-pro-preview.

Nota: esta versão é Gemini-only (usa responseSchema + thinkingConfig, recursos
específicos da API Gemini) — a versão anterior deste arquivo era agnóstica de
provedor (LLM_PROVIDER gemini|openai|anthropic). Decisão consciente ao portar:
Pedro já fixou Gemini AI Studio pago como provedor (ver memória
mp-dashboard-ai-analysis), e o pipeline de 3 estágios não existe sem
responseSchema.
"""
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

import requests

DEFAULT_MODEL = "gemini-3.1-pro-preview"
REQUEST_TIMEOUT = 300
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def enabled():
    return bool(os.environ.get("LLM_API_KEY"))


def _model():
    return (os.environ.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL


# ── payload ───────────────────────────────────────────────────────────────

def _window_bounds(cutoff_dt):
    d = cutoff_dt
    return {
        "7d": ((d - timedelta(days=6)).isoformat(), d.isoformat()),
        "7d_prev": ((d - timedelta(days=13)).isoformat(), (d - timedelta(days=7)).isoformat()),
        "30d": ((d - timedelta(days=29)).isoformat(), d.isoformat()),
        "30d_prev": ((d - timedelta(days=59)).isoformat(), (d - timedelta(days=30)).isoformat()),
        "mes_atual": (d.replace(day=1).isoformat(), d.isoformat()),
    }


def _ratio(num, den):
    return round(num / den, 2) if den else None


def _week_start(dia_iso):
    """Segunda-feira da semana do dia — mesma convenção do DATE_TRUNC('week') do Redshift."""
    d = date.fromisoformat(dia_iso)
    return (d - timedelta(days=d.weekday())).isoformat()


def weekly_series(all_daily, all_dfg, all_dfm, cutoff_dt, valid_partners, n_weeks=8):
    """Série semanal por partner × canal, derivada do dado DIÁRIO.

    Substitui o PARTNER_WEEKLY como fonte da série (fix 2026-07-24). Três motivos:
    - o dict do PARTNER_WEEKLY traz `leads_g`/`leads_m`, não `leads` — o
      _trend_summary lia um campo inexistente e devolvia sem_base/0 pra TODO
      partner desde sempre. A tendência nunca funcionou em produção.
    - só tem verba no nível do partner, então CPL/CAC por canal eram impossíveis;
      e não tem impressões, então CTR por semana também não existia — mas o prompt
      mandava ancorar tendência de CTR e impressões nesse campo. O modelo só podia
      inventar.
    - o resto do payload já sai do DAILY_SNAPSHOT/DAILY_FUNNEL_*; derivar daqui
      elimina divergência de atribuição entre a série e as janelas.

    Semanas incompletas são descartadas: o run é terça com cutoff = segunda, então
    o bucket mais recente teria 1 dia só e puxaria toda tendência para "queda".
    """
    fim_ultima_completa = cutoff_dt - timedelta(days=cutoff_dt.weekday() + 1)  # domingo anterior
    ini = (fim_ultima_completa - timedelta(days=7 * n_weeks - 1)).isoformat()
    fim = fim_ultima_completa.isoformat()
    semanas = sorted({_week_start(d) for d in (
        (fim_ultima_completa - timedelta(days=7 * i)).isoformat() for i in range(n_weeks))})

    agg = defaultdict(lambda: defaultdict(float))  # (partner, canal, ws) -> campos
    for r in all_daily:
        if r["id_mp"] in valid_partners and ini <= r["dia"] <= fim:
            for canal in (r["canal"], "total"):
                k = (r["id_mp"], canal, _week_start(r["dia"]))
                for f in ("liquido", "leads", "vendas"):
                    agg[k][f] += r.get(f) or 0
    # etapas do funil também entram na série: sem elas não dá pra dizer QUAL etapa
    # moveu o CAC de uma semana pra outra, que é o "isso aconteceu porque a taxa de
    # redirect>lead melhorou" que o time de mídia pediu.
    for rows, canal in ((all_dfg, "google"), (all_dfm, "meta")):
        campos = ("cliques", "impressoes") + STAGE_COLS[canal]
        for r in rows:
            if r["id_mp"] in valid_partners and ini <= r["dia"] <= fim:
                for c in (canal, "total"):
                    k = (r["id_mp"], c, _week_start(r["dia"]))
                    for f in campos:
                        agg[k][f] += r.get(f) or 0
                    # atribuição do funil (por campanha/chat) é diferente da do
                    # snapshot (por partner_id_partner). A taxa redirect>leads
                    # tem que usar a MESMA base do redirect, senão passa de 100%.
                    agg[k]["leads_funil"] += r.get("leads") or 0

    out = {}
    for p in valid_partners:
        for canal in ("total", "google", "meta"):
            serie = []
            for ws in semanas:
                v = agg.get((p, canal, ws), {})
                liq = round(v.get("liquido", 0))
                leads, vendas = int(v.get("leads", 0)), int(v.get("vendas", 0))
                cliques, impr = int(v.get("cliques", 0)), int(v.get("impressoes", 0))
                sem = {
                    "ws": ws, "liquido": liq, "leads": leads, "vendas": vendas,
                    "cliques": cliques, "impressoes": impr,
                    "cpl": _ratio(liq, leads), "cac": _ratio(liq, vendas),
                    "ctr_pct": round(100 * cliques / impr, 2) if impr else None,
                    "cpc": _ratio(liq, cliques),
                }
                sem["leads_funil"] = int(v.get("leads_funil", 0))
                if canal in STAGE_DEFS_WEEK:
                    for de, para in STAGE_DEFS_WEEK[canal]:
                        d_val = cliques if de == "cliques" else int(v.get(de, 0))
                        p_val = (sem["leads_funil"] if para == "leads"
                                 else int(v.get(para, 0)))
                        sem[f"taxa_{de}>{para}"] = (round(100 * p_val / d_val, 1)
                                                    if d_val else None)
                sem["taxa_lead_venda"] = round(100 * vendas / leads, 1) if leads else None
                serie.append(sem)
            if any(s["liquido"] or s["leads"] or s["cliques"] for s in serie):
                out.setdefault(p, {})[canal] = serie
    return out


# Só as etapas que existem APENAS no funil. `leads`/`vendas` NÃO entram aqui:
# já vêm do DAILY_SNAPSHOT e somar de novo dobrava o número (bug pego em
# 2026-07-27 — a taxa redirect>leads dava 259%, e as vendas semanais da Ultranet
# apareciam como 8 quando eram 4).
STAGE_COLS = {
    "google": ("sessoes", "clickoff", "redirect"),
    "meta": ("chat_start", "zip_search", "redirect"),
}
STAGE_DEFS_WEEK = {
    "google": (("cliques", "sessoes"), ("sessoes", "clickoff"),
               ("clickoff", "redirect"), ("redirect", "leads")),
    "meta": (("cliques", "chat_start"), ("chat_start", "zip_search"),
             ("zip_search", "redirect"), ("redirect", "leads")),
}

TREND_FIELDS = ("leads", "vendas", "cpl", "cac", "cliques", "ctr_pct")


def _trend_summary(serie, field):
    """Direção da série de 8 semanas + estabilidade recente, com baseline por MEDIANA.

    Mediana e não média (fix 2026-07-24): com contagem baixa de vendas, um pico
    único destrói a média e contamina tudo que se compara contra ela.
    Semanas sem base para a métrica (ex.: CAC numa semana sem venda) entram como
    None e são ignoradas, em vez de virar zero e fabricar uma queda.
    """
    vals = [s.get(field) for s in serie]
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n < 4:
        return None
    half = n // 2
    m1, m2 = median(vals[:half]), median(vals[half:])
    delta_pct = round(100 * (m2 - m1) / m1, 1) if m1 else None
    if delta_pct is None:
        direcao = "sem_base"
    elif delta_pct >= 15:
        direcao = "alta"
    elif delta_pct <= -15:
        direcao = "queda"
    else:
        direcao = "estavel"
    base = median(vals)
    estaveis = 0
    for v in reversed(vals):
        if base and abs(v - base) / base <= 0.15:
            estaveis += 1
        else:
            break
    fmt, quebra = _formato(serie, field, vals, base, direcao)
    return {
        "direcao_8sem": direcao,
        "variacao_pct_1a_vs_2a_metade": delta_pct,
        "semanas_estaveis_consecutivas": estaveis,
        "mediana_8sem": round(base, 2),
        "semanas_com_base": n,
        "formato": fmt,
        "semana_da_quebra": quebra,
        "desvio_atual_vs_mediana_pct": (round(100 * (vals[-1] - base) / base, 1)
                                        if base else None),
    }


# Limiares da classificação de formato. Chute educado do desenho, rodado contra o
# histórico real de 8 semanas dos 8 partners (2026-07-24): os valores em si
# aguentaram, o que precisou de conserto foi a regra de gradual_* (ver abaixo).
# Recalibrar aqui se a classificação começar a discordar do que o time vê no
# dashboard — o desenho lista isso como pendência em aberto.
FMT_SALTO = 0.30      # salto entre semanas consecutivas que caracteriza degrau
FMT_PLATO = 0.15      # dispersão máxima depois do salto pra ainda ser platô
FMT_MONOTONIA = 5     # transições (de 7) na mesma direção pra ser gradual
FMT_RUIDO = 0.40      # desvio-padrão relativo acima disso, sem direção, é ruído


def _formato(serie, field, vals, base, direcao):
    """Classifica o FORMATO da curva, não só a direção.

    `direcao_8sem` compara metade com metade, o que torna indistinguíveis dois
    casos de diagnóstico oposto:
    - degrau: caiu de uma vez e ficou parado → algo mudou numa data específica
      (campanha pausada, verba cortada, segmentação alterada, rastreio quebrado).
      A ação é descobrir o que mudou naquela semana.
    - gradual_queda: cai um pouco toda semana → fadiga de criativo ou leilão
      encarecendo. A ação é rotacionar/relançar.
    O formato É o diagnóstico.
    """
    if len(vals) < 5 or not base:
        return "sem_base", None
    # degrau: um salto grande seguido de platô
    for i in range(1, len(vals) - 1):
        ant, cur = vals[i - 1], vals[i]
        if not ant or abs(cur - ant) / abs(ant) < FMT_SALTO:
            continue
        depois = vals[i:]
        pico = max(abs(v) for v in depois) or 1
        if (max(depois) - min(depois)) / pico <= FMT_PLATO:
            # a semana do salto, no índice da série que tem valor pra esta métrica
            ws = [s["ws"] for s in serie if s.get(field) is not None]
            return "degrau", ws[i] if i < len(ws) else None
    # gradual: maioria das transições na mesma direção E deslocamento acumulado
    # que valha a pena. Sem o teste de magnitude, uma curva que oscila ±2% com 5
    # descidas e 2 subidas virava "gradual_queda" — monotonia sem tamanho não é
    # tendência, é ruído com sorte.
    deltas = [b - a for a, b in zip(vals, vals[1:])]
    sobe = sum(1 for d in deltas if d > 0)
    desce = sum(1 for d in deltas if d < 0)
    # gradual_* é um REFINAMENTO da direção, então tem que sair da mesma base que
    # ela (mediana da 1ª metade vs 2ª). Medir o deslocamento por primeiro-vs-último
    # ponto dava contradição dentro do mesmo objeto — "gradual_alta" com
    # "direcao_8sem: queda" — e os dois campos vão juntos pro modelo ler.
    if sobe >= FMT_MONOTONIA and direcao == "alta":
        return "gradual_alta", None
    if desce >= FMT_MONOTONIA and direcao == "queda":
        return "gradual_queda", None
    media = sum(vals) / len(vals)
    if media:
        dp = (sum((v - media) ** 2 for v in vals) / len(vals)) ** 0.5
        if dp / abs(media) > FMT_RUIDO:
            return "ruidoso", None
    return "estavel", None


def build_payload(all_daily, all_dfg, all_dfm, cutoff_dt, valid_partners):
    windows = _window_bounds(cutoff_dt)

    # investimento/leads/vendas por partner × canal × janela (do DAILY_SNAPSHOT).
    # "conta" = os dois canais somados. É o nível em que as metas de CAC/CPL estão
    # definidas e em que a triagem julga: com o volume de vendas dessas contas, o
    # mínimo de base quase nunca é atingido POR CANAL, e sem o agregado uma conta
    # com CAC de R$986 caía em "sem_base" e sumia do topo da fila.
    invest = {}
    for wkey, (d_ini, d_fim) in windows.items():
        agg = defaultdict(lambda: defaultdict(float))
        for r in all_daily:
            if r["id_mp"] in valid_partners and d_ini <= r["dia"] <= d_fim:
                for canal in (r["canal"], "conta"):
                    for f in ("bruto", "cashback", "liquido", "leads", "vendas"):
                        agg[(r["id_mp"], canal)][f] += r.get(f) or 0
        for (id_mp, canal), v in agg.items():
            bruto, liq = round(v["bruto"]), round(v["liquido"])
            leads, vendas = int(v["leads"]), int(v["vendas"])
            invest.setdefault(id_mp, {}).setdefault(canal, {})[wkey] = {
                "investimento_bruto": bruto,
                "investimento_liquido": liq,
                "cashback": round(v["cashback"]),
                "pct_cashback": round(100 * v["cashback"] / v["bruto"], 1) if v["bruto"] else None,
                "leads": leads,
                "vendas": vendas,
                "cpl": _ratio(liq, leads),
                "cac": _ratio(liq, vendas),
            }

    # etapas do funil por partner × canal × janela (7d vs prev, 30d)
    def agg_funnel(rows, fields, wkeys):
        out = {}
        for wkey in wkeys:
            d_ini, d_fim = windows[wkey]
            agg = defaultdict(lambda: defaultdict(int))
            for r in rows:
                if r["id_mp"] in valid_partners and d_ini <= r["dia"] <= d_fim:
                    for f in fields:
                        agg[r["id_mp"]][f] += r.get(f) or 0
            for id_mp, v in agg.items():
                out.setdefault(id_mp, {})[wkey] = dict(v)
        return out

    fg_fields = ("impressoes", "cliques", "sessoes", "clickoff", "redirect", "leads", "vendas")
    fm_fields = ("impressoes", "cliques", "chat_start", "zip_search", "redirect", "leads", "vendas")
    wkeys = ("7d", "7d_prev", "30d", "30d_prev")
    funil_google = agg_funnel(all_dfg, fg_fields, wkeys)
    funil_meta = agg_funnel(all_dfm, fm_fields, wkeys)

    # métricas de pré-clique derivadas aqui (LLM não faz aritmética confiável).
    # cpc_estimado usa investimento bruto (financeiro) / cliques (plataforma de ads):
    # bases diferentes do gerenciador — serve pra tendência, não pra auditoria.
    for funil, canal in ((funil_google, "google"), (funil_meta, "meta")):
        for id_mp, per_window in funil.items():
            for wkey, v in per_window.items():
                v["ctr_pct"] = (round(100 * v["cliques"] / v["impressoes"], 2)
                                if v.get("impressoes") else None)
                bruto = (invest.get(id_mp, {}).get(canal, {}).get(wkey, {})
                         .get("investimento_bruto"))
                v["cpc_estimado"] = _ratio(bruto, v["cliques"]) if bruto else None

    # taxas de passagem entre etapas do meio de funil, calculadas AQUI pelo mesmo
    # motivo do ctr/cpc: o LLM ignora "cheque a etapa X" quando só tem contagens
    # brutas — precisa da taxa já pronta pra apontar o gargalo específico
    # (em vez de cair sempre no diagnóstico genérico de cashback/CTR).
    STAGE_DEFS = {
        "google": (("cliques", "sessoes"), ("sessoes", "clickoff"),
                   ("clickoff", "redirect"), ("redirect", "leads")),
        "meta": (("cliques", "chat_start"), ("chat_start", "zip_search"),
                 ("zip_search", "redirect"), ("redirect", "leads")),
    }
    for funil, canal in ((funil_google, "google"), (funil_meta, "meta")):
        for per_window in funil.values():
            for v in per_window.values():
                taxas = {}
                for de, para in STAGE_DEFS[canal]:
                    label = f"{de}>{para}"
                    taxas[label] = (round(100 * v[para] / v[de], 1)
                                     if v.get(de) else None)
                v["taxas_etapa"] = taxas

    # série semanal — 8 últimas semanas COMPLETAS, por partner × canal, derivada do
    # diário. Crédito/runway fica FORA do payload de propósito (decisão do Pedro
    # 2026-07-09): já existem alertas dedicados e, se o dado estiver aqui, o modelo
    # desvia o parecer pra isso.
    series = weekly_series(all_daily, all_dfg, all_dfm, cutoff_dt, valid_partners)

    # série crua enxuta (5 campos) só no nível do partner: rede de segurança pro
    # modelo perceber caso de borda que a classificação de tendência errou. Os
    # derivados por semana (cpl/cac/ctr) ficam só na tendência, pra não inchar.
    semanal = {p: [{k: s[k] for k in ("ws", "liquido", "leads", "vendas", "cliques")}
                   for s in canais["total"]]
               for p, canais in series.items() if "total" in canais}

    # tendência calculada aqui — o LLM não extrai de forma confiável "tendência
    # real vs ruído de 1 semana" de uma lista crua de 8 números.
    tendencia_semanal = {}
    for p, canais in series.items():
        for canal, serie in canais.items():
            t = {f: _trend_summary(serie, f) for f in TREND_FIELDS}
            tendencia_semanal.setdefault(p, {})[canal] = {k: v for k, v in t.items() if v}

    # benchmark de pré-clique 30d — comparação com os pares calculada AQUI.
    # Sem isso o modelo ignora ctr/cpc mesmo com instrução explícita (testado
    # em 2026-07-09): LLM comenta o que está saliente nos dados, não o que a
    # instrução manda procurar.
    benchmark = {}
    for canal, funil in (("google", funil_google), ("meta", funil_meta)):
        stats = {id_mp: pw["30d"] for id_mp, pw in funil.items()
                 if pw.get("30d", {}).get("ctr_pct") is not None}
        for id_mp, w in stats.items():
            peers_ctr = [v["ctr_pct"] for k, v in stats.items() if k != id_mp]
            peers_cpc = [v["cpc_estimado"] for k, v in stats.items()
                         if k != id_mp and v.get("cpc_estimado")]
            entry = {"ctr_pct_30d": w["ctr_pct"], "cpc_estimado_30d": w.get("cpc_estimado")}
            if peers_ctr:
                media = sum(peers_ctr) / len(peers_ctr)
                entry["ctr_media_outros_partners"] = round(media, 2)
                if media:
                    entry["ctr_vs_pares_pct"] = round(100 * (w["ctr_pct"] - media) / media, 1)
            if peers_cpc and w.get("cpc_estimado"):
                media = sum(peers_cpc) / len(peers_cpc)
                entry["cpc_media_outros_partners"] = round(media, 2)
                entry["cpc_vs_pares_pct"] = round(100 * (w["cpc_estimado"] - media) / media, 1)
            benchmark.setdefault(id_mp, {})[canal] = entry

    # gargalo_funil_30d: aponta a ETAPA mais fraca do meio de funil de cada
    # partner×canal (vs a própria janela anterior e vs a média dos pares),
    # já pré-selecionada — mesmo racional do benchmark_pre_clique_30d. Sem
    # isso o modelo só cruza cashback/CTR (os únicos dados já mastigados) e
    # nunca chega no diagnóstico de funil descrito em <como_pensar>.
    gargalo_funil = {}
    for canal, funil in (("google", funil_google), ("meta", funil_meta)):
        stage_labels = [f"{de}>{para}" for de, para in STAGE_DEFS[canal]]
        stats_30d = {id_mp: pw["30d"]["taxas_etapa"] for id_mp, pw in funil.items()
                     if pw.get("30d", {}).get("taxas_etapa")}
        for id_mp, taxas in stats_30d.items():
            taxas_prev = funil[id_mp].get("30d_prev", {}).get("taxas_etapa", {})
            pior_label, pior_desvio, pior_detalhe = None, None, None
            for label in stage_labels:
                taxa = taxas.get(label)
                if taxa is None:
                    continue
                peers = [t[label] for k, t in stats_30d.items()
                         if k != id_mp and t.get(label) is not None]
                desvio_pares = None
                if peers:
                    media_pares = sum(peers) / len(peers)
                    if media_pares:
                        desvio_pares = round(100 * (taxa - media_pares) / media_pares, 1)
                taxa_prev = taxas_prev.get(label)
                delta_hist = (round(100 * (taxa - taxa_prev) / taxa_prev, 1)
                              if taxa_prev else None)
                # pior sinal = menor entre desvio vs pares e delta vs histórico
                candidatos = [d for d in (desvio_pares, delta_hist) if d is not None]
                if not candidatos:
                    continue
                pior_deste = min(candidatos)
                if pior_desvio is None or pior_deste < pior_desvio:
                    pior_desvio = pior_deste
                    pior_label = label
                    pior_detalhe = {
                        "etapa": label, "taxa_30d_pct": taxa,
                        "taxa_media_pares_pct": round(media_pares, 1) if peers else None,
                        "desvio_vs_pares_pct": desvio_pares,
                        "taxa_30d_prev_pct": taxa_prev,
                        "delta_vs_historico_pct": delta_hist,
                    }
            if pior_detalhe:
                gargalo_funil.setdefault(id_mp, {})[canal] = pior_detalhe

    # dias_de_dados: do primeiro dia com investimento BRUTO > 0 até o corte.
    # Satura na poda de 180 dias do all_daily — só importa pro teste de ramp (<14),
    # onde a poda nunca alcança.
    #
    # dias_desde_ultimo_investimento: do ÚLTIMO dia com investimento BRUTO > 0
    # até o corte (2026-07-28, achado real: Unifique). Sem isso, uma conta que
    # parou de rodar há semanas ainda aparece com investimento_liquido > 0 na
    # janela móvel de 30d enquanto o último dia de gasto real estiver dentro
    # dela — e a triagem confundia isso com "dado_suspeito" (investimento
    # contínuo sem lead, rastreamento quebrado), quando o problema real é outro:
    # a campanha simplesmente parou de existir/sincronizar. Ver _escada().
    dias_de_dados = {}
    dias_desde_ultimo_investimento = {}
    for p in valid_partners:
        dias_ativos = [r["dia"] for r in all_daily
                       if r["id_mp"] == p and (r.get("bruto") or 0) > 0
                       and r["dia"] <= cutoff_dt.isoformat()]
        dias_de_dados[p] = ((cutoff_dt - date.fromisoformat(min(dias_ativos))).days + 1
                            if dias_ativos else 0)
        dias_desde_ultimo_investimento[p] = ((cutoff_dt - date.fromisoformat(max(dias_ativos))).days
                                             if dias_ativos else None)

    return {
        "data_corte": cutoff_dt.isoformat(),
        "janelas": {k: {"inicio": v[0], "fim": v[1]} for k, v in windows.items()},
        "partners": valid_partners,
        "dias_de_dados_por_partner": dias_de_dados,
        "dias_desde_ultimo_investimento_por_partner": dias_desde_ultimo_investimento,
        "kpis_por_partner_canal_janela": invest,
        "funil_google_por_partner": funil_google,
        "funil_meta_por_partner": funil_meta,
        "serie_semanal_por_partner": semanal,
        "tendencia_semanal_por_partner": tendencia_semanal,
        "comparativo_semana_vs_semana": comparativo_semanal(series),
        "benchmark_pre_clique_30d": benchmark,
        "gargalo_funil_30d": gargalo_funil,
    }



# ── comparativo semana vs semana ──────────────────────────────────────────
#
# Pedido direto do time de mídia (2026-07-27): "o CAC cresceu X% e quem mais
# contribuiu foi A, B e C" e, por provedor, "caiu em qual canal e por causa de
# quê". Tudo calculado aqui — é aritmética de decomposição, exatamente o que o
# modelo não pode fazer sem inventar número.
#
# Trava de base: com 1 venda/semana o CAC semanal de uma conta pequena vira
# ruído (uma venda a mais move 100%). Abaixo do mínimo o campo sai com
# base_suficiente=False e o prompt proíbe narrar a variação.

MIN_VENDAS_SEMANA = 3
MIN_LEADS_SEMANA = 10


def _delta_pct(atual, anterior, exige_positivo=False):
    """Variação relativa. `exige_positivo` para métricas de custo.

    Líquido = bruto − cashback pode ficar NEGATIVO quando o cashback supera o
    bruto (Ativa Telecom, 97% de cashback). Aí CPC/CPL/CAC ficam negativos e a
    variação percentual vira número sem sentido — deu "cpc -201%" no teste.
    """
    if atual is None or anterior is None or not anterior:
        return None
    if exige_positivo and (atual <= 0 or anterior <= 0):
        return None
    return round(100 * (atual - anterior) / abs(anterior), 1)


def _delta_pp(atual, anterior):
    if atual is None or anterior is None:
        return None
    return round(atual - anterior, 1)


MIN_DENOM_ETAPA = 10   # abaixo disso a taxa da etapa oscila por 1-2 eventos


def _denom_etapa(sem, de):
    return sem.get("cliques", 0) if de == "cliques" else int(sem.get(de) or 0)


def _driver(sem, prev, canal):
    """Qual alavanca mais se moveu: etapa do funil, CPC ou volume de investimento.

    Etapas são comparadas entre si em pontos percentuais (mesma unidade); CPC e
    investimento entram como candidatos separados, em variação relativa. O campo
    `principal` aponta a maior movimentação com base, e é o que o parecer deve
    citar como causa.
    """
    etapas = {}
    for de, para in STAGE_DEFS_WEEK.get(canal, ()):
        # sem base no denominador nas DUAS semanas a taxa vira ruído: com 3
        # cliques, um evento a mais move 33pp e o parecer narra isso como causa
        if min(_denom_etapa(sem, de), _denom_etapa(prev, de)) < MIN_DENOM_ETAPA:
            continue
        d = _delta_pp(sem.get(f"taxa_{de}>{para}"), prev.get(f"taxa_{de}>{para}"))
        if d is not None:
            etapas[f"{de}>{para}"] = d
    if min(sem.get("leads", 0), prev.get("leads", 0)) >= MIN_DENOM_ETAPA:
        lv = _delta_pp(sem.get("taxa_lead_venda"), prev.get("taxa_lead_venda"))
        if lv is not None:
            etapas["lead>venda"] = lv

    out = {
        "etapas_delta_pp": etapas,
        "cpc_delta_pct": _delta_pct(sem.get("cpc"), prev.get("cpc"), exige_positivo=True),
        "investimento_delta_pct": _delta_pct(sem.get("liquido"), prev.get("liquido"),
                                             exige_positivo=True),
        "cliques_delta_pct": _delta_pct(sem.get("cliques"), prev.get("cliques")),
    }
    if etapas:
        nome, valor = max(etapas.items(), key=lambda kv: abs(kv[1]))
        # etapa só vira "a causa" se moveu de verdade; senão o driver é custo/volume
        if abs(valor) >= 3.0:
            out["principal"] = {"tipo": "taxa_de_conversao", "onde": nome, "delta_pp": valor}
            return out
    cands = [("cpc", out["cpc_delta_pct"]), ("investimento", out["investimento_delta_pct"])]
    cands = [(n, v) for n, v in cands if v is not None and abs(v) >= 10]
    if cands:
        nome, valor = max(cands, key=lambda kv: abs(kv[1]))
        out["principal"] = {"tipo": nome, "onde": nome, "delta_pct": valor}
    else:
        out["principal"] = None
    return out


def _bloco_semana(sem, prev, canal):
    vendas, vendas_prev = sem["vendas"], prev["vendas"]
    leads, leads_prev = sem["leads"], prev["leads"]
    base_cac = vendas >= MIN_VENDAS_SEMANA and vendas_prev >= MIN_VENDAS_SEMANA
    base_cpl = leads >= MIN_LEADS_SEMANA and leads_prev >= MIN_LEADS_SEMANA
    return {
        "liquido": sem["liquido"], "liquido_anterior": prev["liquido"],
        "leads": leads, "leads_anterior": leads_prev,
        "vendas": vendas, "vendas_anterior": vendas_prev,
        "cac": sem["cac"], "cac_anterior": prev["cac"],
        "cac_delta_pct": _delta_pct(sem["cac"], prev["cac"], exige_positivo=True) if base_cac else None,
        "cac_base_suficiente": base_cac,
        "cpl": sem["cpl"], "cpl_anterior": prev["cpl"],
        "cpl_delta_pct": _delta_pct(sem["cpl"], prev["cpl"], exige_positivo=True) if base_cpl else None,
        "cpl_base_suficiente": base_cpl,
        "motivo_sem_base": None if base_cac else
            f"{vendas} venda(s) nesta semana e {vendas_prev} na anterior — "
            f"abaixo do mínimo de {MIN_VENDAS_SEMANA} para ler CAC semanal",
        "driver": _driver(sem, prev, canal),
    }


def comparativo_semanal(series):
    """Última semana completa vs. a anterior, no portfólio e por partner×canal."""
    partners = [p for p, c in series.items() if "total" in c and len(c["total"]) >= 2]
    if not partners:
        return None

    ws = series[partners[0]]["total"][-1]["ws"]
    ws_prev = series[partners[0]]["total"][-2]["ws"]

    tot = {"liquido": 0, "vendas": 0, "leads": 0}
    tot_prev = {"liquido": 0, "vendas": 0, "leads": 0}
    atual, anterior = {}, {}
    for p in partners:
        a, b = series[p]["total"][-1], series[p]["total"][-2]
        atual[p], anterior[p] = a, b
        for k in tot:
            tot[k] += a[k]
            tot_prev[k] += b[k]

    cac_now = _ratio(tot["liquido"], tot["vendas"])
    cac_prev = _ratio(tot_prev["liquido"], tot_prev["vendas"])

    # contribuição por contrafactual: recalcula o CAC do portfólio mantendo ESTE
    # partner na semana anterior. A diferença é o efeito dele — exato, e responde
    # "quem puxou" sem precisar de rateio arbitrário.
    contrib = []
    for p in partners:
        liq = tot["liquido"] - atual[p]["liquido"] + anterior[p]["liquido"]
        ven = tot["vendas"] - atual[p]["vendas"] + anterior[p]["vendas"]
        cac_cf = _ratio(liq, ven)
        contrib.append({
            "partner": p,
            "efeito_no_cac_portfolio": round(cac_now - cac_cf, 2)
                                        if (cac_now is not None and cac_cf is not None) else None,
            "delta_vendas": atual[p]["vendas"] - anterior[p]["vendas"],
            "delta_liquido": atual[p]["liquido"] - anterior[p]["liquido"],
        })
    contrib.sort(key=lambda c: abs(c["efeito_no_cac_portfolio"] or 0), reverse=True)

    por_partner = {}
    for p in partners:
        entrada = {"conta": _bloco_semana(atual[p], anterior[p], "total")}
        for canal in ("google", "meta"):
            serie = series[p].get(canal)
            if serie and len(serie) >= 2:
                entrada[canal] = _bloco_semana(serie[-1], serie[-2], canal)
        por_partner[p] = entrada

    return {
        "semana": ws, "semana_anterior": ws_prev,
        "portfolio": {
            "cac": cac_now, "cac_anterior": cac_prev,
            "cac_delta_pct": _delta_pct(cac_now, cac_prev, exige_positivo=True),
            "leads": tot["leads"], "leads_anterior": tot_prev["leads"],
            "vendas": tot["vendas"], "vendas_anterior": tot_prev["vendas"],
            "liquido": tot["liquido"], "liquido_anterior": tot_prev["liquido"],
            "contribuicao_por_partner": contrib,
        },
        "por_partner": por_partner,
    }


# ── estágio 0: triagem ────────────────────────────────────────────────────
#
# Regra, não julgamento. Os limiares são numéricos e absolutos — comparar número
# com limiar é o que LLM faz pior e código faz perfeito. Com a triagem aqui, ela
# para de variar de uma semana pra outra e nenhuma conta em alarme é esquecida
# porque o modelo economizou atenção.

META = {
    "CAC_IDEAL": 150,     # <= ideal · 150-200 alerta · > 200 alarme
    "CAC_ALARME": 200,
    "CPL_META": 100,      # <= meta · 100-200 alerta · > 200 alarme
    "CPL_ALARME": 200,    # 2x a meta — suposição, o Pedro definiu só o alerta em 100
    "MIN_VENDAS": 3,      # base mínima pro CAC ter significado (decisão 2026-07-24)
    "MIN_LEADS": 10,      # base mínima pro CPL
    "RAMP_DIAS": 14,
    "PAUSA_DIAS": 10,     # achado real 2026-07-28 (Unifique): sem investimento novo há
                          # mais que isso, o valor em 30d é resíduo da janela móvel, não
                          # gasto contínuo — vira "pausada", não "dado_suspeito"
}

SEV = {"alarme": 0, "atencao": 1, "dado_suspeito": 2, "pausada": 3, "sem_base": 4, "ramp": 5, "ok": 6}


def _gargalo_relevante(g):
    """False quando os dois desvios estão dentro de ±15%.

    Sem isso o campo SEMPRE existe e até a conta mais saudável do portfólio "tem
    um gargalo" — um mínimo relativo não é um problema, e o modelo patologiza a
    melhor conta da carteira.
    """
    if not g:
        return False
    return any(d is not None and d <= -15
               for d in (g.get("desvio_vs_pares_pct"), g.get("delta_vs_historico_pct")))


def _escada(liq, leads, vendas, cac, cpl, leads_7d, dias, dias_desde_ultimo_investimento):
    """A escada de julgamento: para no primeiro degrau que o dado sustenta."""
    # achado real 2026-08-04 (Unifique): quando o resíduo da janela móvel de 30d
    # zera de vez (liq chega a 0, não só "baixo"), a conta continua tão pausada
    # quanto estava com liq>0 — só que sem esse sinal ela caía direto no
    # "sem_base" genérico (pra conta que nunca teve atividade), diferente do que
    # o histórico da conta e o bom senso diziam. dias_desde_ultimo_investimento
    # não depende de liq>0, então checa isso ANTES do fallback sem_base.
    if (not leads and not leads_7d and dias_desde_ultimo_investimento is not None
            and dias_desde_ultimo_investimento > META["PAUSA_DIAS"]):
        return ("pausada",
                f"sem investimento novo há {dias_desde_ultimo_investimento} dias — a campanha "
                f"não está mais rodando" + (" (resíduo da janela móvel já zerou)" if not liq
                                            else ", o valor em 30d é resíduo da janela móvel"),
                "nenhuma")
    if not liq and not leads:
        return "sem_base", "sem investimento nem lead em 30d", "nenhuma"
    if dias < META["RAMP_DIAS"]:
        return "ramp", f"conta com {dias} dias de dados (ramp de {META['RAMP_DIAS']})", "ramp"
    if liq > 0 and not leads and not leads_7d:
        return "dado_suspeito", "investimento em 30d sem nenhum lead em 7d e 30d", "nenhuma"
    if vendas >= META["MIN_VENDAS"] and cac is not None:
        if cac <= META["CAC_IDEAL"]:
            return "ok", f"CAC 30d R${cac:.0f} dentro do ideal", "cac"
        if cac <= META["CAC_ALARME"]:
            return "atencao", f"CAC 30d R${cac:.0f} na zona de alerta (150-200)", "cac"
        return "alarme", f"CAC 30d R${cac:.0f} acima do teto de R$200", "cac"
    if leads >= META["MIN_LEADS"] and cpl is not None:
        suf = f" (só {vendas} venda(s) em 30d, CAC sem base)"
        if cpl <= META["CPL_META"]:
            return "ok", f"CPL 30d R${cpl:.0f} na meta{suf}", "cpl"
        if cpl <= META["CPL_ALARME"]:
            return "atencao", f"CPL 30d R${cpl:.0f} acima da meta de R$100{suf}", "cpl"
        return "alarme", f"CPL 30d R${cpl:.0f} acima do dobro da meta{suf}", "cpl"
    return ("sem_base",
            f"{leads} lead(s) e {vendas} venda(s) em 30d — abaixo da base mínima", "nenhuma")


def _distancia_meta_pct(base, cac, cpl):
    if base == "cac" and cac is not None:
        return round(100 * (cac - META["CAC_ALARME"]) / META["CAC_ALARME"])
    if base == "cpl" and cpl is not None:
        return round(100 * (cpl - META["CPL_META"]) / META["CPL_META"])
    return None


def triar(payload):
    """Status por partner (nível conta) + localização por canal.

    O status vem do agregado da CONTA, não do pior canal: as metas de CAC/CPL do
    Pedro são definidas nesse nível, e com esse volume de vendas o mínimo de base
    quase nunca é atingido canal a canal — julgar por canal jogava conta com CAC
    de R$986 em "sem_base". O canal serve pra localizar onde está o problema, que
    é exatamente o papel dele na escada (degrau 3 explica, não decide).
    """
    kpis = payload.get("kpis_por_partner_canal_janela", {})
    tend = payload.get("tendencia_semanal_por_partner", {})
    garg = payload.get("gargalo_funil_30d", {})
    dias_map = payload.get("dias_de_dados_por_partner", {})
    ultimo_invest_map = payload.get("dias_desde_ultimo_investimento_por_partner", {})
    partners = payload.get("partners") or list(kpis)
    out = {}

    for p in partners:
        dias = dias_map.get(p, 0)
        dias_ultimo_invest = ultimo_invest_map.get(p)
        niveis = {}
        for nivel in ("conta", "google", "meta"):
            w = kpis.get(p, {}).get(nivel, {})
            j30, j30p, j7 = w.get("30d", {}), w.get("30d_prev", {}), w.get("7d", {})
            liq = j30.get("investimento_liquido") or 0
            leads, vendas = j30.get("leads") or 0, j30.get("vendas") or 0
            cac, cpl = j30.get("cac"), j30.get("cpl")
            status, motivo, base = _escada(liq, leads, vendas, cac, cpl,
                                           j7.get("leads") or 0, dias, dias_ultimo_invest)
            niveis[nivel] = {
                "status": status, "motivo": motivo, "base_de_julgamento": base,
                "cac_30d": cac, "cpl_30d": cpl,
                "cac_30d_prev": j30p.get("cac"), "cpl_30d_prev": j30p.get("cpl"),
                "leads_30d": leads, "vendas_30d": vendas,
                "investimento_liquido_30d": round(liq),
                "distancia_meta_pct": _distancia_meta_pct(base, cac, cpl),
                "pct_cashback_30d": j30.get("pct_cashback"),
                "pct_cashback_30d_prev": j30p.get("pct_cashback"),
                "cashback_subiu": (j30.get("pct_cashback") > j30p.get("pct_cashback")
                                   if j30.get("pct_cashback") is not None
                                   and j30p.get("pct_cashback") is not None else None),
                # gargalo só conta onde há base: conta sem lead marca -100% em toda
                # etapa e viraria "gargalo grave" sendo que o problema é não ter dado
                "gargalo_relevante": (_gargalo_relevante(garg.get(p, {}).get(nivel))
                                      if base not in ("nenhuma", "ramp") else False),
            }

        conta = niveis.pop("conta")
        # canal crítico = o pior canal; empate ou nenhum problema → nenhum
        piores = sorted(niveis.items(), key=lambda kv: (SEV[kv[1]["status"]],
                                                        -(kv[1]["distancia_meta_pct"] or 0)))
        canal_critico = piores[0][0] if SEV[piores[0][1]["status"]] <= SEV["atencao"] else "nenhum"

        # risco de churn: proxy operacional pra "CAC acima de R$200 por 3 semanas
        # seguidas". CAC semanal com esse volume de vendas é ruído puro, então a
        # persistência sai da comparação entre as duas janelas de 30d.
        cac, cac_prev = conta["cac_30d"], conta["cac_30d_prev"]
        dir_leads = (tend.get(p, {}).get("total", {}).get("leads") or {}).get("direcao_8sem")
        risco_churn = bool(cac and cac_prev and cac > META["CAC_ALARME"]
                           and cac_prev > META["CAC_ALARME"]
                           and dir_leads in ("queda", "estavel"))

        out[p] = {
            "status_geral": conta["status"],
            "motivo": conta["motivo"],
            "base_de_julgamento": conta["base_de_julgamento"],
            "distancia_meta_pct": conta["distancia_meta_pct"],
            "dias_de_dados": dias,
            "conta": conta,
            "canal_critico": canal_critico,
            "canais": niveis,
            "tendencia_leads_8sem": dir_leads,
            "risco_churn": risco_churn,
            "motivo_risco_churn": ("CAC acima de R$200 em 30d e em 30d_prev, com leads em "
                                   "queda ou estáveis no piso") if risco_churn else None,
        }

    ordem = sorted(out, key=lambda p: (SEV[out[p]["status_geral"]],
                                       -(out[p]["distancia_meta_pct"] or 0)))
    return {"metas": META, "ordem_de_prioridade": ordem, "por_partner": out}


TRIAGEM_LABEL = {"ok": "OK", "atencao": "Atenção", "alarme": "Alarme",
                 "sem_base": "Sem base", "dado_suspeito": "Dado suspeito", "ramp": "Ramp",
                 "pausada": "Pausada"}


def render_triagem_html(triagem):
    """Tabela de triagem do topo do relatório — dado puro, renderizado aqui.

    Fora do LLM de propósito: assim o bloco sai do relatório com uma linha quando
    o Pedro decidir que não vale a pena, e nunca diverge do que a triagem decidiu.
    """
    linhas = []
    for p in triagem["ordem_de_prioridade"]:
        v = triagem["por_partner"][p]
        c = v["conta"]
        cac = f"R${c['cac_30d']:.0f}" if c["cac_30d"] is not None else "—"
        cpl = f"R${c['cpl_30d']:.0f}" if c["cpl_30d"] is not None else "—"
        churn = " (risco de churn)" if v["risco_churn"] else ""
        canal = v["canal_critico"] if v["canal_critico"] != "nenhum" else "—"
        linhas.append(
            f"<tr><td><strong>{p}</strong></td><td>{TRIAGEM_LABEL[v['status_geral']]}{churn}</td>"
            f"<td>{cac}</td><td>{cpl}</td><td>{c['vendas_30d']}</td><td>{canal}</td>"
            f"<td>{v['motivo']}</td></tr>")
    return ("<h2>Triagem</h2>\n<table><thead><tr><th>Partner</th><th>Status</th>"
            "<th>CAC 30d</th><th>CPL 30d</th><th>Vendas 30d</th><th>Canal crítico</th>"
            "<th>Motivo</th></tr></thead>\n<tbody>\n" + "\n".join(linhas) +
            "\n</tbody></table>\n")

# ── estágio 1: diagnóstico ────────────────────────────────────────────────

PROMPT_E1 = """Você é especialista sênior de mídia paga e cuida das contas do MP Agência (Melhor
Plano) — provedores regionais de internet que compram um pacote mensal de mídia
100% investido em campanhas.

Sua tarefa nesta etapa é DIAGNOSTICAR, não escrever. A redação do relatório
acontece em outra etapa. Aqui você produz julgamento estruturado: o que explica o
número, qual a causa provável, como validar, o que fazer.

<negocio>
Dois canais por partner:
- google — pesquisa no Google Ads.
  Funil: impressoes > cliques > sessoes > clickoff > redirect > leads > vendas
- meta — click-to-WhatsApp com bot.
  Funil: impressoes > cliques > chat_start > zip_search > redirect > leads > vendas

Cashback: quando o lead gerado pela campanha de um partner fecha com OUTRO
provedor (CEP fora da cobertura do anunciante), o anunciante recebe cashback de
reinvestimento.
- investimento_liquido = bruto - cashback. CPL e CAC já vêm sobre o líquido.
- Cashback alto NÃO é problema por si só. Ele mede COBERTURA, não desperdício:
  diz que existe demanda em CEPs onde o parceiro não atende. Se o CPL está na
  meta e o volume de leads é razoável, a campanha está saudável e o cashback é
  só o retrato da área atendida — não abra o parecer por ele e não recomende
  apertar o raio de segmentação.
- Cashback vira problema quando vem ACOMPANHADO de CPL ruim ou volume baixo de
  leads: aí a verba está comprando demanda que não converte pro anunciante.
- Quando o cashback é alto e o resto está saudável, a recomendação certa é
  revisar a ÁREA DE COBERTURA do parceiro (avaliar expandir onde há demanda),
  não cortar segmentação.
- Atribuição de lead e venda é SEMPRE ao partner anunciante, nunca ao provedor
  que recebeu o lead.

Cada partner é uma conta isolada. A verba de um partner só pode ser realocada
entre os canais e campanhas DELE. Nunca recomende mover verba entre partners.
</negocio>

<metas>
- CAC: até R$150 é o ideal · R$150-200 é zona de alerta · acima de R$200 é alarme
- CPL: até R$100 é a meta · acima disso é alerta

Metas absolutas, iguais para todos os partners. Tamanho da conta não altera
prioridade.
</metas>

<escada_de_julgamento>
Julgue cada partner descendo esta escada e pare no primeiro degrau que o dado
sustenta:

1. CAC (30d) — indicador principal.
2. CPL (30d) — use quando o CAC não tem base.
3. Etapas do funil + CPC — sempre, para EXPLICAR o CAC ou o CPL do degrau acima.

O degrau 3 nunca substitui os degraus 1 e 2 como veredito: ele é a explicação de
por que o número de cima está onde está. Parecer que fica só no degrau 3, sem
amarrar em CAC ou CPL, está incompleto.

Toda avaliação de CAC e CPL usa 30d, nunca 7d: venda tem lag de fechamento e o
CAC de 7d vem sistematicamente inflado. Use 7d apenas para topo e meio de funil
(impressões, cliques, sessões/conversas, redirects, leads).
</escada_de_julgamento>

<como_diagnosticar>
Ordem de investigação, por partner:

1. Leia "status" e "motivo" em triagem. Os limiares já foram aplicados em código
   — não recalcule nem discuta o status.

2. Se "gargalo_relevante" for true, comece por gargalo_funil_30d: é a etapa mais
   fraca do meio de funil e a de ação mais rápida. Se for false, o funil está
   dentro do normal e o problema está em outro lugar — não invente gargalo.

3. Leia o formato da série semanal antes de concluir. Formato é diagnóstico:
   - degrau -> algo mudou numa data específica: campanha pausada, verba cortada,
     segmentação alterada, rastreamento quebrado. A ação é descobrir o que mudou
     na semana indicada.
   - gradual_queda -> desgaste contínuo: fadiga de criativo ou leilão encarecendo.
   - estavel -> o número atual não é notícia desta semana, é o patamar da conta.
   - ruidoso -> volume baixo demais para ler tendência.

4. Pré-clique (ctr_pct, cpc_estimado vs pares) quando o gargalo não for de meio
   de funil. Desvio de 30% ou mais vs pares precisa aparecer no diagnóstico.

5. Cashback: só entra no diagnóstico se o CPL estiver acima da meta OU o volume
   de leads estiver baixo. Cashback alto com CPL na meta e volume razoável é
   COBERTURA, não desperdício — nesse caso, se citar, é como oportunidade de
   revisar a área atendida, nunca como problema de campanha, e nunca como
   abertura do parecer.

6. Taxa redirect>leads fraca ou taxa lead>venda fraca tem DUAS causas possíveis,
   e você precisa escolher com evidência:
   - captação desqualificada (é alavanca de mídia): volume alto + CPL abaixo da
     meta + cashback subindo -> segmentação aberta demais puxando tráfego que não
     fecha.
   - funil comercial do provedor (fora da mídia): volume normal + CPL na meta +
     cobertura ok -> atendimento ou agenda de instalação do provedor.
   Não atribua ao provedor por default.

Padrões de leitura pré-clique:
- impressões caindo + CTR estável = perda de entrega (orçamento/lance/leilão)
- CTR caindo + impressões estáveis = fadiga de criativo ou concorrente novo
- impressões subindo + CTR caindo sem ganho de cliques = segmentação aberta demais
- CPC subindo + CTR estável = leilão mais caro

Padrões por etapa:
- google: cliques ok + sessões baixas = landing ou rastreamento ·
  sessoes>clickoff fraca = oferta pouco competitiva · clickoff>redirect fraca =
  cobertura/viabilidade · redirect>leads fraca = fricção de formulário
- meta: cliques>chat_start fraca = criativo/CTA ou fricção do click-to-WhatsApp ·
  chat_start>zip_search fraca = abandono no início do bot · zip_search>redirect
  fraca = CEPs fora da cobertura · redirect>leads fraca = fricção final do fluxo
</como_diagnosticar>

<regras_de_evidencia>
- Todo número que você citar em qualquer campo tem que estar também em
  evidencias_citadas, com metrica, valor e janela exatamente como aparecem no
  payload. Número fora dessa lista é rejeitado na validação automática.
- Essa lista é o TETO de especificidade da redação: a etapa seguinte só pode usar
  número que esteja aqui. Declare TODO número relevante do partner, não só os que
  você citou — o CAC e o CPL da semana e da anterior, o delta, o número do driver,
  a taxa da etapa que moveu. Menos de 6 evidências num partner com base é sinal
  de que você deixou a redação sem material.
- Não calcule nada. Taxas, variações, CTR, CPC e tendências já vêm prontas. Se um
  número que você quer citar não existe no payload, ele não existe — reformule o
  diagnóstico no nível que os dados permitem.
- Não cite nome de campanha, criativo, adset ou posição média: não estão no
  payload.
- Não atribua variação a sazonalidade, feriado ou ciclo de faturamento.
  Sazonalidade não está mapeada neste negócio; essa atribuição é sempre
  especulação e fecha a investigação antes da hora.
- confianca "baixa" é resposta válida e melhor que hipótese inventada. Mas se a
  confiança é baixa, a ação da semana é de observação ou coleta de dado, não
  mexida em campanha.
- Quando 7d e 30d apontarem em direções opostas na MESMA métrica, com base
  suficiente nas duas janelas, o resultado não fechou direção: diga isso e
  proponha acompanhar. Divergência entre métricas diferentes não autoriza essa
  saída.
- status "dado_suspeito" -> investimento CONTÍNUO sem nenhum lead: não
  diagnostique performance, a ação é verificar rastreamento (pixel/tags/link).
- status "pausada" -> a conta NÃO tem investimento novo há mais de 10 dias; o
  valor que aparece em 30d é resíduo da janela móvel (o gasto real aconteceu
  há semanas e ainda cabe na janela). Isto é DIFERENTE de "dado_suspeito": não
  é rastreamento quebrado, é a campanha ter parado de rodar/sincronizar. Não
  diagnostique performance nem recomende auditoria de tracking — diga que a
  conta está pausada há quantos dias e, se fizer sentido reativar, o que
  checar antes (orçamento, config da campanha).
- status "ramp" -> diga só que a conta está em ramp e o que observar quando
  fechar 14 dias de dados.
- risco_churn true na triagem -> o diagnóstico tem que endereçar isso
  explicitamente, não apenas repetir o CAC.
- Limites de tamanho: diagnostico até 400 caracteres, cadeia_causal até 300,
  hipotese até 250, como_validar até 200, acao_semana até 250.
</regras_de_evidencia>

<comparacao_com_semana_anterior>
Você recebe diagnostico_semana_anterior. Para cada partner que aparecer lá,
preencha mudou_vs_semana_anterior com uma destas leituras:
- a ação recomendada foi executada e o indicador respondeu
- a ação foi executada e o indicador NÃO respondeu — nesse caso a hipótese
  anterior provavelmente estava errada e você precisa de uma nova
- a ação não parece ter sido executada (indicador e padrão seguem idênticos)
- o diagnóstico mudou de eixo (o problema anterior saiu, apareceu outro)

Partner ausente na semana anterior, ou lista vazia -> deixe o campo fora. Não
invente continuidade.
</comparacao_com_semana_anterior>

<acoes_priorizadas>
Além dos diagnósticos, produza a lista de ações da semana ordenada por impacto.
Sem número mínimo nem máximo: quantas o dado sustentar.

Impacto = gravidade (distância da meta, conforme o status da triagem) x confiança
no diagnóstico. Ordene por gravidade primeiro; em caso de empate, confiança alta
antes de média.

Não inclua ação com confiança baixa nesta lista — ela vive no parecer do partner
como observação, não como ação priorizada.
Nunca proponha mover verba entre partners.
</acoes_priorizadas>

DADOS — data de corte {cover}:
{payload}

DIAGNÓSTICO DA SEMANA ANTERIOR:
{diagnostico_anterior}

Responda apenas com o JSON do schema. Sem markdown em volta, sem comentários."""


STAGE1_SCHEMA = {
    "type": "object",
    "required": ["partners", "acoes_priorizadas"],
    "properties": {
        "partners": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["partner", "status_geral", "eixo_principal", "canal_critico",
                             "diagnostico", "cadeia_causal", "acao_semana", "confianca",
                             "evidencias_citadas", "risco_churn", "movimento_semanal"],
                "properties": {
                    "partner": {"type": "string"},
                    "status_geral": {"type": "string",
                                     "enum": ["ok", "atencao", "alarme", "sem_base",
                                              "dado_suspeito", "ramp", "pausada"]},
                    "eixo_principal": {"type": "string",
                                       "enum": ["cac", "cpl", "funil", "pre_clique", "cobertura",
                                                "rastreamento", "ramp", "saudavel"]},
                    "canal_critico": {"type": "string",
                                      "enum": ["google", "meta", "ambos", "nenhum"]},
                    # "até N caracteres" fazia o modelo mirar ~50% do teto (medido
                    # no run de 26/07). Descrição agora pede o CONTEÚDO esperado;
                    # o tamanho vem de ter o que dizer, não de um alvo numérico.
                    "diagnostico": {"type": "string", "description":
                        "O que está acontecendo com a conta ESTA semana. Cobrir: o "
                        "movimento de CAC e CPL vs a semana anterior (ou dizer que "
                        "não há base), em qual canal, e o número que sustenta cada "
                        "afirmação. 3 a 5 frases densas, 400-700 caracteres."},
                    "cadeia_causal": {"type": "string", "description":
                        "A cadeia completa: métrica de topo -> etapa do funil que "
                        "moveu -> efeito em CPL/CAC. Use o driver.principal do "
                        "comparativo. 300-500 caracteres."},
                    "hipotese": {"type": "string", "description":
                        "Causa raiz provável, com o mecanismo — não só o rótulo. "
                        "Obrigatório quando confianca for alta ou media. 200-350 "
                        "caracteres."},
                    "como_validar": {"type": "string", "description":
                        "O que olhar para confirmar ou derrubar a hipótese, e em "
                        "qual prazo. Obrigatório quando houver hipótese. 150-300 "
                        "caracteres."},
                    "acao_semana": {"type": "string", "description":
                        "O que fazer, específico o bastante para alguém executar "
                        "sem perguntar nada. 200-350 caracteres."},
                    "confianca": {"type": "string", "enum": ["alta", "media", "baixa"]},
                    "risco_churn": {"type": "boolean"},
                    "mudou_vs_semana_anterior": {"type": "string", "description":
                        "Leitura de continuidade vs o diagnóstico da semana passada: "
                        "a ação foi executada e respondeu, foi executada e não "
                        "respondeu (hipótese errada), não parece ter sido executada, "
                        "ou o problema mudou de eixo."},
                    "movimento_semanal": {"type": "string", "description":
                        "Uma frase: CAC e CPL subiram ou caíram vs a semana anterior, "
                        "em qual canal, e o que puxou. Se não houver base, dizer isso "
                        "explicitamente em vez de omitir."},
                    "evidencias_citadas": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["metrica", "valor", "janela"],
                            "properties": {
                                "metrica": {"type": "string"},
                                "valor": {"type": "string"},
                                "janela": {"type": "string"},
                                "canal": {"type": "string", "enum": ["google", "meta", "ambos"]},
                            },
                        },
                    },
                },
            },
        },
        "acoes_priorizadas": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["ordem", "partner", "canal", "acao", "justificativa",
                             "impacto_esperado", "confianca"],
                "properties": {
                    "ordem": {"type": "integer"},
                    "partner": {"type": "string"},
                    "canal": {"type": "string", "enum": ["google", "meta", "ambos"]},
                    "acao": {"type": "string"},
                    "justificativa": {"type": "string"},
                    "impacto_esperado": {"type": "string"},
                    "confianca": {"type": "string", "enum": ["alta", "media"]},
                },
            },
        },
    },
}


# ── estágio 2: redação ────────────────────────────────────────────────────
#
# Tags XML e não JSON: não existe escape pra quebrar, truncamento devolve as
# seções que fecharam em vez de perder o run inteiro, e prosa longa dentro de
# string JSON é onde o Gemini erra escape com mais frequência.

PROMPT_E2 = """Você é especialista sênior de mídia paga do MP Agência e está escrevendo o
relatório semanal das contas. Os diagnósticos já estão fechados — seu trabalho
aqui é redigir, não reanalisar.

<leitores>
Três públicos no mesmo documento:
- Time de mídia — executa as ações. Precisa da alavanca específica: qual canal,
  qual etapa, o que mexer.
- PM do projeto — precisa da leitura de conjunto e do que mudou desde a semana
  passada.
- CEO — lê o topo e para. Precisa saber onde está o dinheiro em risco e qual é a
  decisão da semana, sem vocabulário de operação.

Voz de especialista de mídia falando sobre contas que acompanha toda semana:
direto, opinativo, específico. Não é consultoria externa avaliando de fora, nem
laudo formal. O topo do relatório precisa funcionar para o CEO; o detalhe é do
time de mídia.
</leitores>

<materia_prima>
Você recebe o JSON de diagnóstico desta semana e o da semana anterior. Não recebe
os dados brutos — de propósito. Os leitores já têm um dashboard interativo com
todos os números, funis e comparações. Relatório que repete o dashboard tem valor
zero; seu valor é o raciocínio.
</materia_prima>

<regra_dos_numeros>
Você só pode citar números que aparecem em evidencias_citadas do partner
correspondente. Nada além disso — não estime, não arredonde para um valor que não
está lá, não some, não calcule variação.

Critério de uso, não de quantidade: todo número citado tem que estar na mesma
frase que a conclusão que ele sustenta. Número solto, sem inferência amarrada, é
recitação de dashboard — corte.

Quando o volume for baixo, prefira absolutos a percentuais: "de 20 leads, só 3
viraram venda" comunica o tamanho real do problema melhor que "taxa de 15%".

Valores em R$ sem centavos.
</regra_dos_numeros>

<ordem_de_escrita>
Escreva nesta ordem, porque cada bloco depende do anterior:
1. pareceres por partner
2. recomendações
3. leitura de portfólio (derivada dos pareceres que você acabou de escrever)
4. resumo Slack (derivado da leitura de portfólio)

Não escreva o resumo antes dos pareceres.
</ordem_de_escrita>

<pareceres>
Um parecer por partner, na ordem de prioridade que vier no JSON. Nunca omita um
partner.

4 a 7 frases. Todo parecer cobre, obrigatoriamente:

1. O MOVIMENTO DA SEMANA — CAC e CPL subiram ou caíram vs a semana anterior, e em
   qual canal. Use `movimento_semanal`. Se o diagnóstico disser que não há base
   para leitura semanal, diga isso em uma frase e siga para a janela de 30d.
2. O QUE EXPLICA — a cadeia causal, terminando na alavanca concreta que moveu
   (taxa de conversão em qual etapa, CPC, ou volume de investimento).
3. A HIPÓTESE, quando existir no diagnóstico. Não é opcional: é a parte que o
   dashboard não faz. Escreva o mecanismo, não o rótulo.
4. COMO VALIDAR, quando existir. Uma frase.
5. A AÇÃO da semana.

Os campos 3 e 4 vinham sendo descartados na redação — o diagnóstico produzia
hipótese e forma de validar e o parecer publicava sem eles. Se estão no JSON,
entram no texto.

Os campos cobertos são fixos — a ênfase e a ordem são livres. Abra pela parte
mais forte da história daquela conta, não por um template. Uma conta com degrau na
série abre pelo degrau; uma conta com gargalo de bot abre pelo bot; uma conta
saudável e subaproveitada abre pela oportunidade.

Quando mudou_vs_semana_anterior estiver preenchido, isso entra no parecer —
inclusive quando a leitura é que a ação anterior não foi executada, ou foi e não
funcionou. Essa é a parte do relatório que nenhum dashboard faz.

Partner em ramp, sem_base ou dado_suspeito: uma frase dizendo o que é e o que
verificar. Não force análise.
</pareceres>

<leitura_portfolio>
Comece pelo movimento da carteira, no formato que o time pediu: o CAC geral desta
semana vs a anterior, em %, e QUAIS provedores mais contribuíram para o
movimento. Use o bloco COMPARATIVO PORTFÓLIO (abaixo, fora do diagnóstico) —
`contribuicao_por_partner` já vem ordenado por impacto e diz quanto cada um
empurrou o CAC em reais. Cite os 2 ou 3 primeiros pela direção do efeito — quem
puxou pra cima e quem segurou. Se vier "(sem dado suficiente pra comparar
semanas)", diga isso em vez de inventar a comparação.

Depois: onde o MP Agência ganha e perde dinheiro hoje, qual conta exige ação
urgente e por quê, e a decisão mais importante da semana.

Escrito para o CEO: sem jargão de etapa de funil, sem nome de métrica de
plataforma. Se houver padrão de portfólio — várias contas com o mesmo problema ao
mesmo tempo — a manchete é o padrão, não a conta individual.

Verba nunca se move entre partners. Cada conta é isolada e os resultados são dela.
</leitura_portfolio>

<nao_faca>
- Não escreva frase que não contenha diagnóstico, hipótese, risco, oportunidade
  ou decisão. Teste: se a frase não muda nenhuma decisão do leitor, corte.
- Não subdivida o parecer em "Google:" / "Meta:" com lista de métricas. Canal
  entra na narrativa quando for relevante para o diagnóstico.
- Não use preâmbulo ("vale destacar que", "é importante notar", "conforme os
  dados") nem adjetivo que não carrega informação.
- Não repita entre seções. O resumo Slack não é a leitura de portfólio
  abreviada, é a conclusão dela.
- Não hedge quando o diagnóstico tem confiança alta. Posicione-se: faça X porque
  Y. Quando o diagnóstico disser que o resultado não fechou direção, dizer
  "vamos acompanhar mais uma semana" é a posição correta, não uma fuga.
- Não comente crédito, saldo ou runway do pacote — há alertas dedicados a isso.
- Não mencione responsáveis, pessoas ou times na ação. Descreva o problema, a
  hipótese e o que fazer.
- Não invente número, data ou nome de campanha.
- Não use emoji em nenhum dos quatro blocos da resposta, em nenhuma hipótese.
</nao_faca>

<formato>
Responda com os quatro blocos abaixo, nesta ordem, sem markdown em volta e sem
texto fora das tags:

<pareceres>
HTML. Um h3 com o nome do partner por parecer, seguido de p.
Tags permitidas: h3, p, strong, ul, li.
</pareceres>

<recomendacoes>
HTML. Uma table com thead/tbody e as colunas: Prioridade, Partner, Canal, Ação,
Justificativa, Impacto esperado, Confiança.
Uma linha por item de acoes_priorizadas, na ordem do JSON.
Esta tabela é o recorte executável dos pareceres — repetição aqui é intencional.
Não introduza ação que não esteja no JSON.
</recomendacoes>

<leitura_portfolio>
HTML. Um único p.
</leitura_portfolio>

<resumo_slack>
Texto puro em mrkdwn do Slack (*negrito*, bullets com •). Até 600 caracteres.
SEM NENHUM EMOJI (nem no texto, nem como marcador de bullet).
SÓ os pontos críticos — pra quem vai ler batendo o olho no celular. NÃO
mencione contas saudáveis, estáveis ou "indo bem": se o status_geral do
partner na triagem é "ok", ele não entra aqui, nem como contraste positivo.
Conta "pausada" (sem investimento novo, resíduo da janela móvel) também NÃO
entra — não é ponto crítico, é conta parada, nada a fazer esta semana.
1 bullet com a leitura crítica da semana — a conclusão, não os números.
2 a 3 bullets com os diagnósticos mais graves (alarme/atenção/dado_suspeito/
risco de churn), cada um com o motivo em poucas palavras.
1 bullet com a ação nº 1.
Se NENHUMA conta estiver em alarme ou atenção, diga isso em 1 frase curta e
pare — não force um problema que não existe.
</resumo_slack>
</formato>

DIAGNÓSTICO DESTA SEMANA (corte {cover}):
{diagnostico_json}

DIAGNÓSTICO DA SEMANA ANTERIOR:
{diagnostico_anterior}

COMPARATIVO PORTFÓLIO (última semana completa vs. a anterior, calculado em
Python — só para a leitura de portfólio, não é diagnóstico por partner):
{comparativo_portfolio}"""


# ── chamadas ao Gemini ────────────────────────────────────────────────────

RETRYABLE = {429, 500, 502, 503, 529}


def _post(body, tries=3):
    """POST no Gemini com retry só em erro transiente. 4xx de auth/payload
    estoura na hora — retentar um payload inválido só queima tempo."""
    url = GEMINI_URL.format(model=_model())
    last = None
    for i in range(tries):
        if i:
            time.sleep(20 * i)
        r = requests.post(url, headers={"x-goog-api-key": os.environ["LLM_API_KEY"]},
                          json=body, timeout=REQUEST_TIMEOUT)
        if r.status_code in RETRYABLE:
            last = requests.HTTPError(f"{r.status_code} do Gemini", response=r)
            print(f"Aviso: Gemini devolveu {r.status_code} (tentativa {i + 1}/{tries}).")
            continue
        r.raise_for_status()
        cand = r.json()["candidates"][0]
        parts = cand.get("content", {}).get("parts") or []
        txt = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not txt.strip():
            # resposta sem texto útil. O caso real (2026-08-04, estágio 2) é
            # finishReason=MAX_TOKENS: no Gemini 3 o pensamento conta contra
            # maxOutputTokens, então o modelo pode gastar todo o teto pensando e
            # devolver zero texto — indistinguível de falha silenciosa sem isso.
            raise RuntimeError(
                f"Gemini devolveu resposta sem texto (finishReason="
                f"{cand.get('finishReason')}, usage={r.json().get('usageMetadata')})")
        return txt
    raise last


def call_stage1(payload, cover, diag_anterior):
    """Thinking alto: é a etapa de julgamento e se beneficia."""
    prompt = (PROMPT_E1
              .replace("{cover}", cover)
              .replace("{payload}", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
              .replace("{diagnostico_anterior}",
                       json.dumps(diag_anterior, ensure_ascii=False, separators=(",", ":"))
                       if diag_anterior else "(primeira semana — não existe diagnóstico anterior)"))
    txt = _post({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 65536,
            "responseMimeType": "application/json",
            "responseSchema": STAGE1_SCHEMA,
            "thinkingConfig": {"thinkingBudget": 24576},
        },
    })
    return json.loads(txt)


def call_stage2(diag, cover, diag_anterior, comparativo_portfolio=None):
    """Thinking baixo: aqui é executar uma especificação; thinking alto só
    aumenta a variância da redação.

    `comparativo_portfolio` (bugfix do porte, 2026-07-28): o PROMPT_E2 instrui
    a leitura de portfólio a usar `portfolio.contribuicao_por_partner`, mas o
    estágio 2 só recebia `diag` — o JSON do estágio 1, que por schema
    (STAGE1_SCHEMA) não tem esse campo. Esse dado nunca chegava aqui na versão
    original (nunca rodou ponta a ponta, ver HANDOFF.md de lá). Passado agora
    explicitamente, calculado em Python por `comparativo_semanal()`.
    """
    prompt = (PROMPT_E2
              .replace("{cover}", cover)
              .replace("{diagnostico_json}", json.dumps(diag, ensure_ascii=False, separators=(",", ":")))
              .replace("{diagnostico_anterior}",
                       json.dumps(diag_anterior, ensure_ascii=False, separators=(",", ":"))
                       if diag_anterior else "(primeira semana — sem bloco de continuidade)")
              .replace("{comparativo_portfolio}",
                       json.dumps(comparativo_portfolio, ensure_ascii=False, separators=(",", ":"))
                       if comparativo_portfolio else "(sem dado suficiente pra comparar semanas)"))
    # maxOutputTokens igual ao do estágio 1: no Gemini 3 o pensamento conta
    # contra esse teto e thinkingBudget não o limita de forma confiável, então
    # com 32768 o modelo estourava pensando e devolvia texto vazio (2026-08-04).
    return _post({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 65536,
                             "thinkingConfig": {"thinkingBudget": 2048}},
    })


BLOCOS = ("pareceres", "recomendacoes", "leitura_portfolio", "resumo_slack")
TAGS_OK = {"h3", "p", "strong", "ul", "li", "table", "thead", "tbody", "tr", "th", "td"}


def parse_blocos(txt):
    out = {}
    for b in BLOCOS:
        m = re.search(rf"<{b}>(.*?)</{b}>", txt, re.S)
        if m:
            out[b] = m.group(1).strip()
    return out


def sanitize(html):
    """Nunca deixar o LLM injetar script/style/iframe na página publicada."""
    return re.sub(r"<\s*/?\s*(script|style|iframe|link|meta|object|embed)\b[^>]*>", "", html, flags=re.I)


# faixas unicode de emoji/pictograma — removidas por código em vez de só pedir
# ao LLM, porque o modelo ignora a instrução de vez em quando (visto em
# produção na versão anterior deste arquivo, ex.: "Fala time! 🚀" sem nunca ter
# sido pedido).
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002700-\U000027BF\U0001F900-\U0001F9FF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D]+")


def _strip_emoji(text):
    return EMOJI_RE.sub("", text) if text else text


# ── validação ─────────────────────────────────────────────────────────────

def _numeros(txt):
    limpo = re.sub(r"<[^>]+>", " ", txt)
    achados = set()
    for m in re.finditer(r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:[.,]\d+)?)", limpo):
        try:
            v = float(m.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            continue
        if v >= 2:      # ignora 0 e 1, que aparecem em prosa ("1 lead", "0%")
            achados.add(round(v, 2))
    return achados


def _permitidos(diag):
    ok = set()
    for p in diag.get("partners", []):
        for e in p.get("evidencias_citadas", []):
            for m in re.finditer(r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:[.,]\d+)?)", str(e.get("valor", ""))):
                try:
                    v = float(m.group(1).replace(".", "").replace(",", "."))
                except ValueError:
                    continue
                ok.add(round(v, 2))
                ok.add(float(round(v)))
    return ok


def validar(triagem, diag, blocos):
    """Checks que o desenho pediu. Devolve (avisos, erros_fatais).

    O check de números é o que mata a classe de erro do prompt antigo, em que o
    modelo citava "estável há 6 semanas" — número que não existia no payload.
    """
    avisos, fatais = [], []

    esperados = set(triagem["por_partner"])
    vistos = {p.get("partner") for p in diag.get("partners", [])}
    faltando = esperados - vistos
    if faltando:
        fatais.append(f"partner ausente no diagnóstico: {', '.join(sorted(faltando))}")

    for p in diag.get("partners", []):
        t = triagem["por_partner"].get(p.get("partner"))
        if t and t["status_geral"] != p.get("status_geral"):
            fatais.append(f"{p['partner']}: modelo reclassificou status "
                          f"({p.get('status_geral')} vs {t['status_geral']} da triagem)")

    for a in diag.get("acoes_priorizadas", []):
        if a.get("confianca") == "baixa":
            fatais.append(f"ação priorizada com confiança baixa: {a.get('partner')}")

    # densidade: os dois sintomas medidos no run de 26/07 (pareceres rasos)
    com_base = {k for k, v in triagem["por_partner"].items()
                if v["status_geral"] in ("ok", "atencao", "alarme")}
    magros = [p["partner"] for p in diag.get("partners", [])
              if p.get("partner") in com_base and len(p.get("evidencias_citadas", [])) < 6]
    if magros:
        avisos.append("poucas evidências declaradas (teto da especificidade da prosa): "
                      + ", ".join(magros))
    sem_hip = [p["partner"] for p in diag.get("partners", [])
               if p.get("partner") in com_base
               and p.get("confianca") in ("alta", "media") and not p.get("hipotese")]
    if sem_hip:
        avisos.append("confiança alta/média sem hipótese: " + ", ".join(sem_hip))

    churn_tri = {k for k, v in triagem["por_partner"].items() if v["risco_churn"]}
    churn_diag = {p["partner"] for p in diag.get("partners", []) if p.get("risco_churn")}
    if churn_tri - churn_diag:
        avisos.append(f"risco de churn não endereçado: {', '.join(sorted(churn_tri - churn_diag))}")

    if blocos is not None:
        for b in BLOCOS:
            if not blocos.get(b):
                fatais.append(f"bloco <{b}> ausente ou não fechou")
        n_h3 = len(re.findall(r"<h3", blocos.get("pareceres", ""), re.I))
        if n_h3 != len(vistos):
            avisos.append(f"{n_h3} pareceres para {len(vistos)} partners")
        if len(blocos.get("resumo_slack", "")) > 700:
            avisos.append(f"resumo Slack com {len(blocos['resumo_slack'])} caracteres (limite 700)")
        prosa = " ".join(blocos.get(b, "") for b in ("pareceres", "leitura_portfolio", "resumo_slack"))
        usadas = {t.lower() for t in re.findall(r"<\s*/?\s*([a-zA-Z0-9]+)", prosa)}
        if usadas - TAGS_OK:
            avisos.append(f"tag fora da whitelist: {', '.join(sorted(usadas - TAGS_OK))}")
        permitidos = _permitidos(diag)
        inventados = {v for v in _numeros(prosa)
                      if v not in permitidos and float(round(v)) not in permitidos}
        if inventados:
            amostra = ", ".join(str(v) for v in sorted(inventados)[:12])
            avisos.append(f"número na prosa fora de evidencias_citadas: {amostra}")

    return avisos, fatais


# ── orquestração ──────────────────────────────────────────────────────────

DIAG_DIR = "data/diagnosticos"


def _diag_anterior(repo_root):
    """Diagnóstico mais recente já persistido. Na 1ª semana não existe e o
    bloco de continuidade simplesmente não sai."""
    d = os.path.join(repo_root, DIAG_DIR)
    if not os.path.isdir(d):
        return None, None
    arquivos = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not arquivos:
        return None, None
    with open(os.path.join(d, arquivos[-1]), encoding="utf-8") as f:
        return json.load(f), arquivos[-1][:-5]


def build_context(all_daily, all_dfg, all_dfm, cutoff_dt, partners):
    """Payload + triagem. Puro cálculo, sem LLM.

    Separado do run() de propósito: é também o contexto da aba de chat, que
    precisa existir mesmo quando a análise semanal falha ou está desligada.
    """
    payload = build_payload(all_daily, all_dfg, all_dfm, cutoff_dt, partners)
    payload["triagem"] = triar(payload)
    return payload


def run(context, cutoff_dt, repo_root):
    """Estágios 1 e 2 sobre um contexto já montado por build_context().

    Levanta exceção em falha — quem chama (o refresh) engole e segue sem a
    análise. Análise nunca derruba o refresh.
    """
    cover = cutoff_dt.strftime("%d/%m/%y")
    payload = context
    triagem = payload["triagem"]

    anterior, anterior_data = _diag_anterior(repo_root)

    diag = call_stage1(payload, cover, anterior)
    avisos, fatais = validar(triagem, diag, None)
    if fatais:
        # 1 retry: falha de validação no estágio 1 costuma ser o modelo
        # desobedecendo a instrução de copiar o status da triagem numa única
        # resposta (ruído da chamada, não do payload) — achado real 2026-08-04.
        # Acontece antes de qualquer envio ao Slack, então não há risco de
        # mensagem duplicada.
        print("Aviso: estágio 1 reprovou na validação (" + " · ".join(fatais)
              + ") — tentando de novo.", file=sys.stderr)
        diag = call_stage1(payload, cover, anterior)
        avisos, fatais = validar(triagem, diag, None)
        if fatais:
            raise RuntimeError("estágio 1 reprovou na validação (após retry): "
                                + " · ".join(fatais))

    comparativo = payload.get("comparativo_semana_vs_semana") or {}
    texto = call_stage2(diag, cover, anterior, comparativo.get("portfolio"))
    blocos = parse_blocos(texto)
    a2, f2 = validar(triagem, diag, blocos)
    if f2:
        # mesmo racional do retry do estágio 1: ainda antes do Slack.
        print("Aviso: estágio 2 reprovou na validação (" + " · ".join(f2)
              + ") — tentando de novo.", file=sys.stderr)
        texto = call_stage2(diag, cover, anterior, comparativo.get("portfolio"))
        blocos = parse_blocos(texto)
        a2, f2 = validar(triagem, diag, blocos)
        if f2:
            raise RuntimeError("estágio 2 reprovou na validação (após retry): "
                                + " · ".join(f2))
    avisos += a2

    for b in ("pareceres", "recomendacoes", "leitura_portfolio"):
        blocos[b] = _strip_emoji(sanitize(blocos[b]))
    if blocos.get("resumo_slack"):
        blocos["resumo_slack"] = _strip_emoji(blocos["resumo_slack"]).strip()

    os.makedirs(os.path.join(repo_root, DIAG_DIR), exist_ok=True)
    with open(os.path.join(repo_root, DIAG_DIR, f"{cutoff_dt.isoformat()}.json"), "w",
              encoding="utf-8") as f:
        json.dump(diag, f, ensure_ascii=False, indent=1)

    return {
        "gerado_em": cutoff_dt.isoformat(),
        "cover": cover,
        "modelo": _model(),
        "triagem": triagem,
        "triagem_html": render_triagem_html(triagem),
        "diagnostico": diag,
        "diagnostico_anterior_de": anterior_data,
        "blocos": blocos,
        "avisos": avisos,
    }


# ── saída específica desta versão (HTML publicado + Slack) ─────────────────
#
# Não existe na versão Streamlit original — lá o app lê data/analysis.json e
# renderiza com st.markdown. Aqui não tem app: o resultado do run() vira uma
# página estática (docs/analise.html) e um resumo no Slack.

def render_cac_slack(payload):
    """CAC 30d por partner ATIVO, separado por canal — pro bloco de thread do Slack.

    Ativo = teve investimento líquido ou lead em 30d na conta (nível agregado)
    E não está com status "pausada" na triagem — achado real 2026-07-28
    (Unifique): sem esse segundo filtro, um partner sem campanha rodando há
    semanas ainda aparece aqui com um resíduo de investimento_liquido > 0
    (a janela móvel de 30d ainda toca o último dia de gasto real), mostrando
    uma linha só de traços que não ajuda ninguém.
    """
    kpis = payload.get("kpis_por_partner_canal_janela", {})
    partners = payload.get("partners") or list(kpis)
    triagem = payload.get("triagem", {}).get("por_partner", {})

    def fmt(v):
        return f"R${v:.0f}" if v is not None else "—"

    linhas = []
    for p in partners:
        if triagem.get(p, {}).get("status_geral") == "pausada":
            continue
        w = kpis.get(p, {})
        conta = w.get("conta", {}).get("30d", {})
        if not (conta.get("investimento_liquido") or conta.get("leads")):
            continue
        cac_google = w.get("google", {}).get("30d", {}).get("cac")
        cac_meta = w.get("meta", {}).get("30d", {}).get("cac")
        cac_conta = conta.get("cac")
        linhas.append(f"• *{p}*: Google {fmt(cac_google)} · Meta {fmt(cac_meta)} · "
                      f"Conta {fmt(cac_conta)}")
    if not linhas:
        return None
    return "*CAC 30d por parceiro (canal):*\n" + "\n".join(linhas)


def render_report_body(result):
    """Monta o corpo HTML de docs/analise.html a partir dos blocos do estágio 2.

    O estágio 2 devolve 4 blocos soltos (pareceres/recomendacoes/leitura_portfolio/
    resumo_slack), sem os h2 de seção que o relatório publicado precisa — aqui
    só empacota, sem reescrever conteúdo.
    """
    blocos = result["blocos"]
    partes = [result["triagem_html"]]
    if blocos.get("leitura_portfolio"):
        partes.append("<h2>Leitura do Portfólio</h2>\n" + blocos["leitura_portfolio"])
    if blocos.get("pareceres"):
        partes.append("<h2>Parecer por Partner</h2>\n" + blocos["pareceres"])
    if blocos.get("recomendacoes"):
        partes.append("<h2>Recomendações</h2>\n" + blocos["recomendacoes"])
    return "\n".join(partes)


PAGE_TEMPLATE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Análise IA — MP Agência ({cover})</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; background: #f6f7f9; color: #1c2733; line-height: 1.55; }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 32px 20px 64px; }}
  header h1 {{ font-size: 1.5rem; margin: 0 0 4px; }}
  header p {{ color: #5b6b7b; margin: 0 0 8px; font-size: .92rem; }}
  .aviso {{ background: #fff8e1; border: 1px solid #f0dfa3; border-radius: 8px;
           padding: 10px 14px; font-size: .85rem; color: #6b5b1e; margin: 16px 0 24px; }}
  main {{ background: #fff; border: 1px solid #e3e8ee; border-radius: 12px; padding: 28px 32px; }}
  main h2 {{ font-size: 1.15rem; border-bottom: 2px solid #e3e8ee; padding-bottom: 6px; margin-top: 32px; }}
  main h2:first-child {{ margin-top: 0; }}
  main h3 {{ font-size: 1rem; margin-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .88rem; margin: 12px 0; }}
  th, td {{ border: 1px solid #e3e8ee; padding: 7px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f0f3f6; }}
  a {{ color: #1665c0; }}
  footer {{ margin-top: 20px; font-size: .8rem; color: #8a97a5; }}
  @media (max-width: 640px) {{ main {{ padding: 18px 14px; }} .wrap {{ padding: 16px 10px 40px; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Análise IA — Funil Ads-to-Sale MP Agência</h1>
    <p>Snapshot de {cover} · gerada automaticamente no refresh semanal · <a href="index.html?v={cutoff}">← voltar ao dashboard</a></p>
  </header>
  <div class="aviso">Relatório gerado por IA (gemini/{model}) a partir dos dados do dashboard.
  Todo número citado é validado contra a lista de evidências do diagnóstico (estágio 1), mas
  confira no dashboard antes de mexer em campanha.</div>
  <main>
{body}
  </main>
  <footer>MP Agência · Melhor Plano — atualizado em {cover}.</footer>
</div>
</body>
</html>
"""


def render_page(result, cover, cutoff):
    return PAGE_TEMPLATE.format(
        cover=cover, cutoff=cutoff, model=result.get("modelo") or _model(),
        body=render_report_body(result))
