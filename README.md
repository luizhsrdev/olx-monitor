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
| Coleta | `olx_monitor/sources/olx.py` | Baixa a busca da OLX e devolve anúncios crus |
| Normalização | `olx_monitor/normalize.py` | Converte o formato cru de cada fonte em `Anuncio` |
| Filtro | `olx_monitor/filters.py` | Aplica `bloqueadas` / `obrigatorias_ou` / `preco_max` / `prioritarias` |
| Dedupe | `olx_monitor/dedupe.py` | SQLite — nunca notifica o mesmo anúncio duas vezes |
| Alerta | `olx_monitor/alerts/telegram.py` | Envia a mensagem no Telegram |
| Orquestração | `olx_monitor/scheduler.py` | Uma thread por monitor, com seu próprio intervalo |

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
  alta (🔥 no Telegram).
- Toda comparação de texto é case-insensitive e insensível a acento
  ("peças" e "pecas" são tratados como iguais).
- `ativo: false` desliga o monitor sem apagar a configuração.
- `intervalo_segundos` nunca pode ser menor que **30s** — a config recusa
  carregar com uma mensagem clara se você tentar. Isso existe para não levar
  rate limit/bloqueio da OLX.

Na primeira rodada de um monitor novo (banco ainda sem registro para aquele
`nome`), o monitor só popula o SQLite e não notifica nada — assim você não
leva uma enxurrada de alertas de anúncios antigos.

## Rodando

```bash
python run.py
# ou, explicitando os caminhos:
python run.py --config monitores.yaml --db olx_monitor.db --log-level INFO
```

`Ctrl+C` encerra todos os monitores de forma limpa.

## Deploy 24/7 (systemd)

1. Copie o projeto para o servidor, ex. `/opt/olx-monitor`, com `.venv`,
   `.env` e `monitores.yaml` já configurados lá.
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

## Testes

```bash
pytest
```

Cobrem a camada de filtro (`tests/test_filters.py`) e o parser de
normalização da OLX (`tests/test_normalize_olx.py`) — são as duas partes com
lógica de negócio real. Chamadas de rede não são testadas.

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
com campos `None`, o caminho de depuração é sempre o mesmo: gere um
`debug_page.html` (veja abaixo), ache o id de um anúncio visível na tela e
veja que chaves o objeto usa de verdade.

Se o bloqueio por Cloudflare for frequente com `modo: requests`, mude o
monitor para `modo: playwright` no `monitores.yaml` — ele renderiza a página
com Chromium headless, contornando bloqueios que dependem de execução de JS.

Se for preciso inspecionar manualmente o HTML renderizado (para diagnosticar
bloqueio ou uma futura mudança de formato), dá pra rodar com o Chromium
visível e salvar o HTML completo em `debug_page.html`:

```bash
OLX_MONITOR_PLAYWRIGHT_HEADLESS=false python run.py
```

Isso é um toggle de depuração temporário (variável de ambiente, não existe
em `monitores.yaml` de propósito) — requer um ambiente com display (não
funciona numa VPS/Raspberry Pi sem X11/VNC) e não deve ser usado em deploy
24/7.

## Não-objetivos (de propósito, ver SPEC.md)

Mercado Livre/Facebook Marketplace, canais além do Telegram, dashboard web,
compra automatizada e histórico de preços não foram implementados — as
interfaces (`Source`, `Notifier`) estão prontas para receber isso quando
fizer sentido, mas nada foi construído especulativamente.
