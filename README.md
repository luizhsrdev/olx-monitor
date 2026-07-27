# OLX Monitor

Monitor de anúncios de marketplace com alertas em tempo real no Telegram.
Consulta continuamente URLs de busca da OLX e notifica assim que aparece um
anúncio novo que atenda aos critérios configurados — sem depender de busca
salva/e-mail da própria OLX, que chegam tarde demais.

O código não conhece nenhum produto específico (PS5, Switch, MacBook, ...).
Todo esse conhecimento vive em `monitores.yaml`. Adicionar um produto novo é
editar YAML, nunca `.py`.

## Arquitetura

```
BUSCA (config) → COLETA → NORMALIZAÇÃO → FILTRO → DEDUPE → ALERTA
                 [fonte]    [formato]     [regras]  [SQLite]  [canal]
```

| Estágio | Módulo | Responsabilidade |
|---|---|---|
| Coleta | `olx_monitor/sources/olx.py` | Baixa a busca da OLX (browser persistente + circuit breaker) e devolve anúncios crus |
| Normalização | `olx_monitor/normalize.py` | Converte o formato cru de cada fonte em `Anuncio` |
| Filtro | `olx_monitor/filters.py` | Aplica `bloqueadas` / `obrigatorias_ou` / `preco_max` / `prioritarias` |
| Dedupe | `olx_monitor/dedupe.py` | SQLite — nunca notifica o mesmo anúncio duas vezes |
| Alerta | `olx_monitor/alerts/telegram.py` | Envia a notificação (rápido) e depois edita com dados do vendedor |
| Enriquecimento | `olx_monitor/enrichment.py` + `seller_info.py` | Worker em background que busca dados do vendedor sem atrasar a notificação |
| Orquestração | `olx_monitor/scheduler.py` | Uma thread por monitor, com seu próprio intervalo |

`olx_monitor/rsc.py` é um utilitário compartilhado (decodifica o formato de
streaming RSC da OLX) usado tanto pela coleta quanto pelo enriquecimento —
não é um estágio do pipeline em si.

Fonte e canal de alerta são interfaces (`sources/base.py`, `alerts/base.py`).
Hoje só existe o adaptador OLX e o canal Telegram — Mercado Livre, Discord etc.
não foram implementados de propósito (ver `SPEC.md`, seção "Não-objetivos").

## Instalação

