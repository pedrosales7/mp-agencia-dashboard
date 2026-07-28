# MP Agência — Funil Ads-to-Sale (Dashboard HTML)

Dashboard estático (HTML self-contained) do funil de mídia paga dos 8 partners do **MP Agência**, atualizado automaticamente toda semana via GitHub Actions e publicado no GitHub Pages.

**Repo:** [`pedrosales7/mp-agencia-dashboard`](https://github.com/pedrosales7/mp-agencia-dashboard) (público, conta pessoal `pedrosales7`)
**Dashboard ao vivo:** `docs/index.html` via GitHub Pages (URL em `vars.PAGES_URL`)
**Projeto irmão:** [`mp-agencia-dashboard-streamlit`](https://github.com/pedrosales7/mp-agencia-dashboard-streamlit) — mesma lógica de negócio, reimplementada em Streamlit, rodando em paralelo (ver README daquele repo).

---

## 1. Contexto de negócio

**MP Agência** é uma linha de receita da Melhor Plano que opera como agência de performance de mídia paga vertical para **provedores regionais de internet (ISPs)** — os "partners". Modelo comercial: pacote fixo mensal, 100% investido em mídia, sem fee de agência, com **cashback de reinvestimento** quando um lead gerado pela campanha de um partner fecha com outro provedor (a instalação está fora da área de cobertura do anunciante).

**8 partners ativos:** `loga-internet`, `the fiber internet`, `interplus internet`, `direct internet`, `enove-fibra`, `unifique`, `ultranet-network`, `ativa-telecom`.

**2 canais de mídia:**
- **Google Ads** (pesquisa) — funil: `impressões → cliques → sessões → clickoff → redirect → leads → vendas`
- **Meta Ads** (click-to-WhatsApp com bot) — funil: `impressões → cliques → chat_start → zip_search → redirect → leads → vendas`

Owner do produto/coordenação: **Pedro Ribeiro Sales**.

## 2. Regras de negócio (não revisitar sem motivo)

| Regra | Definição |
|---|---|
| **Atribuição** | Lead e venda são sempre atribuídos ao **anunciante** (quem pagou a campanha), nunca ao recebedor. Quando divergem (cashback), o anunciante prevalece no funil. |
| **Venda** | `current_situation IN ('sold','installed','scheduled')` em `checkout.lead_detail` |
| **Lead produtivo** | `source IN ('google','whatsapp') AND lead_accepted = true` |
| **Excluídos** | `whatsapp:paid` e Desktop (`referral_agent_label='mpa.desktop@desktop internet'`) — mesmo schema, outra iniciativa, não são MP Agência |
| **CPL / CAC** | Sempre sobre **investimento líquido** (bruto − cashback) |
| **Cashback alto** | É sinal de **cobertura** (redirect funcionando), não de desperdício de mídia — não tratar como problema por padrão |
| **Cutoff** | Sempre `current_date - 1` (último dia completo; hoje tem dado parcial) |

**Armadilha crítica:** `label_map` pode ter 2 linhas por partner → toda query de funil Meta usa `SELECT DISTINCT id_mp_canon FROM label_map` como CTE base.

Contexto completo de schema/tabelas do Data Warehouse (Redshift, `database_id=69`, Metabase) vive na skill `outputs/mp-data-context/SKILL.md` na pasta local do projeto (`Dashboard-CodeVersion`) — não faz parte deste repo.

## 3. Arquitetura — pipeline automatizado

Desde **2026-07-03** o refresh roda 100% sozinho via **GitHub Actions**, sem depender de sessão local, login ativo ou do Claude aberto (migrado do fluxo antigo via Cowork, que exigia sessão interativa para os conectores MCP de Slack/Drive).

```
GitHub Actions (cron terça 8:37 BRT)
   └─ scripts/weekly_refresh.py
        ├─ 1. Login no Metabase via API (/api/session, usuário/senha em secrets)
        ├─ 2. Roda as 6 queries de scripts/queries.sql via /api/dataset (HTTP direto,
        │      sem o limite de 500 linhas do conector MCP do Metabase)
        ├─ 3. Merge/compacta com data/cache/historical_data.json (histórico rolling 180d)
        ├─ 4. Regenera docs/index.html (in-place)
        ├─ 5. scripts/ai_analysis.py → gera docs/analise.html (opcional, ver seção 5)
        └─ 6. Commit + push (docs/, data/cache/) + posta link no Slack via Incoming Webhook
   └─ GitHub Pages publica docs/index.html a partir de main
```

### Cron e confiabilidade

- **Refresh:** `37 11 * * 2` UTC (terça 8:37 BRT). Minuto quebrado de propósito — evita congestionamento nos `:00/:15/:30` que causa atrasos/runs descartados pelo GitHub (cron do GitHub Actions é best-effort).
- **Watchdog** (`watchdog.yml`, desde 2026-07-14): roda terça `43 13 * * 2` UTC (10:43 BRT, ~2h depois). Confere via API do GitHub se o refresh já rodou com sucesso hoje; se não, alerta no Slack e dispara o refresh via `workflow_dispatch` como fallback. Rede de segurança porque o schedule do GitHub nunca disparou sozinho de forma confiável.
- **Retry de Pages:** o workflow espera o `pages-build-deployment` disparado pelo push e re-roda uma vez se falhar (erro transiente comum da infra do GitHub Pages).
- O step de commit faz `git pull --rebase origin main` antes do push para evitar rejeição por commits concorrentes.

### Disparo manual

```bash
gh auth switch --user pedrosales7   # precisa ser o dono do repo, não só quem tem pull
gh workflow run weekly-refresh.yml --repo pedrosales7/mp-agencia-dashboard -f test_mode=true
```

`test_mode=true` manda a mensagem só para o DM do Pedro no Slack (sem `@channel`) — usar sempre que estiver iterando em correções, nunca disparar no canal público em teste.

## 4. Dados — as 6 queries (`scripts/queries.sql`)

Rodam em sequência (não paralelo) no Metabase, `database_id=69` (Redshift prod):

1. **SNAPSHOT** — resumo por partner no mês corrente (investimento, leads, vendas)
2. **PREV_SNAPSHOT** — mesmo resumo, mês anterior (comparação MoM)
3. **FUNNEL_GOOGLE** — funil ponta a ponta por partner, canal Google. Roda 4× por refresh (janelas 7d/30d/90d/mês corrente)
4. **FUNNEL_META** — funil ponta a ponta por partner, canal Meta/WhatsApp. Mesma lógica de 4 janelas; usa o `DISTINCT id_mp_canon` acima
5. **DAILY_SNAPSHOT** / **DAILY_FUNNEL_GOOGLE** / **DAILY_FUNNEL_META** — séries diárias que alimentam o cache histórico rolling e os gráficos de evolução

`SNAPSHOT`/`PREV_SNAPSHOT` do SQL não são usadas diretamente — `weekly_refresh.py` deriva o equivalente agregando `DAILY_SNAPSHOT` em Python (`agg_daily`), herdado do pipeline manual original.

**Gotcha de atribuição:** no Google, `FUNNEL_GOOGLE` atribui por campanha (`ld.campaign → config.utm_campaign`); as queries de snapshot atribuem por `partner_id_partner`. É design intencional — funil responde "quem passou pela etapa", snapshot responde "quem foi atribuído no CRM" — pequenas divergências de leads no Google entre os dois blocos são esperadas, não bug.

## 5. Análise IA semanal (`scripts/ai_analysis.py`)

Pipeline de **3 estágios**, portado 1:1 (mesmos processamentos, prompts e estrutura) da versão Streamlit do dashboard em 2026-07-28 — só a saída final é diferente (aqui vira HTML publicado + Slack, lá vira JSON lido por um app). Roda dentro do mesmo job de refresh:

- **Estágio 0 — triagem:** Python puro. Monta o payload de KPIs (investimento/leads/vendas/CPL/CAC por partner × canal × janela, etapas de funil, série semanal de 8 semanas, tendências, benchmarks pré-clique, comparativo semana-vs-semana com decomposição de driver) e aplica os limiares fixos de CAC/CPL — não é julgamento de modelo.
- **Estágio 1 — diagnóstico:** Gemini com `responseSchema` (JSON estruturado, thinking alto). Recebe o payload inteiro + o diagnóstico da semana anterior (`data/diagnosticos/AAAA-MM-DD.json`) e produz o parecer estruturado por partner, com toda evidência numérica citada declarada em `evidencias_citadas`.
- **Estágio 2 — redação:** Gemini com tags XML (thinking baixo). Recebe só o JSON do estágio 1 (nunca o payload bruto) — sem números crus na mão, recitar o dashboard fica impossível. Produz pareceres, recomendações, leitura de portfólio e resumo do Slack.

Uma **validação em código** roda depois de cada estágio: rejeita (fatal) partner ausente, status reclassificado pelo modelo ou ação de confiança baixa promovida a prioritária; avisa (não bloqueia) sobre poucas evidências, hipótese ausente ou número citado na prosa que não está em `evidencias_citadas` — essa última é o check que mata a classe de erro "o modelo inventou um número".

**Saída:** `docs/analise.html` (publicado no Pages, montado a partir dos 4 blocos do estágio 2 + a tabela de triagem) + resumo crítico anexado à mensagem semanal do Slack + bloco de **CAC 30d por parceiro ativo (Google/Meta/Conta)** postado na thread da mensagem principal (via bot token, `SLACK_BOT_TOKEN`/`SLACK_CHANNEL_ID` — cai pro Incoming Webhook sem thread se não configurados).

O JSON do estágio 1 é persistido em `data/diagnosticos/` (commitado pelo workflow) — é a entrada do estágio 2 da semana seguinte (bloco "recomendei X, a conta respondeu?") e o registro histórico auditável.

**Gemini-only** (não é mais agnóstico de provedor) — `responseSchema`/`thinkingConfig` são recursos específicos da API Gemini, e o pipeline de 3 estágios não existe sem eles:
- `LLM_API_KEY` (secret) — sem ela, o passo é **pulado silenciosamente** e o refresh roda normal (falha da análise nunca derruba o refresh)
- `LLM_MODEL` (variable, opcional) — default `gemini-3.1-pro-preview` no código; **cuidado:** uma variable esquecida sobrescreve o default do código sem aviso (já causou incidente — checar `gh variable list` antes de trocar modelo default no código)

**Modelo em produção:** `gemini-3.1-pro-preview` via Google AI Studio (não Vertex AI — os dois usam endpoints/autenticação diferentes).

**Regras de estilo dos prompts (`PROMPT_E1`/`PROMPT_E2`), herdadas da versão Streamlit + ajustes deste repo:**
- **Proibido** ser genérico/recitar números que já aparecem no dashboard — exige diagnóstico qualitativo, hipótese de causa raiz e posição sobre o que fazer, tom de consultor
- Analisar **todos os 8 partners**, nunca omitir um partner mesmo estável — mas o **resumo do Slack** cita só contas em alarme/atenção/risco de churn (decisão deste repo, 2026-07-28: pra Slack, só o crítico)
- Comparações: 7d vs 7d_prev (topo de funil) e 30d vs 30d_prev (eficiência), nunca CAC/CPL em 7d (lag de fechamento de venda)
- Recomendações priorizadas só com confiança média/alta
- **Sem emoji em nenhum bloco** (decisão deste repo, 2026-07-28) — instrução no prompt + sanitização por código (`_strip_emoji`), porque o modelo ignora a instrução sozinha de vez em quando
- Checar `gargalo_funil_30d` e tendência de 8 semanas **antes** de cashback/CTR (lição: o modelo só usa o que vem pré-calculado no payload em Python — instrução sozinha no prompt não basta, "salência no dado > instrução no prompt")
- Crédito/runway **fora de escopo** (há alertas dedicados em outro lugar)

Se as regras de negócio do dashboard mudarem (seção 2), atualizar também `PROMPT_E1`/`PROMPT_E2` em `ai_analysis.py` — não é derivado automaticamente.

## 6. Stack e conectores

| Camada | Tecnologia |
|---|---|
| Runtime do refresh | Python 3.12, `requests` (sem framework) |
| Dashboard | HTML/CSS/JS estático, self-contained (sem build step) |
| Dados | Metabase (`database_id=69`) → Redshift prod, via API HTTP direta (`/api/session` + `/api/dataset`) |
| Análise IA | Gemini (AI Studio), pipeline de 3 estágios — ver seção 5 |
| Automação | GitHub Actions (2 workflows: refresh + watchdog) |
| Publicação | GitHub Pages (branch `main`, pasta `docs/`) |
| Notificação | Slack — Incoming Webhook (app **"Bot MP Agência"**, canal `#mp-agencia-logs-e-avisos`, `C0BEATJ7VFB`) para a mensagem principal; bot token (`chat.postMessage`) para a thread do CAC 30d quando configurado |

**Secrets do repo:** `METABASE_URL`, `METABASE_USERNAME`, `METABASE_PASSWORD`, `SLACK_WEBHOOK_URL`, `SLACK_WEBHOOK_URL_TEST`, `SLACK_BOT_TOKEN`, `LLM_API_KEY`
**Variables do repo:** `PAGES_URL`, `SLACK_CHANNEL_ID`, `LLM_MODEL`

Google Drive **não é mais usado** — upload manual era o gargalo que motivou a migração para GitHub Actions.

## 7. Estrutura de arquivos

```
docs/index.html              # dashboard publicado (GitHub Pages)
docs/analise.html            # análise IA semanal publicada
scripts/weekly_refresh.py    # orquestra: Metabase → merge → HTML → commit → Slack
scripts/queries.sql          # as 6 queries, documentadas com [QUERY:NOME]/[/QUERY:NOME]
scripts/ai_analysis.py       # payload de KPIs + chamada ao LLM + render de analise.html
scripts/requirements.txt     # deps do refresh
data/cache/historical_data.json  # estado rolling 180 dias (necessário pq o HTML carrega histórico completo a cada view)
.github/workflows/weekly-refresh.yml
.github/workflows/watchdog.yml
```

## 8. Rodar/depurar localmente

```bash
cd /Users/pedro/Claude/Projects/Dashboard-CodeVersion/mp-agencia-dashboard
pip install -r scripts/requirements.txt
METABASE_URL=... METABASE_USERNAME=... METABASE_PASSWORD=... \
SLACK_WEBHOOK_URL=... SLACK_WEBHOOK_URL_TEST=... TEST_MODE=true \
python scripts/weekly_refresh.py
```

Fallback manual (se o GitHub Actions falhar completamente): fluxo antigo interativo documentado em `outputs/skills/mp-dashboard-refresh/SKILL.md` na pasta local `Dashboard-CodeVersion` — gera o HTML mas **não publica sozinho**, precisa copiar manualmente para `docs/index.html` neste repo.

## 9. Contexto adicional / memória entre sessões

- Discovery, specs e histórico estratégico completo da iniciativa MP Agência: pasta "MP Agencia" (fora deste repo)
- Nota de projeto no Obsidian: `~/Claude/Projects/Obsidian/01-Projects/Dashboard-CodeVersion/Dashboard-CodeVersion.md`
- Schema/definições de negócio do Data Warehouse: `outputs/mp-data-context/SKILL.md` (pasta local `Dashboard-CodeVersion`)
