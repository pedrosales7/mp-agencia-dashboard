#!/usr/bin/env python3
"""
Análise IA semanal do funil MP Agência — camada opcional do weekly_refresh.

Lê os mesmos dados já carregados pelo refresh (séries diárias, funis, semanais,
crédito), monta um payload compacto de KPIs e pede a um LLM um diagnóstico
ponta-a-ponta + recomendações de mídia. Saída: resumo pro Slack + relatório
HTML publicado no GitHub Pages (docs/analise.html).

Agnóstico de provedor — controlado por env vars (secrets/variables do repo):
  LLM_API_KEY   (secret)  — obrigatório; sem ele a análise é pulada em silêncio.
  LLM_PROVIDER  (variable) — gemini (default) | openai | anthropic.
  LLM_MODEL     (variable) — opcional, sobrescreve o default do provedor.

Defaults pensados pro menor custo: Gemini via Google AI Studio tem tier
gratuito que cobre folgado 1 chamada/semana.
"""
import json
import os
import re
from collections import defaultdict
from datetime import timedelta

import requests

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}

REQUEST_TIMEOUT = 300


def enabled():
    return bool(os.environ.get("LLM_API_KEY"))


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


def build_payload(all_daily, all_dfg, all_dfm, partner_weekly_dict, credit_dict,
                  cutoff_dt, valid_partners):
    windows = _window_bounds(cutoff_dt)

    # investimento/leads/vendas por partner × canal × janela (do DAILY_SNAPSHOT)
    invest = {}
    for wkey, (d_ini, d_fim) in windows.items():
        agg = defaultdict(lambda: defaultdict(float))
        for r in all_daily:
            if r["id_mp"] in valid_partners and d_ini <= r["dia"] <= d_fim:
                k = (r["id_mp"], r["canal"])
                for f in ("bruto", "cashback", "liquido", "leads", "vendas"):
                    agg[k][f] += r.get(f) or 0
        for (id_mp, canal), v in agg.items():
            liq = round(v["liquido"])
            leads, vendas = int(v["leads"]), int(v["vendas"])
            invest.setdefault(id_mp, {}).setdefault(canal, {})[wkey] = {
                "investimento_liquido": liq,
                "cashback": round(v["cashback"]),
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

    fg_fields = ("cliques", "sessoes", "clickoff", "redirect", "leads", "vendas")
    fm_fields = ("cliques", "chat_start", "zip_search", "redirect", "leads", "vendas")
    wkeys = ("7d", "7d_prev", "30d")
    funil_google = agg_funnel(all_dfg, fg_fields, wkeys)
    funil_meta = agg_funnel(all_dfm, fm_fields, wkeys)

    # série semanal (últimas 8 semanas) + crédito atual
    semanal = {p: rows[-8:] for p, rows in partner_weekly_dict.items() if p in valid_partners}
    credito = {p: rows[-1] for p, rows in credit_dict.items() if p in valid_partners and rows}

    return {
        "data_corte": cutoff_dt.isoformat(),
        "janelas": {k: {"inicio": v[0], "fim": v[1]} for k, v in windows.items()},
        "partners": valid_partners,
        "kpis_por_partner_canal_janela": invest,
        "funil_google_por_partner": funil_google,
        "funil_meta_por_partner": funil_meta,
        "serie_semanal_por_partner": semanal,
        "credito_atual_por_partner": credito,
    }


# ── prompt ────────────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """Você é um analista sênior de mídia paga da MP Agência (Melhor Plano), \
agência de performance para provedores regionais de internet (ISPs). Modelo de negócio: \
o provedor ("partner") compra um pacote 100% investido em mídia, sem fee; leads que fecham \
com outro provedor geram cashback de reinvestimento.

Dois canais por partner:
- google: Google Ads. Funil: cliques > sessoes > clickoff > redirect > leads > vendas.
- meta: Meta Ads via WhatsApp. Funil: cliques > chat_start > zip_search > redirect > leads > vendas.

Definições fixas (não recalcule diferente):
- Lead produtivo: lead aceito pelo provedor. Venda: situação sold/installed/scheduled.
- CPL e CAC já vêm calculados sobre investimento LÍQUIDO (bruto - cashback).
- Atribuição sempre ao partner anunciante (quem pagou a campanha).

Cuidados de leitura:
- Vendas têm lag de fechamento: na janela 7d o CAC sai inflado e vendas subestimadas. \
Compare 7d vs 7d_prev para tendência de topo/meio de funil; use 30d para eficiência (CPL/CAC).
- Volumes pequenos geram variações percentuais grandes — só aponte anomalia com base mínima \
(>= ~10 eventos na etapa) ou padrão persistente na série semanal.
- KPIs (kpis_por_partner_canal_janela) e funis usam metodologias de atribuição levemente \
diferentes; pequenas divergências de leads/vendas entre eles são esperadas, não são erro.
- credito_atual_por_partner: "credito" é o saldo restante do pacote. Estime runway dividindo \
pelo gasto líquido semanal médio (serie_semanal_por_partner) e alerte se < 3 semanas.

Sua tarefa, com os dados JSON abaixo (data de corte {cover}):
1. Visão geral da semana (7d vs 7d_prev): investimento, leads, vendas, CPL/CAC 30d, agregado e por canal.
2. Destaques e alertas por partner — só partners com movimento relevante (queda/alta forte, funil travado, crédito acabando).
3. Gargalos de funil: para cada partner+canal relevante, identifique a etapa com pior taxa de passagem vs histórico e o diagnóstico provável (ex.: cliques altos + chat_start baixo = criativo/segmentação; redirect alto + leads baixos = qualificação/aceite do provedor; sessões baixas por clique = landing/tracking).
4. Recomendações: 3 a 6 ações concretas e priorizadas para as campanhas (orçamento, criativo, segmentação, horário, negociação com o provedor), cada uma com partner, canal, justificativa nos dados e impacto esperado.

Responda SOMENTE com JSON válido, sem markdown em volta, neste formato:
{{
  "resumo_slack": "resumo executivo em até 700 caracteres, formato mrkdwn do Slack (*negrito*, bullets com •), 3-5 bullets: 1 de visão geral, 2-3 alertas/destaques, 1 apontando a recomendação nº1",
  "relatorio_html": "corpo HTML do relatório completo (apenas h2, h3, p, ul, li, strong, table/thead/tbody/tr/th/td — sem html/head/body/script/style). Seções: Visão geral; Destaques por partner; Gargalos de funil; Recomendações (tabela com colunas Prioridade, Partner, Canal, Ação, Justificativa, Impacto esperado). Valores em R$ sem centavos."
}}

Escreva em português do Brasil, tom direto de analista, números sempre com contexto de comparação.

DADOS:
{payload}
"""


def build_prompt(payload, cover):
    return PROMPT_TEMPLATE.format(
        cover=cover,
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


# ── LLM providers ─────────────────────────────────────────────────────────

def provider_info():
    provider = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    if provider not in DEFAULT_MODELS:
        raise RuntimeError(f"LLM_PROVIDER desconhecido: {provider!r} (use gemini|openai|anthropic)")
    model = (os.environ.get("LLM_MODEL") or "").strip() or DEFAULT_MODELS[provider]
    return provider, model


def call_llm(prompt):
    provider, model = provider_info()
    key = os.environ["LLM_API_KEY"]

    if provider == "gemini":
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 16384,
                    "responseMimeType": "application/json",
                },
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        parts = resp.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)

    if provider == "openai":
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    # anthropic
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={
            "model": model,
            "max_tokens": 16384,
            "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json()["content"])


def parse_response(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise RuntimeError(f"Resposta do LLM sem JSON reconhecível: {text[:200]!r}")
    data = json.loads(text[start:end + 1])
    for field in ("resumo_slack", "relatorio_html"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise RuntimeError(f"Resposta do LLM sem campo {field!r}")
    # nunca deixar o LLM injetar script/style na página publicada
    data["relatorio_html"] = re.sub(
        r"<\s*/?\s*(script|style|iframe|link|meta)\b[^>]*>", "", data["relatorio_html"], flags=re.I)
    return data


def generate(payload, cover):
    raw = call_llm(build_prompt(payload, cover))
    return parse_response(raw)


# ── página publicada ──────────────────────────────────────────────────────

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
    <h1>🤖 Análise IA — Funil Ads-to-Sale MP Agência</h1>
    <p>Snapshot de {cover} · gerada automaticamente no refresh semanal · <a href="index.html?v={cutoff}">← voltar ao dashboard</a></p>
  </header>
  <div class="aviso">⚠️ Relatório gerado por IA ({provider}/{model}) a partir dos dados do dashboard.
  Valide os números no dashboard antes de executar mudanças nas campanhas.</div>
  <main>
{body}
  </main>
  <footer>MP Agência · Melhor Plano — atualizado em {cover}.</footer>
</div>
</body>
</html>
"""


def render_page(relatorio_html, cover, cutoff):
    provider, model = provider_info()
    return PAGE_TEMPLATE.format(
        cover=cover, cutoff=cutoff, provider=provider, model=model, body=relatorio_html)