```bash
git clone <este repositório>
cd olx-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Se você pretende usar `modo: playwright` em algum monitor (fallback para
quando a OLX bloqueia requisições simples), instale também o navegador:

```bash
playwright install chromium
```

Copie os arquivos de exemplo:

```bash
cp .env.example .env
cp monitores.yaml.example monitores.yaml
```

Edite `.env` com suas credenciais do Telegram (veja abaixo) e `monitores.yaml`
com seus produtos. Ambos os arquivos já estão no `.gitignore` — nunca serão
commitados.

## Criando o bot no Telegram e descobrindo o `chat_id`

1. No Telegram, fale com **@BotFather** → `/newbot` → siga as instruções.
   Ao final ele te dá um **token** (formato `123456789:ABC-...`). Esse é o
   `TELEGRAM_TOKEN`.
2. Envie qualquer mensagem para o seu bot recém-criado (procure pelo
   username que você escolheu e clique em "Start").
3. Descubra seu `chat_id` acessando no navegador, logado no Telegram Web
   (ou via `curl`):
   ```
   https://api.telegram.org/bot<SEU_TOKEN>/getUpdates
   ```
   Na resposta JSON, procure `"chat":{"id": ...}`. Esse número (pode ser
   negativo, se for grupo) é o `TELEGRAM_CHAT_ID`.
4. Preencha os dois valores em `.env`.

Se `getUpdates` vier vazio, mande outra mensagem para o bot e tente de novo —
o Telegram só retorna updates que ainda não foram "consumidos".

Depois de preencher o `.env`, dá pra confirmar que token e chat_id estão
certos sem subir o monitor de verdade:

```bash
python -m olx_monitor.test_telegram
```

Isso envia uma mensagem de anúncio fake ("[TESTE] PlayStation 5 lacrado...")
usando o `TelegramNotifier` de verdade. Se falhar, o erro do Telegram é
impresso direto (401 = token errado, "chat not found" = chat_id errado).
Comando de depuração temporário — não faz parte do pipeline.

## Montando uma URL de busca da OLX

1. Vá em olx.com.br, escolha a categoria e faça a busca com os filtros que
   quiser (palavra-chave, faixa de preço, "aceita entrega" etc.).
2. Ordene por "Mais recentes" (`sf=1`) — essencial para o monitor sempre ver
   os anúncios mais novos primeiro.
3. Copie a URL da barra de endereço inteira, com todos os query params
   (`ps`, `pe`, `q`, `sf`, `opst`, ...), e cole em `urls:` no monitor.
4. Você pode colocar mais de uma URL no mesmo monitor (ex.: duas faixas de
   preço diferentes) — todas são consultadas a cada ciclo.

## Adicionando um produto novo

Edite `monitores.yaml` e adicione um bloco em `monitores:` — não precisa
tocar em nenhum `.py`:

```yaml
  - nome: "Nintendo Switch 2"
    ativo: true
    fonte: olx
    intervalo_segundos: 90
    urls:
      - "<sua URL de busca>"
    preco_max: 3000
    bloqueadas: ["joy-con", "case", "jogo avulso"]
    obrigatorias_ou: ["switch 2"]
    prioritarias: ["lacrado", "novo"]
```

Semântica dos campos de filtro:

- **`bloqueadas`** (+ `bloqueadas_globais`, que valem para todos os
  monitores) — se o título contiver qualquer um destes termos, descarta.
- **`obrigatorias_ou`** — o título precisa conter **pelo menos um** destes
  termos. É o filtro mais importante: mais robusto que tentar enumerar tudo
  que deveria ser bloqueado.
- **`prioritarias`** — não filtra nada, só marca o alerta como prioridade
  alta (🔥 no Telegram). A mensagem mostra quais termos bateram, ex.:
  `🔥 PRIORITÁRIO (lacrado, 1tb)` — útil pra decidir de relance qual alerta
  atacar primeiro quando chegam vários juntos. A notificação sai na hora,
  sem dados do vendedor (ver seção "Dados do vendedor" abaixo) — eles chegam
  editando a mesma mensagem alguns segundos depois.
- Toda comparação de texto é case-insensitive e insensível a acento
  ("peças" e "pecas" são tratados como iguais).
- `ativo: false` desliga o monitor sem apagar a configuração.
- `intervalo_segundos` nunca pode ser menor que **30s** — a config recusa
  carregar com uma mensagem clara se você tentar. Isso existe para não levar
  rate limit/bloqueio da OLX.

Na primeira rodada de um monitor novo (banco ainda sem registro para aquele
`nome`), o monitor só popula o SQLite e não notifica nada — assim você não
leva uma enxurrada de alertas de anúncios antigos.

## Painel de edição (Streamlit)

Se editar `monitores.yaml` na mão for atrito demais, tem um painel local:

```bash
streamlit run app.py
```

Abre em `http://localhost:8501`. Deixa listar monitores (com toggle de
ativo/inativo direto na lista), criar, editar e remover (com confirmação)
sem precisar saber sintaxe YAML. É **só** uma ferramenta de conveniência
local — não roda o monitoramento nem interage com o `run.py` de forma
alguma, e não tem autenticação (não deveria ser exposto além do
`localhost`).

Duas coisas a saber:

- Cada salvamento **reescreve `monitores.yaml` inteiro** — os dados de
  monitores não tocados são preservados, mas comentários/formatação
  manuais que você tenha adicionado ao arquivo real são perdidos. Os
  comentários explicando cada campo continuam intactos em
  `monitores.yaml.example`, que o painel nunca toca.
