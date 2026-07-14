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
import time
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


def build_payload(all_daily, all_dfg, all_dfm, partner_weekly_dict,
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

    # série semanal (últimas 8 semanas). Crédito/runway fica FORA do payload de
    # propósito (decisão do Pedro 2026-07-09): já existem alertas dedicados e,
    # se o dado estiver aqui, o modelo desvia o parecer pra isso.
    semanal = {p: rows[-8:] for p, rows in partner_weekly_dict.items() if p in valid_partners}

    # tendência da série semanal calculada aqui — o LLM não extrai de forma
    # confiável "tendência real vs ruído de 1 semana" de uma lista crua de 8
    # números. direcao_8sem compara a média da 1ª metade com a da 2ª metade;
    # semanas_estaveis_consecutivas conta quantas semanas recentes seguidas
    # ficam dentro de ±15% da média das 8 — sinal de estagnação/fadiga (o
    # padrão do exemplo "BOM" do prompt: "estável há 6 semanas").
    def _trend_summary(rows, field):
        vals = [r.get(field) or 0 for r in rows]
        n = len(vals)
        if n < 4:
            return None
        half = n // 2
        first_avg = sum(vals[:half]) / half
        second_avg = sum(vals[half:]) / (n - half)
        delta_pct = round(100 * (second_avg - first_avg) / first_avg, 1) if first_avg else None
        if delta_pct is None:
            direcao = "sem_base"
        elif delta_pct >= 15:
            direcao = "alta"
        elif delta_pct <= -15:
            direcao = "queda"
        else:
            direcao = "estavel"
        media_geral = sum(vals) / n
        semanas_estaveis = 0
        for v in reversed(vals):
            if media_geral and abs(v - media_geral) / media_geral <= 0.15:
                semanas_estaveis += 1
            else:
                break
        return {
            "direcao_8sem": direcao,
            "variacao_pct_1a_vs_2a_metade": delta_pct,
            "semanas_estaveis_consecutivas": semanas_estaveis,
        }

    tendencia_semanal = {}
    for p, rows in semanal.items():
        tendencia_semanal[p] = {
            "leads": _trend_summary(rows, "leads"),
            "vendas": _trend_summary(rows, "vendas"),
        }

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

    return {
        "data_corte": cutoff_dt.isoformat(),
        "janelas": {k: {"inicio": v[0], "fim": v[1]} for k, v in windows.items()},
        "partners": valid_partners,
        "kpis_por_partner_canal_janela": invest,
        "funil_google_por_partner": funil_google,
        "funil_meta_por_partner": funil_meta,
        "serie_semanal_por_partner": semanal,
        "tendencia_semanal_por_partner": tendencia_semanal,
        "benchmark_pre_clique_30d": benchmark,
        "gargalo_funil_30d": gargalo_funil,
    }


# ── prompt ────────────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """Você é um consultor sênior de mídia paga especializado em performance para \
provedores regionais de internet (ISPs). Toda semana você assina o parecer estratégico das contas \
do MP Agência para a equipe de mídia e o coordenador do serviço.

FATO CENTRAL: seus leitores JÁ TÊM um dashboard interativo com todos os números, funis, séries e \
comparações de período. Relatório que repete números do dashboard tem valor ZERO. Seu valor é o que \
o dashboard não faz: pensamento crítico — explicar POR QUE os números estão como estão, cruzar \
sinais que uma tabela não cruza, formular hipóteses de causa raiz e se posicionar sobre O QUE FAZER.

<contexto_negocio>
O MP Agência (Melhor Plano) vende para provedores regionais ("partners") um pacote de mídia 100%
investido em campanhas, sem fee de agência. São 2 canais por partner:
- google: campanhas de pesquisa no Google Ads. Funil: impressoes > cliques > sessoes > clickoff >
  redirect > leads > vendas.
- meta: campanhas de WhatsApp no Meta Ads (click-to-WhatsApp com bot). Funil: impressoes > cliques >
  chat_start > zip_search > redirect > leads > vendas.

MECÂNICA DO CASHBACK (importante): quando o lead gerado pela campanha de um partner fecha com OUTRO
provedor (ex.: sem cobertura do anunciante no CEP do usuário), o anunciante recebe cashback de
reinvestimento. Por isso:
- investimento_liquido = investimento_bruto - cashback. CPL e CAC JÁ vêm calculados sobre o líquido.
- Cashback alto NÃO é "dinheiro de volta, ótimo": é sinal de que a campanha está gerando demanda
  fora da área de cobertura do anunciante. pct_cashback crescente pede revisão de segmentação
  geográfica (raios, CEPs, cidades) da campanha.
- Atribuição de lead e venda é SEMPRE ao partner anunciante (quem pagou a campanha), nunca ao
  provedor que recebeu o lead.
</contexto_negocio>

<definicoes_fixas>
Não recalcule nem reinterprete:
- Lead (produtivo): lead registrado e aceito pelo provedor.
- Venda: lead com situação sold, installed ou scheduled.
- CPL = investimento líquido / leads. CAC = investimento líquido / vendas.
- ctr_pct = cliques / impressões (%). cpc_estimado = investimento bruto / cliques — é ESTIMADO:
  investimento vem do sistema financeiro e cliques da plataforma de ads, bases diferentes do
  gerenciador. Use para tendência e comparação entre janelas, não para auditar o valor absoluto.
- taxas_etapa (dentro de cada janela de funil): taxa de passagem (%) entre cada par de etapas
  consecutivas (ex.: "sessoes>clickoff"). Já vem calculada — não recalcule a partir das contagens.
- gargalo_funil_30d: a ETAPA do meio de funil já identificada como mais fraca de cada
  partner×canal em 30d (maior desvio negativo vs pares OU vs a própria janela anterior). Este é
  o ponto de partida do diagnóstico de funil — veja <como_pensar>.
- tendencia_semanal_por_partner: direção da série de 8 semanas (alta/queda/estavel, comparando
  1ª metade com 2ª metade) e há quantas semanas seguidas o valor está estável (dentro de ±15%
  da média das 8). Use isso pra dizer "há N semanas", não invente esse número.
- Valores monetários em R$ (BRL).
</definicoes_fixas>

<parametros_de_analise>
- Base mínima para apontar anomalia: >= 10 eventos na etapa OU padrão que se repete em >= 3 semanas
  da série semanal. Abaixo disso, não alarme — no máximo cite como "sinal fraco, monitorar".
- Variação relevante: |variação| >= 25% entre janelas comparadas, respeitando a base mínima.
- Benchmark: compare cada partner primeiro com o próprio histórico (série semanal e janela
  anterior); use a média dos demais partners no mesmo canal apenas como referência secundária de
  taxas de passagem.
- Confiança: só recomende ações com confiança alta ou média. Rotule cada recomendação com
  [confiança alta] ou [confiança média]. Sem base suficiente = não recomende.
</parametros_de_analise>

<cuidados_de_leitura>
- Vendas têm lag de fechamento (lead vira venda dias depois): em janelas de 7d, vendas ficam
  subestimadas e CAC inflado. Use 7d vs 7d_prev para topo e meio de funil (impressões, cliques,
  sessões/conversas, redirects, leads) e 30d vs 30d_prev para eficiência (CPL, CAC, taxa
  lead>venda) e leitura de custo.
- kpis_por_partner_canal_janela e funil_*_por_partner usam metodologias de atribuição levemente
  diferentes; pequenas divergências de leads/vendas entre os dois blocos são esperadas — não trate
  como erro nem some os dois.
- Impressões, CTR e CPC existem só nos blocos de funil (funil_google_por_partner /
  funil_meta_por_partner).
- "sessoes" (Google) pode ter ~1% de dupla contagem na virada do dia. Ignore variações pequenas
  nessa etapa.
- A etapa lead > venda depende da operação comercial do PROVEDOR (atendimento, agenda de
  instalação), não da campanha. Se o gargalo for aí, a recomendação é acionar o responsável pela
  conta/provedor, não mexer em mídia.
- Não invente dados: se uma informação não está no JSON (ex.: nome de campanha, criativo específico,
  posição média), não a cite. Formule a recomendação no nível que os dados permitem.
</cuidados_de_leitura>

<como_pensar>
Antes de escrever, monte internamente o quadro de cada partner — NESTA ORDEM (não pule etapas):

1. Abra por gargalo_funil_30d e tendencia_semanal_por_partner primeiro. gargalo_funil_30d já
   aponta a etapa mais fraca do meio de funil (google: sessoes>clickoff, clickoff>redirect,
   redirect>leads; meta: chat_start>zip_search, zip_search>redirect, redirect>leads) — é o
   diagnóstico mais específico e acionável que existe no payload, e o que o time de mídia
   consegue agir mais rápido (ajuste de landing, oferta, fricção do bot). Comece a análise por
   aqui, não pelo cashback.
2. Só depois olhe benchmark_pre_clique_30d (CTR/CPC vs pares). Desvio de 30% ou mais DEVE
   aparecer no parecer: CTR muito abaixo dos pares = criativo/segmentação; CPC muito acima =
   leilão/qualidade do anúncio.
3. Cashback (pct_cashback) entra como explicação SÓ quando o gargalo apontado for pré-clique
   (CTR/CPC) ou quando pct_cashback subiu vs a janela anterior — cashback alto sozinho, sem
   subida, é contexto, não é a manchete do parecer.
4. Use tendencia_semanal_por_partner pra dizer HÁ QUANTAS SEMANAS o padrão se repete
   (semanas_estaveis_consecutivas) em vez de citar só o valor da janela atual — isso é o que
   separa tendência real de ruído de uma semana.
5. A conta está saudável, estagnada ou em deterioração? Qual é O problema (ou A oportunidade)
   número 1 desta conta agora — response a essa pergunta deve vir do gargalo mais forte
   encontrado nos passos 1-3, não de um template fixo.
- Cruze sinais que uma tabela não cruza: canais divergindo no mesmo partner (demanda existe, canal
  falha?); etapas contando histórias contraditórias; pct_cashback vs segmentação geográfica;
  pré-clique vs meio de funil; eficiência relativa vs os outros partners no mesmo canal.
- Formule hipóteses de causa raiz e rotule como [hipótese], dizendo como validar cada uma.
- Se esta conta fosse sua, o que você mudaria ESTA semana?

Padrões de diagnóstico úteis:
- impressões caindo + CTR estável = perda de entrega (orçamento/lance/leilão); CTR caindo +
  impressões estáveis = fadiga de criativo ou concorrente novo; impressões subindo + CTR caindo
  sem ganho de cliques = segmentação aberta demais; CPC subindo + CTR estável = leilão mais caro.
- google: cliques ok + sessões baixas = landing/tracking; sessões>clickoff fraca = oferta pouco
  competitiva; clickoff>redirect fraca = cobertura/viabilidade; redirect>lead fraca = fricção de
  formulário/aceite.
- meta: cliques>chat_start fraca = criativo/CTA ou fricção do click-to-WhatsApp; chat_start>
  zip_search fraca = abandono no início do bot; zip_search>redirect fraca = CEPs fora da cobertura
  (segmentação geográfica); redirect>lead fraca = fricção final do fluxo.
- pct_cashback subindo = campanha vendendo para concorrentes = segmentação geográfica desalinhada.
- lead>venda fraca = operação comercial do provedor, não mídia — ação é acionar o responsável.
</como_pensar>

<o_que_nao_fazer>
- NÃO recite variações numéricas ("leads +28%, CPL R$ 62 (-18%), CAC +5%..."). O dashboard mostra
  isso melhor que você. LIMITE DURO: no máximo 3 números por parecer, no máximo 1 casa decimal
  cada. Se o rascunho interno tem mais de 3, corte os mais fracos antes de escrever a versão final.
- NÃO escreva frase que não contenha diagnóstico, hipótese, risco, oportunidade ou decisão. Teste:
  se a frase não muda nenhuma decisão do leitor, corte.
- NÃO use a mesma estrutura mecânica para todos os partners — template preenchido é relatório morto.
  Cada parecer segue a história daquela conta. Teste concreto: releia os 8 pareceres antes de
  finalizar — se mais de 2 deles abrem citando cashback ou CTR/CPC como primeiro sinal, você
  ignorou gargalo_funil_30d e está caindo no padrão fácil. Reescreva puxando o gargalo de funil
  (ou outro sinal) como abertura desses casos.
- NÃO subdivida cada partner em "Google:" / "Meta:" com lista de métricas; canal entra na narrativa
  quando for relevante para o diagnóstico.
- NÃO hedge ("pode ser interessante avaliar..."). Posicione-se: "faça X porque Y".
- NÃO comente crédito, saldo ou runway do pacote — já existem alertas dedicados a isso. Escopo
  deste relatório: performance de campanha e gargalos de funil, só.
- NÃO ultrapasse 5 frases por parecer de partner. Limite duro.
</o_que_nao_fazer>

<regua_de_qualidade>
RUIM (relata — valor zero): "Loga Google: 28 leads (+28% vs 7d ant.), CPL R$ 62 (-18%), CAC R$ 270.
No Meta, 38 cliques, CTR 0,9% (-25%), 14 conversas iniciadas."
BOM (analisa, enxuto — é isso que se espera): "Loga é a conta mais saudável do portfólio e está
subaproveitada: converte sessão em lead acima da média e o CAC 30d caiu sem verba nova — escalar
orçamento no Google. Freio no Meta: CTR 0,9% com impressões estáveis há 6 semanas indica criativo
fatigado [hipótese — checar data da última troca]. Rotacionar criativo antes de cortar verba."
</regua_de_qualidade>

<tarefa>
Analise os dados JSON abaixo (data de corte: {cover}) raciocinando passo a passo internamente
antes de escrever. Produza:

1. LEITURA DO PORTFÓLIO — 1 parágrafo curto: onde o MP Agência ganha e perde dinheiro hoje, qual
   conta exige ação urgente e por quê, e a decisão mais importante da semana (30d vs 30d_prev e
   série semanal separam tendência de ruído).
2. PARECER POR PARTNER — para CADA um dos 8 partners, um parecer de 3 a 5 frases: situação em uma
   frase; diagnóstico do que explica a performance, começando por gargalo_funil_30d (a etapa mais
   fraca já identificada) e só recorrendo a pré-clique (ctr_pct/cpc_estimado) ou cashback quando
   fizerem mais sentido pro caso; use tendencia_semanal_por_partner pra ancorar tendência vs
   ruído; hipótese de causa raiz rotulada [hipótese] com forma de validação; ação da semana.
   Partner sem investimento/atividade = 1 linha dizendo isso e o que verificar. Nunca omita um
   partner.
3. RECOMENDAÇÕES PARA A EQUIPE DE MÍDIA — 3 a 6 ações concretas, priorizadas por impacto,
   cada uma com partner, canal, ação específica, justificativa (com os números que a sustentam),
   impacto esperado e rótulo [confiança alta] ou [confiança média].
</tarefa>

<formato_de_saida>
Responda SOMENTE com JSON válido, sem markdown em volta:
{{
  "resumo_slack": "resumo executivo em até 700 caracteres, formato mrkdwn do Slack (*negrito*, bullets com •): 1 bullet com a leitura da semana (a conclusão, não os números), 2-3 bullets com os diagnósticos mais importantes, 1 bullet com a ação nº 1 da semana",
  "relatorio_html": "corpo HTML do relatório (apenas h2, h3, p, ul, li, strong, table/thead/tbody/tr/th/td). Seções: Leitura do Portfólio; Parecer por Partner (um h3 por partner); Recomendações (tabela com colunas Prioridade, Partner, Canal, Ação, Justificativa, Impacto esperado, Confiança). Valores em R$ sem centavos."
}}
Tom: consultor experiente falando com colegas — direto, opinativo, específico. Estilo enxuto:
frases curtas, sem preâmbulos ("vale destacar que", "é importante notar"), sem adjetivo que não
carrega informação, sem repetir o que outra seção já disse. Se dá para dizer em 8 palavras, não
use 20. Português do Brasil.
</formato_de_saida>

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


def call_llm(prompt, model_override=None):
    provider, model = provider_info()
    if model_override:
        model = model_override
    key = os.environ["LLM_API_KEY"]

    if provider == "gemini":
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.4,
                    # relatório de 8 partners × 2 canais é longo, e nos Gemini 2.5 os
                    # tokens de thinking também descontam daqui — 16384 truncava o JSON.
                    "maxOutputTokens": 65536,
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
                "temperature": 0.4,
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
            "temperature": 0.4,
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
        raise RuntimeError(f"Resposta do LLM sem JSON reconhecível (truncada?): "
                           f"{len(text)} chars, início: {text[:200]!r}")
    data = json.loads(text[start:end + 1])
    for field in ("resumo_slack", "relatorio_html"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise RuntimeError(f"Resposta do LLM sem campo {field!r}")
    # nunca deixar o LLM injetar script/style na página publicada
    data["relatorio_html"] = re.sub(
        r"<\s*/?\s*(script|style|iframe|link|meta)\b[^>]*>", "", data["relatorio_html"], flags=re.I)
    return data


RETRYABLE_STATUS = {429, 500, 502, 503, 529}


def generate(payload, cover):
    """Chama o LLM com retry e fallback de modelo.

    Ordem: modelo configurado (2 tentativas, pausa entre elas) e, se ele seguir
    indisponível (ex.: 429 por modelo fora do free tier), o default do provedor.
    Erros não-transientes (4xx de auth/payload) estouram na hora.
    """
    prompt = build_prompt(payload, cover)
    provider, model = provider_info()
    attempts = [model, model]
    if model != DEFAULT_MODELS[provider]:
        attempts.append(DEFAULT_MODELS[provider])
    last_err = None
    for i, m in enumerate(attempts):
        if i:
            time.sleep(45)
        try:
            data = parse_response(call_llm(prompt, model_override=m))
            data["_model"] = m
            return data
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in RETRYABLE_STATUS:
                raise
            print(f"Aviso: {provider}/{m} retornou {status}; "
                  f"{'tentando fallback' if i + 1 < len(attempts) else 'sem mais opções'}.")
            last_err = e
    raise last_err


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


def render_page(relatorio_html, cover, cutoff, model=None):
    provider, configured = provider_info()
    return PAGE_TEMPLATE.format(
        cover=cover, cutoff=cutoff, provider=provider, model=model or configured,
        body=relatorio_html)
