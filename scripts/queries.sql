-- ============================================================
-- MP Agência — 6 queries de refresh do dashboard
-- Rodar em sequência (não paralelo) no Metabase database_id=69
-- Substituir {{CUTOFF}} pela data correta: current_date - 1
-- cutoff = 'YYYY-MM-DD' (último dia completo)
-- d_ini = primeiro dia do mês corrente (ou 30 dias antes do cutoff)
-- ============================================================


-- ============================================================
-- QUERY 1: SNAPSHOT
-- Resumo por partner no período (mês corrente)
-- ============================================================
-- [QUERY:SNAPSHOT]
WITH periodo AS (
    SELECT DATE_TRUNC('month', '{{CUTOFF}}'::date)::date AS d_ini,
           '{{CUTOFF}}'::date AS d_fim
),
investimento AS (
    SELECT partnership_id, id_mp,
           SUM(raw_investment) AS invest,
           SUM(CASE WHEN source = 'google' THEN raw_investment ELSE 0 END) AS invest_g,
           SUM(CASE WHEN source = 'meta'   THEN raw_investment ELSE 0 END) AS invest_m
    FROM midia_paga.performance_partner_mp_agency p
    JOIN periodo ON p.date BETWEEN periodo.d_ini AND periodo.d_fim
    GROUP BY 1, 2
),
leads_vendas AS (
    SELECT CASE WHEN ld.partner_id_partner = 'enove-solucoes' THEN 'enove-fibra' ELSE ld.partner_id_partner END AS partner_id_partner,
           COUNT(DISTINCT ld.id) AS leads,
           SUM(CASE WHEN ld.current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas,
           SUM(CASE WHEN ld.source = 'google' THEN 1 ELSE 0 END) AS leads_g,
           SUM(CASE WHEN ld.source = 'whatsapp' THEN 1 ELSE 0 END) AS leads_m
    FROM checkout.lead_detail ld
    JOIN periodo ON ld.created_at >= periodo.d_ini AND ld.created_at < periodo.d_fim + INTERVAL '1 day'
    WHERE ld.source IN ('google','whatsapp') AND ld.lead_accepted = true
    GROUP BY 1
),
credito AS (
    SELECT partnership_id, MAX(credit_remaining) AS credito_restante
    FROM midia_paga.performance_partner_mp_agency
    WHERE date = '{{CUTOFF}}'
    GROUP BY 1
)
SELECT
    i.id_mp,
    i.invest, i.invest_g, i.invest_m,
    lv.leads, lv.leads_g, lv.leads_m,
    lv.vendas,
    ROUND(i.invest::numeric / NULLIF(lv.leads, 0), 2) AS cpl,
    ROUND(i.invest::numeric / NULLIF(lv.vendas, 0), 2) AS cac,
    ROUND(lv.vendas::numeric / NULLIF(lv.leads, 0) * 100, 1) AS conv,
    c.credito_restante AS credito
FROM investimento i
LEFT JOIN leads_vendas lv ON lv.partner_id_partner = i.id_mp
LEFT JOIN credito c ON c.partnership_id = i.partnership_id
ORDER BY i.invest DESC NULLS LAST;
-- [/QUERY:SNAPSHOT]


-- ============================================================
-- QUERY 2: PREV_SNAPSHOT
-- Mesmo resumo, mês anterior (para comparação MoM)
-- ============================================================
-- [QUERY:PREV_SNAPSHOT]
WITH periodo AS (
    SELECT DATE_TRUNC('month', DATE_TRUNC('month', '{{CUTOFF}}'::date) - INTERVAL '1 day')::date AS d_ini,
           (DATE_TRUNC('month', '{{CUTOFF}}'::date) - INTERVAL '1 day')::date AS d_fim
),
investimento AS (
    SELECT id_mp,
           SUM(raw_investment) AS invest,
           SUM(CASE WHEN source = 'google' THEN raw_investment ELSE 0 END) AS invest_g,
           SUM(CASE WHEN source = 'meta'   THEN raw_investment ELSE 0 END) AS invest_m
    FROM midia_paga.performance_partner_mp_agency
    JOIN periodo ON date BETWEEN periodo.d_ini AND periodo.d_fim
    GROUP BY 1
),
leads_vendas AS (
    SELECT CASE WHEN partner_id_partner = 'enove-solucoes' THEN 'enove-fibra' ELSE partner_id_partner END AS partner_id_partner,
           COUNT(DISTINCT id) AS leads,
           SUM(CASE WHEN current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas
    FROM checkout.lead_detail
    JOIN periodo ON created_at BETWEEN periodo.d_ini AND periodo.d_fim
    WHERE source IN ('google','whatsapp') AND lead_accepted = true
    GROUP BY 1
)
SELECT
    i.id_mp,
    i.invest, i.invest_g, i.invest_m,
    lv.leads, lv.vendas,
    ROUND(i.invest::numeric / NULLIF(lv.leads, 0), 2) AS cpl,
    ROUND(i.invest::numeric / NULLIF(lv.vendas, 0), 2) AS cac
FROM investimento i
LEFT JOIN leads_vendas lv ON lv.partner_id_partner = i.id_mp
ORDER BY i.invest DESC NULLS LAST;
-- [/QUERY:PREV_SNAPSHOT]


-- ============================================================
-- QUERY 3: FUNNEL_GOOGLE
-- Funil Google ponta a ponta por partner
--
-- CHANGELOG 2026-07-01: removido o filtro `status = 'enabled'` da CTE
-- `config`. Causa raiz do bug "investimento sem cliques" (Rafael):
-- em backoffice.db_backoffice_lead_agency_paid_media_config, os partners
-- enove-fibra (partnership_id 4ebe0769-f89a-4fa4-adb0-c1094a68b20a) e
-- the fiber internet (partnership_id b0e1598d-b699-45f9-9fdf-4252b8453bf1)
-- têm suas linhas de config Google com status='disabled', mesmo tendo
-- investimento real rodando — por isso sumiam do funil de cliques.
-- Decisão do usuário: ignorar o status e contar mesmo assim.
-- Mantido: deleted_at IS NULL AND utm_source = 'google'.
--
-- CHANGELOG 2026-07-01 (2): adicionada CTE `clicks` pra popular o campo
-- `cliques` (cliques de anúncio, distinto de `sessoes`). O join correto
-- é `config.campaign_name = ads.google_ads_campaigns_daily_data.name`
-- (NÃO `config.utm_campaign = name` — esse join não bate, os valores de
-- utm_campaign e o nome real da campanha no Google Ads são diferentes;
-- ver coluna `campaign_name` na tabela de config, que guarda o nome
-- exato da campanha no Ads).
--
-- CHANGELOG 2026-07-02: esta query PRECISA rodar 4x por refresh — uma
-- vez por janela (7d, 30d, 90d, mês corrente), substituindo a CTE
-- `periodo` abaixo pelos d_ini/d_fim de cada janela e tagueando cada
-- resultado com o p_key correspondente antes de mesclar no HTML. Antes
-- só rodava pro mês corrente, o que fazia os p_keys 7d/30d/90d
-- desaparecerem do funil detalhado a cada refresh (KPIs do topo, vindos
-- de SNAPSHOT, continuavam OK — só a tabela de funil detalhado ficava
-- vazia pra esses períodos). Mesma correção aplicada em FUNNEL_META e
-- na tarefa agendada `mp-agencia-dashboard-semanal`.
-- ============================================================
-- [QUERY:FUNNEL_GOOGLE]
WITH periodo AS (
    -- Rodar 1x por janela: (CUTOFF-6, CUTOFF)=7d · (CUTOFF-29, CUTOFF)=30d ·
    -- (CUTOFF-89, CUTOFF)=90d · (primeiro dia do mês de CUTOFF, CUTOFF)=mês corrente
    SELECT DATE_TRUNC('month', '{{CUTOFF}}'::date)::date AS d_ini,
           '{{CUTOFF}}'::date AS d_fim
),
config AS (
    SELECT DISTINCT partnership_id, utm_campaign, campaign_name
    FROM backoffice.db_backoffice_lead_agency_paid_media_config
    WHERE deleted_at IS NULL AND utm_source = 'google'
),
clicks AS (
    SELECT cf.partnership_id, SUM(g.clicks) AS cliques
    FROM ads.google_ads_campaigns_daily_data g
    JOIN config cf ON cf.campaign_name = g.name
    JOIN periodo ON g.date BETWEEN periodo.d_ini AND periodo.d_fim
    GROUP BY 1
),
sessions AS (
    SELECT cf.partnership_id, COUNT(DISTINCT pl.session_id) AS sessoes
    FROM comparison.page_load pl
    JOIN config cf ON cf.utm_campaign = pl.user_landing_page_utm_campaign
    JOIN periodo ON pl._timestamp >= periodo.d_ini AND pl._timestamp < periodo.d_fim + INTERVAL '1 day'
    WHERE LOWER(pl.user_landing_page_utm_source) LIKE '%google%'
    GROUP BY 1
),
zip_search AS (
    SELECT cf.partnership_id, COUNT(*) AS zip_search
    FROM comparison.zip_search_click z
    JOIN config cf ON cf.utm_campaign = z.user_landing_page_utm_campaign
    JOIN periodo ON z._timestamp >= periodo.d_ini AND z._timestamp < periodo.d_fim + INTERVAL '1 day'
    WHERE LOWER(z.user_landing_page_utm_source) LIKE '%google%'
    GROUP BY 1
),
clickoffs AS (
    SELECT cf.partnership_id, COUNT(*) AS clickoffs
    FROM comparison.clickoff c
    JOIN config cf ON cf.utm_campaign = c.user_landing_page_utm_campaign
    JOIN periodo ON c._timestamp >= periodo.d_ini AND c._timestamp < periodo.d_fim + INTERVAL '1 day'
    WHERE LOWER(c.user_landing_page_utm_source) LIKE '%google%'
    GROUP BY 1
),
redirects AS (
    SELECT cf.partnership_id, COUNT(*) AS redirects_total
    FROM comparison.clickoff_redirect cr
    JOIN config cf ON cf.utm_campaign = cr.user_landing_page_utm_campaign
    JOIN periodo ON cr._timestamp >= periodo.d_ini AND cr._timestamp < periodo.d_fim + INTERVAL '1 day'
    WHERE LOWER(cr.user_landing_page_utm_source) LIKE '%google%'
    GROUP BY 1
),
leads_e_vendas AS (
    SELECT cf.partnership_id,
           COUNT(*) AS leads,
           SUM(CASE WHEN ld.current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas
    FROM checkout.lead_detail ld
    JOIN config cf ON cf.utm_campaign = ld.campaign
    JOIN periodo ON ld.created_at >= periodo.d_ini AND ld.created_at < periodo.d_fim + INTERVAL '1 day'
    WHERE ld.source = 'google' AND ld.lead_accepted = true
    GROUP BY 1
)
SELECT
    p.id_mp,
    cl.cliques,
    s.sessoes, zs.zip_search, co.clickoffs,
    r.redirects_total AS redirects,
    lv.leads, lv.vendas
FROM (SELECT DISTINCT partnership_id FROM config) base
LEFT JOIN clicks         cl ON cl.partnership_id = base.partnership_id
LEFT JOIN sessions       s  ON s.partnership_id  = base.partnership_id
LEFT JOIN zip_search     zs ON zs.partnership_id = base.partnership_id
LEFT JOIN clickoffs      co ON co.partnership_id = base.partnership_id
LEFT JOIN redirects      r  ON r.partnership_id  = base.partnership_id
LEFT JOIN leads_e_vendas lv ON lv.partnership_id = base.partnership_id
LEFT JOIN (
    SELECT partnership_id, MAX(id_mp) AS id_mp
    FROM midia_paga.performance_partner_mp_agency
    WHERE date >= '{{CUTOFF}}'::date - 90
    GROUP BY 1
) p ON p.partnership_id = base.partnership_id
ORDER BY cl.cliques DESC NULLS LAST;
-- [/QUERY:FUNNEL_GOOGLE]


-- ============================================================
-- QUERY 4: FUNNEL_META
-- Funil Meta/WhatsApp por partner
-- ARMADILHA CRÍTICA: usar DISTINCT id_mp_canon no label_map
--
-- CHANGELOG 2026-07-01: adicionada CTE `clicks` pra popular o campo
-- `cliques` (estava sempre NULL). NÃO dá pra usar utm_campaign — na
-- config de Meta/WhatsApp esse campo vem vazio pra maioria dos partners
-- (a atribuição de lead usa `referral_agent_label`, não utm_campaign).
-- O join correto é `config.campaign_name = ads.facebook_ads_daily_data.campaign_name`
-- (tabela a nível de anúncio, não `ads.facebook_ads_campaigns_daily_data`,
-- que tem poucas linhas e a maioria marcada "- deleted"). `campaign_name`
-- na tabela de config vem preenchido pra todos os partners mesmo quando
-- utm_campaign está vazio.
--
-- CHANGELOG 2026-07-02: mesma correção do FUNNEL_GOOGLE — esta query
-- PRECISA rodar 4x por refresh (janelas 7d/30d/90d/mês corrente), não
-- só pro mês corrente. Ver nota completa na query FUNNEL_GOOGLE acima.
--
-- CHANGELOG 2026-07-06: troca de `unique_clicks` pra `link_click_unique`
-- na CTE `clicks`. Métrica de referência do Pedro é "cliques no link
-- únicos" do Meta Ads Manager (cliques que efetivamente saíram do anúncio
-- pro destino, sem contar reação/like/comentário). `unique_clicks` cobria
-- qualquer clique único (incluindo interações no post), inflava o número.
-- Nota herdada da versão anterior: dedup do Meta é intraday, então somar
-- linhas diárias ainda pode contar 2x quem clicou em dias distintos do
-- mesmo mês — o Ads Manager, em contraste, pede o intervalo inteiro numa
-- chamada só e faz dedup real. Sem solução trivial pra paridade exata.
--
-- CHANGELOG 2026-07-03 (superseded): `clicks` → `unique_clicks`. Migrado
-- pra link_click_unique acima; nota preservada como histórico do bug de
-- "cliques do dashboard muito acima do Looker" (Pedro).
-- ============================================================
-- [QUERY:FUNNEL_META]
WITH periodo AS (
    -- Rodar 1x por janela: 7d / 30d / 90d / mês corrente (ver FUNNEL_GOOGLE acima)
    SELECT DATE_TRUNC('month', '{{CUTOFF}}'::date)::date AS d_ini,
           '{{CUTOFF}}'::date AS d_fim
),
campaign_config AS (
    SELECT DISTINCT partnership_id, campaign_name
    FROM backoffice.db_backoffice_lead_agency_paid_media_config
    WHERE utm_source IN ('meta','whatsapp') AND campaign_name IS NOT NULL AND campaign_name <> ''
),
clicks AS (
    SELECT cf.partnership_id, SUM(f.link_click_unique) AS cliques
    FROM ads.facebook_ads_daily_data f
    JOIN campaign_config cf ON cf.campaign_name = f.campaign_name
    JOIN periodo ON f.date::date BETWEEN periodo.d_ini AND periodo.d_fim
    GROUP BY 1
),
label_map AS (
    SELECT DISTINCT id_mp AS id_mp_canon, partnership_id, agent_label FROM (
        SELECT 'mpa.loga-internet'                       AS agent_label, '011f62ae-d224-4569-9697-542f959685b2' AS partnership_id, 'loga-internet'      AS id_mp UNION ALL
        SELECT 'mpa.loga-internet@loga-internet',                       '011f62ae-d224-4569-9697-542f959685b2', 'loga-internet'                          UNION ALL
        SELECT 'mpa.the-fiber-internet',                                'b0e1598d-b699-45f9-9fdf-4252b8453bf1', 'the fiber internet'                     UNION ALL
        SELECT 'mpa.interplus internet',                                '27a09b7c-4a5d-469a-a04f-1979a060b64b', 'interplus internet'                     UNION ALL
        SELECT 'mpa.direct-internet',                                   '60c264f6-47ea-44d3-8293-0102737dd211', 'direct internet'                        UNION ALL
        SELECT 'mpa.direct internet@direct internet',                   '60c264f6-47ea-44d3-8293-0102737dd211', 'direct internet'                        UNION ALL
        SELECT 'mpa.enove-fibra@enove-solucoes',                        '4ebe0769-f89a-4fa4-adb0-c1094a68b20a', 'enove-fibra'                            UNION ALL
        SELECT 'mpa.unifique',                                          'dbacdf0e-de80-4c74-a92e-3369a4cb27fd', 'unifique'                               UNION ALL
        SELECT 'mpa.ultranet-network@ultranet-network',                 '964662ae-9d9c-466b-881d-373e832c785f', 'ultranet-network'                       UNION ALL
        SELECT 'mpa.ativa-telecom',                                     '13dc5cc1-1ad6-45fe-8669-06126d622af6', 'ativa-telecom'
    ) x
),
base AS (SELECT DISTINCT id_mp_canon, partnership_id FROM label_map),
invest_por_partner AS (
    SELECT partnership_id, SUM(raw_investment) AS invest
    FROM midia_paga.performance_partner_mp_agency p
    JOIN periodo ON p.date BETWEEN periodo.d_ini AND periodo.d_fim
    WHERE source = 'meta'
    GROUP BY 1
),
chat_start AS (
    SELECT m.partnership_id, COUNT(*) AS conversas
    FROM whatsapp_assistant.wa_chat_start c
    JOIN label_map m ON m.agent_label = c.referral_agent_label
    JOIN periodo ON c._timestamp >= periodo.d_ini AND c._timestamp < periodo.d_fim + INTERVAL '1 day'
    GROUP BY 1
),
zip_search AS (
    SELECT m.partnership_id, COUNT(*) AS zip_search
    FROM whatsapp_assistant.wa_zip_search z
    JOIN label_map m ON m.agent_label = z.referral_agent_label
    JOIN periodo ON z._timestamp >= periodo.d_ini AND z._timestamp < periodo.d_fim + INTERVAL '1 day'
    GROUP BY 1
),
get_plans AS (
    SELECT m.partnership_id, COUNT(*) AS get_plans
    FROM whatsapp_assistant.wa_get_plans g
    JOIN label_map m ON m.agent_label = g.referral_agent_label
    JOIN periodo ON g._timestamp >= periodo.d_ini AND g._timestamp < periodo.d_fim + INTERVAL '1 day'
    GROUP BY 1
),
redirects AS (
    SELECT m.partnership_id,
           COUNT(*) AS redirects,
           SUM(CASE WHEN LOWER(r.plan_provider) = LOWER(m.id_mp_canon) THEN 1 ELSE 0 END) AS red_anunc,
           SUM(CASE WHEN LOWER(r.plan_provider) <> LOWER(m.id_mp_canon) AND r.plan_provider IS NOT NULL THEN 1 ELSE 0 END) AS red_cashback
    FROM whatsapp_assistant.wa_redirect r
    JOIN label_map m ON m.agent_label = r.referral_agent_label
    JOIN periodo ON r._timestamp >= periodo.d_ini AND r._timestamp < periodo.d_fim + INTERVAL '1 day'
    GROUP BY 1
),
leads_e_vendas AS (
    SELECT m.partnership_id,
           COUNT(DISTINCT ld.id) AS leads,
           SUM(CASE WHEN ld.current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas
    FROM checkout.lead_detail ld
    JOIN whatsapp_assistant.wa_chat_start wcs
        ON wcs.user_id = ld.user_id
       AND wcs._timestamp BETWEEN ld.created_at - INTERVAL '7 day' AND ld.created_at
    JOIN label_map m ON m.agent_label = wcs.referral_agent_label
    JOIN periodo ON ld.created_at >= periodo.d_ini AND ld.created_at < periodo.d_fim + INTERVAL '1 day'
    WHERE ld.source = 'whatsapp' AND ld.lead_accepted = true
    GROUP BY 1
)
SELECT
    base.id_mp_canon AS id_mp,
    ip.invest,
    cl.cliques,
    cs.conversas, zs.zip_search, gp.get_plans,
    r.redirects, r.red_anunc, r.red_cashback,
    lv.leads, lv.vendas,
    ROUND(ip.invest::numeric / NULLIF(lv.leads, 0), 2) AS cpl,
    ROUND(ip.invest::numeric / NULLIF(lv.vendas, 0), 2) AS cac,
    ROUND(r.red_cashback::numeric / NULLIF(r.redirects, 0) * 100, 1) AS pct_cashback
FROM base
LEFT JOIN invest_por_partner ip ON ip.partnership_id = base.partnership_id
LEFT JOIN clicks             cl ON cl.partnership_id = base.partnership_id
LEFT JOIN chat_start         cs ON cs.partnership_id = base.partnership_id
LEFT JOIN zip_search         zs ON zs.partnership_id = base.partnership_id
LEFT JOIN get_plans          gp ON gp.partnership_id = base.partnership_id
LEFT JOIN redirects          r  ON r.partnership_id  = base.partnership_id
LEFT JOIN leads_e_vendas     lv ON lv.partnership_id = base.partnership_id
ORDER BY ip.invest DESC NULLS LAST;
-- [/QUERY:FUNNEL_META]


-- ============================================================
-- QUERY 5: CREDIT_TIMESERIES
-- Evolução semanal de crédito restante (últimas 10 semanas)
-- `total` = SUM(total_credit)/COUNT(DISTINCT date): cada dia tem 2 linhas
-- (google + meta), cada uma com o mesmo total_credit, então a divisão
-- dá o valor do contrato combinado (google + meta).
-- ============================================================
-- [QUERY:CREDIT_TIMESERIES]
SELECT
    DATE_TRUNC('week', date)::date AS semana,
    id_mp,
    AVG(credit_remaining) AS credito,
    SUM(total_credit) / COUNT(DISTINCT date) AS total
FROM midia_paga.performance_partner_mp_agency
WHERE date BETWEEN '{{CUTOFF}}'::date - 70 AND '{{CUTOFF}}'::date
  AND credit_remaining IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 2;
-- [/QUERY:CREDIT_TIMESERIES]


-- ============================================================
-- QUERY 6: PARTNER_WEEKLY
-- Performance semanal com breakdown por canal (google / meta)
-- CHANGELOG 2026-07-02: expandido com cliques_g/m, clickoff_g,
-- chat_start_m, leads_g/m, vendas_g/m, cashback, liquido.
-- ============================================================
-- [QUERY:PARTNER_WEEKLY]
WITH semanas AS (
    SELECT DATE_TRUNC('week', date)::date AS semana,
           partnership_id, id_mp,
           SUM(raw_investment)              AS bruto,
           SUM(cashback)                    AS cashback,
           SUM(partnership_net_daily_spend) AS liquido
    FROM midia_paga.performance_partner_mp_agency
    WHERE date BETWEEN '{{WEEKLY_START}}'::date AND '{{CUTOFF}}'::date
    GROUP BY 1, 2, 3
),
pid AS (SELECT DISTINCT partnership_id, id_mp FROM semanas),
config_g AS (
    SELECT DISTINCT partnership_id, utm_campaign, campaign_name
    FROM backoffice.db_backoffice_lead_agency_paid_media_config
    WHERE deleted_at IS NULL AND utm_source = 'google'
),
config_m AS (
    SELECT DISTINCT partnership_id, campaign_name
    FROM backoffice.db_backoffice_lead_agency_paid_media_config
    WHERE utm_source IN ('meta','whatsapp') AND campaign_name IS NOT NULL AND campaign_name <> ''
),
label_map AS (
    SELECT DISTINCT id_mp AS id_mp_canon, partnership_id, agent_label FROM (
        SELECT 'mpa.loga-internet'                       AS agent_label, '011f62ae-d224-4569-9697-542f959685b2' AS partnership_id, 'loga-internet'      AS id_mp UNION ALL
        SELECT 'mpa.loga-internet@loga-internet',                       '011f62ae-d224-4569-9697-542f959685b2', 'loga-internet'                          UNION ALL
        SELECT 'mpa.the-fiber-internet',                                'b0e1598d-b699-45f9-9fdf-4252b8453bf1', 'the fiber internet'                     UNION ALL
        SELECT 'mpa.interplus internet',                                '27a09b7c-4a5d-469a-a04f-1979a060b64b', 'interplus internet'                     UNION ALL
        SELECT 'mpa.direct-internet',                                   '60c264f6-47ea-44d3-8293-0102737dd211', 'direct internet'                        UNION ALL
        SELECT 'mpa.direct internet@direct internet',                   '60c264f6-47ea-44d3-8293-0102737dd211', 'direct internet'                        UNION ALL
        SELECT 'mpa.enove-fibra@enove-solucoes',                        '4ebe0769-f89a-4fa4-adb0-c1094a68b20a', 'enove-fibra'                            UNION ALL
        SELECT 'mpa.unifique',                                          'dbacdf0e-de80-4c74-a92e-3369a4cb27fd', 'unifique'                               UNION ALL
        SELECT 'mpa.ultranet-network@ultranet-network',                 '964662ae-9d9c-466b-881d-373e832c785f', 'ultranet-network'                       UNION ALL
        SELECT 'mpa.ativa-telecom',                                     '13dc5cc1-1ad6-45fe-8669-06126d622af6', 'ativa-telecom'
    ) x
),
cliques_g AS (
    SELECT DATE_TRUNC('week', g.date)::date AS semana, p.id_mp, SUM(g.clicks) AS cliques_g
    FROM ads.google_ads_campaigns_daily_data g
    JOIN config_g cf ON cf.campaign_name = g.name
    JOIN pid p ON p.partnership_id = cf.partnership_id
    WHERE g.date BETWEEN '{{WEEKLY_START}}'::date AND '{{CUTOFF}}'::date
    GROUP BY 1, 2
),
cliques_m AS (
    -- CHANGELOG 2026-07-06: `unique_clicks` -> `link_click_unique` (pareia com o "cliques no link únicos" do Meta Ads Manager, que é a métrica de referência do Pedro).
    SELECT DATE_TRUNC('week', f.date::date)::date AS semana, p.id_mp, SUM(f.link_click_unique) AS cliques_m
    FROM ads.facebook_ads_daily_data f
    JOIN config_m cf ON cf.campaign_name = f.campaign_name
    JOIN pid p ON p.partnership_id = cf.partnership_id
    WHERE f.date::date BETWEEN '{{WEEKLY_START}}'::date AND '{{CUTOFF}}'::date
    GROUP BY 1, 2
),
clickoff_g AS (
    SELECT DATE_TRUNC('week', c._timestamp)::date AS semana, p.id_mp, COUNT(*) AS clickoff_g
    FROM comparison.clickoff c
    JOIN config_g cf ON cf.utm_campaign = c.user_landing_page_utm_campaign
    JOIN pid p ON p.partnership_id = cf.partnership_id
    WHERE c._timestamp >= '{{WEEKLY_START}}'::date AND c._timestamp < '{{CUTOFF}}'::date + INTERVAL '1 day'
      AND LOWER(c.user_landing_page_utm_source) LIKE '%google%'
    GROUP BY 1, 2
),
chat_start_m AS (
    SELECT DATE_TRUNC('week', cs._timestamp)::date AS semana, lm.id_mp_canon AS id_mp, COUNT(*) AS chat_start_m
    FROM whatsapp_assistant.wa_chat_start cs
    JOIN label_map lm ON lm.agent_label = cs.referral_agent_label
    WHERE cs._timestamp >= '{{WEEKLY_START}}'::date AND cs._timestamp < '{{CUTOFF}}'::date + INTERVAL '1 day'
    GROUP BY 1, 2
),
leads_semana AS (
    SELECT DATE_TRUNC('week', created_at)::date AS semana,
           CASE WHEN partner_id_partner = 'enove-solucoes' THEN 'enove-fibra' ELSE partner_id_partner END AS id_mp,
           SUM(CASE WHEN source = 'google'   THEN 1 ELSE 0 END) AS leads_g,
           SUM(CASE WHEN source = 'google'   AND current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas_g,
           SUM(CASE WHEN source = 'whatsapp' THEN 1 ELSE 0 END) AS leads_m,
           SUM(CASE WHEN source = 'whatsapp' AND current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas_m
    FROM checkout.lead_detail
    WHERE source IN ('google','whatsapp') AND lead_accepted = true
      AND created_at >= '{{WEEKLY_START}}'::date AND created_at < '{{CUTOFF}}'::date + INTERVAL '1 day'
    GROUP BY 1, 2
)
SELECT s.semana, s.id_mp,
       ROUND(s.bruto) AS bruto, ROUND(s.cashback) AS cashback, ROUND(s.liquido) AS liquido,
       COALESCE(cg.cliques_g,   0) AS cliques_g,
       COALESCE(cm.cliques_m,   0) AS cliques_m,
       COALESCE(co.clickoff_g,  0) AS clickoff_g,
       COALESCE(cs.chat_start_m,0) AS chat_start_m,
       COALESCE(l.leads_g,   0) AS leads_g,
       COALESCE(l.vendas_g,  0) AS vendas_g,
       COALESCE(l.leads_m,   0) AS leads_m,
       COALESCE(l.vendas_m,  0) AS vendas_m
FROM semanas s
LEFT JOIN cliques_g    cg ON cg.semana = s.semana AND cg.id_mp = s.id_mp
LEFT JOIN cliques_m    cm ON cm.semana = s.semana AND cm.id_mp = s.id_mp
LEFT JOIN clickoff_g   co ON co.semana = s.semana AND co.id_mp = s.id_mp
LEFT JOIN chat_start_m cs ON cs.semana = s.semana AND cs.id_mp = s.id_mp
LEFT JOIN leads_semana l  ON l.semana  = s.semana AND l.id_mp  = s.id_mp
ORDER BY s.semana, s.bruto DESC NULLS LAST;
-- [/QUERY:PARTNER_WEEKLY]


-- ============================================================
-- QUERY 7: DAILY_SNAPSHOT
-- Granularidade diária por partner e canal, últimos 180 dias
-- a partir do cutoff. Adicionada em 2026-07-01 para suportar o
-- seletor de período customizado no dashboard (Rafael/Thaís).
-- Mesma lógica de atribuição/lead produtivo/venda da query SNAPSHOT,
-- mas agrupada por dia em vez de por mês, e por canal via `source`
-- (google→canal google, whatsapp→canal meta).
-- Resultado esperado: ~8 partners × 2 canais × 180 dias ≈ 2880 linhas.
-- Pode ser necessário paginar no Metabase (row_limit até 500).
-- ============================================================
-- [QUERY:DAILY_SNAPSHOT]
WITH periodo AS (
    SELECT '{{CUTOFF}}'::date - 179 AS d_ini,
           '{{CUTOFF}}'::date       AS d_fim
),
investimento AS (
    SELECT p.date AS dia,
           id_mp,
           CASE WHEN source = 'google' THEN 'google' ELSE 'meta' END AS canal,
           SUM(raw_investment) AS bruto,
           SUM(cashback) AS cashback,
           SUM(partnership_net_daily_spend) AS liquido
    FROM midia_paga.performance_partner_mp_agency p
    JOIN periodo ON p.date BETWEEN periodo.d_ini AND periodo.d_fim
    WHERE source IN ('google','meta')
    GROUP BY 1, 2, 3
),
leads_vendas AS (
    SELECT ld.created_at::date AS dia,
           CASE WHEN ld.partner_id_partner = 'enove-solucoes' THEN 'enove-fibra' ELSE ld.partner_id_partner END AS id_mp,
           CASE WHEN ld.source = 'google' THEN 'google' ELSE 'meta' END AS canal,
           COUNT(DISTINCT ld.id) AS leads,
           SUM(CASE WHEN ld.current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas
    FROM checkout.lead_detail ld
    JOIN periodo ON ld.created_at::date BETWEEN periodo.d_ini AND periodo.d_fim
    WHERE ld.source IN ('google','whatsapp') AND ld.lead_accepted = true
    GROUP BY 1, 2, 3
)
SELECT
    COALESCE(i.dia, lv.dia) AS dia,
    COALESCE(i.id_mp, lv.id_mp) AS id_mp,
    COALESCE(i.canal, lv.canal) AS canal,
    COALESCE(i.bruto, 0) AS bruto,
    COALESCE(i.cashback, 0) AS cashback,
    COALESCE(i.liquido, 0) AS liquido,
    COALESCE(lv.leads, 0) AS leads,
    COALESCE(lv.vendas, 0) AS vendas
FROM investimento i
FULL OUTER JOIN leads_vendas lv
    ON lv.dia = i.dia AND lv.id_mp = i.id_mp AND lv.canal = i.canal
ORDER BY 1, 2, 3;
-- [/QUERY:DAILY_SNAPSHOT]


-- ============================================================
-- QUERY 8: DAILY_FUNNEL_GOOGLE
-- Granularidade diária das etapas do funil Google (cliques/sessões/
-- clickoff/redirect) por partner, últimos 180 dias a partir do cutoff.
-- Adicionada em 2026-07-03 pra permitir que "Funil completo Google"
-- funcione também com o filtro de período customizado (antes só
-- funcionava para 7d/30d/90d/mês, porque DAILY_SNAPSHOT não guarda
-- essas etapas intermediárias, só investimento/leads/vendas).
--
-- Nota de validação: "sessões" usa COUNT(DISTINCT session_id) — ao
-- agregar por dia e depois somar num range, uma sessão que atravessa
-- a virada da meia-noite pode ser contada 2x (uma por dia). Validado
-- contra o funil de 7d em produção: erro de ~1% nesse campo específico,
-- 0% nos demais (cliques/clickoff/redirect são aditivos, sem esse problema).
-- ============================================================
-- [QUERY:DAILY_FUNNEL_GOOGLE]
WITH periodo AS (
    SELECT '{{CUTOFF}}'::date - 179 AS d_ini,
           '{{CUTOFF}}'::date       AS d_fim
),
config AS (
    SELECT DISTINCT partnership_id, utm_campaign, campaign_name
    FROM backoffice.db_backoffice_lead_agency_paid_media_config
    WHERE deleted_at IS NULL AND utm_source = 'google'
),
clicks AS (
    SELECT cf.partnership_id, g.date AS dia, SUM(g.clicks) AS cliques
    FROM ads.google_ads_campaigns_daily_data g
    JOIN config cf ON cf.campaign_name = g.name
    JOIN periodo ON g.date BETWEEN periodo.d_ini AND periodo.d_fim
    GROUP BY 1, 2
),
sessions AS (
    SELECT cf.partnership_id, pl._timestamp::date AS dia, COUNT(DISTINCT pl.session_id) AS sessoes
    FROM comparison.page_load pl
    JOIN config cf ON cf.utm_campaign = pl.user_landing_page_utm_campaign
    JOIN periodo ON pl._timestamp >= periodo.d_ini AND pl._timestamp < periodo.d_fim + INTERVAL '1 day'
    WHERE LOWER(pl.user_landing_page_utm_source) LIKE '%google%'
    GROUP BY 1, 2
),
clickoffs AS (
    SELECT cf.partnership_id, c._timestamp::date AS dia, COUNT(*) AS clickoffs
    FROM comparison.clickoff c
    JOIN config cf ON cf.utm_campaign = c.user_landing_page_utm_campaign
    JOIN periodo ON c._timestamp >= periodo.d_ini AND c._timestamp < periodo.d_fim + INTERVAL '1 day'
    WHERE LOWER(c.user_landing_page_utm_source) LIKE '%google%'
    GROUP BY 1, 2
),
redirects AS (
    SELECT cf.partnership_id, cr._timestamp::date AS dia, COUNT(*) AS redirects_total
    FROM comparison.clickoff_redirect cr
    JOIN config cf ON cf.utm_campaign = cr.user_landing_page_utm_campaign
    JOIN periodo ON cr._timestamp >= periodo.d_ini AND cr._timestamp < periodo.d_fim + INTERVAL '1 day'
    WHERE LOWER(cr.user_landing_page_utm_source) LIKE '%google%'
    GROUP BY 1, 2
),
leads_e_vendas AS (
    SELECT cf.partnership_id, ld.created_at::date AS dia,
           COUNT(*) AS leads,
           SUM(CASE WHEN ld.current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas
    FROM checkout.lead_detail ld
    JOIN config cf ON cf.utm_campaign = ld.campaign
    JOIN periodo ON ld.created_at >= periodo.d_ini AND ld.created_at < periodo.d_fim + INTERVAL '1 day'
    WHERE ld.source = 'google' AND ld.lead_accepted = true
    GROUP BY 1, 2
),
days_partners AS (
    SELECT DISTINCT dia, partnership_id FROM (
        SELECT dia, partnership_id FROM clicks
        UNION SELECT dia, partnership_id FROM sessions
        UNION SELECT dia, partnership_id FROM clickoffs
        UNION SELECT dia, partnership_id FROM redirects
        UNION SELECT dia, partnership_id FROM leads_e_vendas
    ) u
),
mapping AS (
    SELECT partnership_id, MAX(id_mp) AS id_mp
    FROM midia_paga.performance_partner_mp_agency
    WHERE date >= '{{CUTOFF}}'::date - 269
    GROUP BY 1
)
SELECT
    dp.dia, m.id_mp,
    COALESCE(cl.cliques,0) AS cliques,
    COALESCE(s.sessoes,0) AS sessoes,
    COALESCE(co.clickoffs,0) AS clickoff,
    COALESCE(r.redirects_total,0) AS redirect,
    COALESCE(lv.leads,0) AS leads,
    COALESCE(lv.vendas,0) AS vendas
FROM days_partners dp
JOIN mapping m ON m.partnership_id = dp.partnership_id
LEFT JOIN clicks cl ON cl.partnership_id = dp.partnership_id AND cl.dia = dp.dia
LEFT JOIN sessions s ON s.partnership_id = dp.partnership_id AND s.dia = dp.dia
LEFT JOIN clickoffs co ON co.partnership_id = dp.partnership_id AND co.dia = dp.dia
LEFT JOIN redirects r ON r.partnership_id = dp.partnership_id AND r.dia = dp.dia
LEFT JOIN leads_e_vendas lv ON lv.partnership_id = dp.partnership_id AND lv.dia = dp.dia
ORDER BY 1, 2;
-- [/QUERY:DAILY_FUNNEL_GOOGLE]


-- ============================================================
-- QUERY 9: DAILY_FUNNEL_META
-- Equivalente ao DAILY_FUNNEL_GOOGLE, pro funil Meta/WhatsApp
-- (cliques/chat_start/zip_search/redirect). ARMADILHA CRÍTICA:
-- usar DISTINCT id_mp_canon no label_map, igual FUNNEL_META.
-- Validado contra o funil de 7d em produção: bate exato em todos
-- os campos (nenhum usa COUNT DISTINCT, então não tem o problema
-- de fronteira de dia que existe em DAILY_FUNNEL_GOOGLE.sessoes).
--
-- CHANGELOG 2026-07-03: `clicks` -> `unique_clicks` na CTE `clicks`,
-- mesma correção da FUNNEL_META (ver changelog lá pra detalhe da causa
-- raiz e da limitação de paridade com o Looker).
-- ============================================================
-- [QUERY:DAILY_FUNNEL_META]
WITH periodo AS (
    SELECT '{{CUTOFF}}'::date - 179 AS d_ini,
           '{{CUTOFF}}'::date       AS d_fim
),
campaign_config AS (
    SELECT DISTINCT partnership_id, campaign_name
    FROM backoffice.db_backoffice_lead_agency_paid_media_config
    WHERE utm_source IN ('meta','whatsapp') AND campaign_name IS NOT NULL AND campaign_name <> ''
),
clicks AS (
    SELECT cf.partnership_id, f.date::date AS dia, SUM(f.link_click_unique) AS cliques
    FROM ads.facebook_ads_daily_data f
    JOIN campaign_config cf ON cf.campaign_name = f.campaign_name
    JOIN periodo ON f.date::date BETWEEN periodo.d_ini AND periodo.d_fim
    GROUP BY 1, 2
),
label_map AS (
    SELECT DISTINCT id_mp AS id_mp_canon, partnership_id, agent_label FROM (
        SELECT 'mpa.loga-internet'                       AS agent_label, '011f62ae-d224-4569-9697-542f959685b2' AS partnership_id, 'loga-internet'      AS id_mp UNION ALL
        SELECT 'mpa.loga-internet@loga-internet',                       '011f62ae-d224-4569-9697-542f959685b2', 'loga-internet'                          UNION ALL
        SELECT 'mpa.the-fiber-internet',                                'b0e1598d-b699-45f9-9fdf-4252b8453bf1', 'the fiber internet'                     UNION ALL
        SELECT 'mpa.interplus internet',                                '27a09b7c-4a5d-469a-a04f-1979a060b64b', 'interplus internet'                     UNION ALL
        SELECT 'mpa.direct-internet',                                   '60c264f6-47ea-44d3-8293-0102737dd211', 'direct internet'                        UNION ALL
        SELECT 'mpa.direct internet@direct internet',                   '60c264f6-47ea-44d3-8293-0102737dd211', 'direct internet'                        UNION ALL
        SELECT 'mpa.enove-fibra@enove-solucoes',                        '4ebe0769-f89a-4fa4-adb0-c1094a68b20a', 'enove-fibra'                            UNION ALL
        SELECT 'mpa.unifique',                                          'dbacdf0e-de80-4c74-a92e-3369a4cb27fd', 'unifique'                               UNION ALL
        SELECT 'mpa.ultranet-network@ultranet-network',                 '964662ae-9d9c-466b-881d-373e832c785f', 'ultranet-network'                       UNION ALL
        SELECT 'mpa.ativa-telecom',                                     '13dc5cc1-1ad6-45fe-8669-06126d622af6', 'ativa-telecom'
    ) x
),
chat_start AS (
    SELECT m.partnership_id, c._timestamp::date AS dia, COUNT(*) AS conversas
    FROM whatsapp_assistant.wa_chat_start c
    JOIN label_map m ON m.agent_label = c.referral_agent_label
    JOIN periodo ON c._timestamp >= periodo.d_ini AND c._timestamp < periodo.d_fim + INTERVAL '1 day'
    GROUP BY 1, 2
),
zip_search AS (
    SELECT m.partnership_id, z._timestamp::date AS dia, COUNT(*) AS zip_search
    FROM whatsapp_assistant.wa_zip_search z
    JOIN label_map m ON m.agent_label = z.referral_agent_label
    JOIN periodo ON z._timestamp >= periodo.d_ini AND z._timestamp < periodo.d_fim + INTERVAL '1 day'
    GROUP BY 1, 2
),
redirects AS (
    SELECT m.partnership_id, r._timestamp::date AS dia, COUNT(*) AS redirects
    FROM whatsapp_assistant.wa_redirect r
    JOIN label_map m ON m.agent_label = r.referral_agent_label
    JOIN periodo ON r._timestamp >= periodo.d_ini AND r._timestamp < periodo.d_fim + INTERVAL '1 day'
    GROUP BY 1, 2
),
leads_e_vendas AS (
    SELECT m.partnership_id, ld.created_at::date AS dia,
           COUNT(DISTINCT ld.id) AS leads,
           SUM(CASE WHEN ld.current_situation IN ('sold','installed','scheduled') THEN 1 ELSE 0 END) AS vendas
    FROM checkout.lead_detail ld
    JOIN whatsapp_assistant.wa_chat_start wcs
        ON wcs.user_id = ld.user_id
       AND wcs._timestamp BETWEEN ld.created_at - INTERVAL '7 day' AND ld.created_at
    JOIN label_map m ON m.agent_label = wcs.referral_agent_label
    JOIN periodo ON ld.created_at >= periodo.d_ini AND ld.created_at < periodo.d_fim + INTERVAL '1 day'
    WHERE ld.source = 'whatsapp' AND ld.lead_accepted = true
    GROUP BY 1, 2
),
base AS (SELECT DISTINCT id_mp_canon, partnership_id FROM label_map),
days_partners AS (
    SELECT DISTINCT dia, partnership_id FROM (
        SELECT dia, partnership_id FROM clicks
        UNION SELECT dia, partnership_id FROM chat_start
        UNION SELECT dia, partnership_id FROM zip_search
        UNION SELECT dia, partnership_id FROM redirects
        UNION SELECT dia, partnership_id FROM leads_e_vendas
    ) u
)
SELECT dp.dia, b.id_mp_canon AS id_mp,
       COALESCE(cl.cliques,0) AS cliques,
       COALESCE(cs.conversas,0) AS chat_start,
       COALESCE(zs.zip_search,0) AS zip_search,
       COALESCE(r.redirects,0) AS redirect,
       COALESCE(lv.leads,0) AS leads,
       COALESCE(lv.vendas,0) AS vendas
FROM days_partners dp
JOIN base b ON b.partnership_id = dp.partnership_id
LEFT JOIN clicks cl ON cl.partnership_id = dp.partnership_id AND cl.dia = dp.dia
LEFT JOIN chat_start cs ON cs.partnership_id = dp.partnership_id AND cs.dia = dp.dia
LEFT JOIN zip_search zs ON zs.partnership_id = dp.partnership_id AND zs.dia = dp.dia
LEFT JOIN redirects r ON r.partnership_id = dp.partnership_id AND r.dia = dp.dia
LEFT JOIN leads_e_vendas lv ON lv.partnership_id = dp.partnership_id AND lv.dia = dp.dia
ORDER BY 1, 2;
-- [/QUERY:DAILY_FUNNEL_META]