- Os placeholders `${TELEGRAM_TOKEN}` / `${TELEGRAM_CHAT_ID}` do bloco
  `telegram:` nunca são resolvidos pelo painel (ele lê o YAML cru, sem
  substituição de variável de ambiente), então voltam pro arquivo
  exatamente como estavam — o painel não edita esse bloco de qualquer
  forma.

## Rodando

```bash
python run.py
# ou, explicitando os caminhos:
python run.py --config monitores.yaml --db olx_monitor.db --log-level INFO
```

`Ctrl+C` encerra todos os monitores de forma limpa.

## Deploy 24/7 (systemd)

1. Copie o **código** do projeto para o servidor, ex. `/opt/olx-monitor`
   (`git clone` direto lá é o mais simples). **Não copie o `.venv` da sua
   máquina de dev** — um venv Python não é portável entre plataformas
   (ex.: Mac → Linux/VPS quebra na certa). Crie um venv novo *no servidor*
   e instale as dependências lá:
   ```bash
   cd /opt/olx-monitor
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/playwright install chromium  # só se algum monitor usar modo: playwright
   ```
   Depois configure `.env` e `monitores.yaml` diretamente no servidor (veja
   "Instalação" acima) — eles nunca devem ser commitados nem copiados por
   fora de um canal seguro.
2. Copie o serviço de exemplo e ajuste caminhos/usuário se necessário:
   ```bash
   sudo cp systemd/olx-monitor.service /etc/systemd/system/
   sudo useradd --system --no-create-home olxmonitor  # se ainda não existir
   sudo chown -R olxmonitor:olxmonitor /opt/olx-monitor
   sudo systemctl daemon-reload
   sudo systemctl enable --now olx-monitor
   ```
3. Acompanhar logs: `journalctl -u olx-monitor -f`

O serviço já vem com `Restart=always` — se o processo cair por qualquer
motivo, o systemd sobe de novo.

## Performance: browser persistente e circuit breaker

Duas otimizações de latência no estágio de coleta (`sources/olx.py`):

- **Browser Playwright persistente.** Antes, cada chamada em `modo:
  playwright` (ou cada fallback a partir de `modo: requests` bloqueado)
  lançava um Chromium do zero — 2-5s de startup por chamada, multiplicado
  pelo número de URLs de cada monitor. Agora `OlxSource` mantém um
  browser/context vivo entre ciclos; abrir uma `page` nova nesse context já
  existente é quase instantâneo. O browser só é relançado depois de crash
  (detectado automaticamente) ou após 50 páginas abertas, pra não deixar o
  processo do Chromium acumular memória indefinidamente. Cada `OlxSource` é
  exclusiva de uma thread (um monitor, ou o worker de enriquecimento) — a
  API síncrona do Playwright não é thread-safe entre threads diferentes, e
  é por isso que cada monitor tem seu próprio browser, não um pool
  compartilhado.
- **Circuit breaker por domínio.** Se o modo `requests` falha
  consistentemente (a Cloudflare bloqueando de forma persistente, por
  exemplo), não faz sentido pagar ~2-3s de timeout/bloqueio em toda
  tentativa antes de cair pro Playwright. Depois de 3 falhas consecutivas
  num domínio, `OlxSource` pula direto pro Playwright nas tentativas
  seguintes. O circuito reabre sozinho depois de 30 minutos, pra reavaliar
  se o `requests` voltou a funcionar (a proteção pode relaxar, ou o
  ambiente de deploy pode se comportar diferente do de desenvolvimento).

Um monitor com mais de uma URL busca todas em paralelo quando elas resolvem
via `modo: requests` (`OlxSource.collect_many`, usada automaticamente pelo
scheduler) — `requests` é thread-safe pra isso. URLs que caem no fallback
Playwright continuam sequenciais, na mesma thread do monitor: paralelizar
esse caminho exigiria abrir mão do browser persistente (ver acima).

## Dados do vendedor (enriquecimento assíncrono)

Depois que um anúncio novo passa nos filtros e é notificado, o monitor
**não espera** buscar dados do vendedor antes de avisar — isso adicionaria
segundos por anúncio bem na janela em que ele pode ser vendido pra outra
pessoa. Em vez disso:

1. A notificação sai imediatamente, com um `⏳ Buscando dados do
   vendedor...` no final.
2. Em background, um worker global (`enrichment.py` — uma fila + uma thread
   + um browser Playwright dedicado, compartilhados entre todos os
   monitores) busca a página individual do anúncio e extrai nome, tempo de
   conta, verificações (e-mail/telefone/identidade/Facebook) e avaliações
   (`seller_info.py`).
3. A mesma mensagem é **editada** (`editMessageText`) com os dados, ou com
   "👤 Dados do vendedor indisponíveis" se a busca falhar ou não achar nada.
   Isso nunca derruba nada nem propaga exceção — a notificação original já
   é válida e completa sem esse enriquecimento.

Esse worker é global (não um por monitor, não a thread do próprio monitor)
de propósito: o browser persistente de cada monitor só pode ser usado pela
thread que o criou, então enriquecer na mesma thread atrasaria o próximo
ciclo de coleta daquele monitor. Um worker único com seu próprio browser
resolve isso sem multiplicar o número de browsers abertos.

**A página individual de anúncio não usa RSC** — diferente da listagem,
confirmado inspecionando um `debug_seller.html` real: zero ocorrências de
`self.__next_f.push`. A página tem um `<script id="initial-data"
type="text/plain" data-json="...">` com JSON estruturado dos dados do
anúncio (preço, categoria, `user.name`), mas os dados que importam aqui —
"Na OLX desde", verificações, avaliações — **não estão nesse JSON**: eles só
aparecem no HTML depois que uma chamada do lado do cliente roda (o
Playwright já espera a página renderizar, então esse conteúdo chega pronto
no `page.content()`). Por isso `seller_info.py` extrai via regex sobre o
HTML renderizado, não sobre RSC/JSON.

Campos **confirmados** contra uma amostra real (`debug_seller.html`
inspecionado em 2026-07):

- **Nome** — `alt="Foto de <nome>"` na imagem do avatar.
- **Membro desde** — texto literal `Na OLX desde <mês> de <ano>`.
- **Verificações individuais** (E-mail/Telefone/Identidade/Facebook) — cada
  item é um ícone SVG com `fill="#24A148"` (verde, verificado) ou
  `fill="#8994A9"` (cinza, não verificado) seguido do rótulo em texto.
- **Ausência de avaliações** — texto literal `ainda não possui avaliações`.

Campos **não confirmados** (a amostra inspecionada não tinha exemplo
positivo — best-effort, documentado no código):

- **Conta verificada** (selo geral, distinto das verificações individuais)
  — não apareceu em lugar nenhum da amostra real. `seller_info.py` só
  reconhece o texto literal "conta verificada" se aparecer; do contrário
  fica `None` (não `False` — "não sabemos" é diferente de "confirmado que
  não").
- **Formato de quando HÁ avaliações** (estrelas/nota) — a amostra
  inspecionada não tinha nenhuma avaliação, só o caso "ausência". O padrão
  usado (número decimal perto da palavra "avalia") é um chute razoável, não
  uma estrutura vista de verdade.

Se a OLX mudar a estrutura de novo, o worker salva um `debug_seller.html`
automaticamente na primeira extração que não achar nada — inspecione esse
arquivo (`grep` pelos textos "Na OLX desde"/"Informações verificadas" é o
caminho mais rápido, como da primeira vez) e ajuste as regexes em
`seller_info.py`. Cada campo é extraído independentemente — um campo
faltando não invalida os outros nem impede a notificação de ser enriquecida
parcialmente.

## Testes

```bash
pytest
```

Cobrem a camada de filtro (`tests/test_filters.py`), o parser de
normalização/RSC da OLX (`tests/test_normalize_olx.py`), o circuit breaker
(`tests/test_circuit_breaker.py`), o dedupe (`tests/test_dedupe.py`), o
parser de dados do vendedor (`tests/test_seller_info.py`) e o formato das
mensagens do Telegram, incluindo o fluxo send/update
(`tests/test_telegram_message.py`) — as partes com lógica de negócio real.
Chamadas de rede/browser não são testadas.

## Aviso sobre a estrutura da página da OLX

**A OLX não usa mais `__NEXT_DATA__`.** Até meados de 2026 o site rodava no
Pages Router do Next.js, que embute um único bloco
`<script id="__NEXT_DATA__">` com todo o JSON da página — a suposição
original deste projeto. A OLX migrou para o **App Router com streaming
RSC** ("React Server Components"): o conteúdo vem espalhado em vários
`<script>self.__next_f.push([N,"..."])</script>` sem id fixo, cada um
carregando um pedaço da árvore serializada (às vezes várias entradas
`"id:valor"` por chunk, separadas por `\n`). Confirmado inspecionando uma
amostra real (busca de PS5) — ver `olx_monitor/sources/olx.py`.

A extração (`extract_ads_from_rsc`) decodifica cada `push(...)`, separa as
entradas, faz `json.loads` nas que são JSON de verdade (ignorando
referências internas do protocolo React que não são), e roda o **mesmo
scanner recursivo genérico de antes** sobre cada uma — ele não muda com o
formato de transporte, porque nunca fixou caminho nem nome de chave, só a
"cara" de um anúncio (id + título + preço). O suporte ao `__NEXT_DATA__`
antigo (`extract_ads_from_next_data`) foi mantido como fallback secundário,
caso a OLX sirva esse formato em algum contexto.

Duas ressalvas descobertas com dados reais, já corrigidas no código:

- A lista de anúncios vem com **placeholders de banner publicitário
  intercalados** (dicts só com `advertisingId`/`deviceType`, sem
  id/título/preço) a cada punhado de posições. O scanner exige que a
  **maioria** dos itens de uma lista pareça anúncio, não 100% — do
  contrário esses placeholders faziam a lista inteira ser descartada.
- Preços vêm como string brasileira (`"R$ 2.000"`, `"R$ 2.500,00"`) — ponto
  é separador de milhar, vírgula é decimal. `normalize.py` remove o ponto
  **sempre**, não só quando há vírgula, senão `"R$ 2.000"` virava `2.0` em
  vez de `2000.0`.

Os nomes de campo confirmados (case-insensitive) estão nas tuplas `_CHAVES_*`
em `olx_monitor/normalize.py`: `listId`, `subject`, `priceValue`/`price`,
`url` (já absoluta), `location` (string pronta, ex. `"Belém -  PA"`) e `date`
(timestamp Unix em segundos — convertido para ISO 8601 em
`_parse_data_publicacao`). Se a OLX mudar de novo e a extração vier vazia ou
com campos `None`, o caminho de depuração é salvar o HTML de uma página real
(ex.: `page.content()` de um script Playwright avulso) e ver que chaves o
objeto usa de verdade — compare com o que `_CHAVES_*` já espera.

Se o bloqueio por Cloudflare for frequente com `modo: requests`, mude o
monitor para `modo: playwright` no `monitores.yaml` — ele renderiza a página
com Chromium headless, contornando bloqueios que dependem de execução de JS.

## Não-objetivos (de propósito, ver SPEC.md)

Mercado Livre/Facebook Marketplace, canais além do Telegram, dashboard web,
compra automatizada e histórico de preços não foram implementados — as
interfaces (`Source`, `Notifier`) estão prontas para receber isso quando
fizer sentido, mas nada foi construído especulativamente.
