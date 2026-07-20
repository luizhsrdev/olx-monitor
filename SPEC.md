# Monitor de anúncios de marketplace com alertas em tempo real

Quero que você construa uma aplicação Python de monitoramento de anúncios da OLX
que me notifique no Telegram assim que aparecer um anúncio novo que atenda aos
meus critérios.

## Contexto do problema

Compro produtos em marketplaces para revenda. O gargalo não é encontrar anúncios
— é a **latência de detecção**. Os melhores anúncios (bem precificados, produto
em boas condições) são vendidos em minutos ou segundos. Buscas salvas nativas da
OLX e notificações por e-mail chegam tarde demais, porque o índice da plataforma
tem atraso de atualização.

Já tenho URLs de busca da OLX com filtros aplicados (faixa de preço, categoria,
"aceita entrega", ordenado por mais recentes). O que falta é algo que consulte
essas URLs continuamente e me empurre um push instantâneo.

O caso de uso atual é PlayStation 5, mas a aplicação **precisa ser reutilizável
para qualquer produto** — Nintendo Switch 2, notebooks Apple, etc. — sem tocar em
código. Trocar de produto deve ser trocar de configuração.

## Princípio de design central (não negociável)

**O código não pode saber que PS5 existe.** Todo conhecimento de produto
(palavras-chave, faixas de preço, termos de exclusão) vive em arquivo de
configuração externo. O código só sabe executar o pipeline.

Se eu precisar editar um `.py` para começar a monitorar um MacBook, o design
falhou.

## Arquitetura

Pipeline de cinco estágios, cada um em módulo separado:

```
BUSCA (config) → COLETA → NORMALIZAÇÃO → FILTRO → DEDUPE → ALERTA
                 [fonte]    [formato]     [regras]  [SQLite]  [canal]
```

- **Coleta** — baixa a página de busca. Implementar como *adaptador de fonte*,
  com uma interface clara, para que adicionar outra plataforma no futuro seja
  criar um arquivo novo e não uma cirurgia. Por ora, implemente **apenas** o
  adaptador da OLX.
- **Normalização** — é o coração do design. Toda fonte converte para um formato
  interno único: `id, titulo, preco, url, local, fonte, publicado_em, coletado_em`.
  É isso que permite adicionar novas fontes sem tocar em filtro, dedupe ou alerta.
- **Filtro** — aplica as regras vindas da config. Zero regras hardcoded.
- **Dedupe** — SQLite. Precisa lembrar quais anúncios já foram vistos entre
  reinicializações do processo.
- **Alerta** — Telegram por enquanto, mas isolado atrás de uma interface para que
  outros canais (Discord, e-mail, webhook) sejam plugáveis depois.

## Extração de dados da OLX

A OLX é um site Next.js. O HTML da página de busca contém um bloco
`<script id="__NEXT_DATA__" type="application/json">` com um JSON que traz todos
os anúncios da página (id, título, preço, URL, localização). **Extraia daí** — é
muito mais estável do que raspar HTML, que quebra a cada mudança de layout.

Duas exigências sobre o parser:

1. **Não fixe um caminho no JSON** (tipo `props.pageProps.ads`). A estrutura muda
   periodicamente. Varra o JSON recursivamente procurando qualquer lista cujos
   itens tenham cara de anúncio (têm id + título + preço). Assim o parser
   sobrevive a mudanças de esquema.
2. **Plano B para bloqueio.** Se o `__NEXT_DATA__` vier vazio, ou vier 403 /
   página de desafio anti-bot, o adaptador deve conseguir cair para uma
   implementação com Playwright (`chromium` headless) que renderiza a página e
   extrai o mesmo bloco. Deixe isso como estratégia selecionável na config
   (`modo: requests | playwright`), com `requests` como padrão por ser muito mais
   leve.

## Configuração

Arquivo `monitores.yaml` externo ao código. Segredos por variável de ambiente,
nunca commitados. Formato pretendido:

```yaml
telegram:
  token: ${TELEGRAM_TOKEN}
  chat_id: ${TELEGRAM_CHAT_ID}

padroes:
  intervalo_segundos: 60
  jitter_segundos: 20
  bloqueadas_globais: ["defeito", "peças", "sucata", "não liga", "bloqueado"]

monitores:
  - nome: "PS5 revenda"
    ativo: true
    fonte: olx
    intervalo_segundos: 60          # sobrescreve o padrão
    urls:
      - "https://www.olx.com.br/games/consoles-de-video-game?ps=2000&pe=2500&q=playstation%205&sf=1&opst=2"
      - "https://www.olx.com.br/games/consoles-de-video-game?ps=2500&pe=2800&q=playstation%205&sf=1&opst=2"
    preco_max: 2800
    bloqueadas: ["controle", "capa", "somente", "apenas o"]
    obrigatorias_ou: ["ps5", "playstation 5"]
    prioritarias: ["lacrado", "novo", "1tb", "nunca usado"]

  - nome: "Switch 2"
    ativo: true
    fonte: olx
    intervalo_segundos: 120
    urls: ["..."]
    preco_max: 3200
    bloqueadas: ["joy-con", "case", "película", "jogo"]
    obrigatorias_ou: ["switch 2"]
    prioritarias: ["lacrado", "novo"]

  - nome: "MacBook Air M2"
    ativo: false
    fonte: olx
    intervalo_segundos: 600
    urls: ["..."]
    preco_max: 5000
    bloqueadas: ["tela quebrada", "para retirada", "bateria viciada", "capa"]
    obrigatorias_ou: ["macbook"]
    prioritarias: ["m2", "m3", "lacrado", "ciclos"]
```

Semântica dos campos de filtro:

- `bloqueadas` / `bloqueadas_globais` — se o **título** contiver qualquer um
  destes termos, descarta. As globais valem para todos os monitores; as locais
  somam-se a elas.
- `obrigatorias_ou` — o título precisa conter **pelo menos um** destes termos.
  Este é o filtro mais importante: buscas na OLX retornam muito item correlato
  ("suporte para PS5", "jogo de PS5"), e lista de bloqueio nunca cobre tudo.
  Exigir presença do produto é bem mais robusto do que enumerar o lixo.
- `prioritarias` — não filtra; apenas marca o alerta como alta prioridade
  (emoji/destaque diferente na mensagem do Telegram).
- Comparação de texto sempre case-insensitive e **insensível a acento** (o mesmo
  anúncio aparece como "peças" e "pecas").
- `ativo: false` desliga o monitor sem apagar a configuração.

## Comportamento em runtime

- **Intervalos independentes por monitor.** PS5 a cada 60s, MacBook a cada 10min.
  Um monitor lento não pode atrasar um rápido. Use `asyncio` ou uma thread por
  monitor — escolha o que deixar o código mais simples e justifique a escolha.
- **Jitter aleatório** no intervalo e pausa entre requisições consecutivas, para
  não gerar tráfego com padrão robótico.
- **Nunca abaixo de 30s** por URL. Perder acesso por rate limit custa muito mais
  do que os segundos economizados. Valide isso ao carregar a config e recuse
  valores menores com mensagem clara.
- **Primeira execução não notifica.** Se o banco estiver vazio para um monitor,
  a primeira rodada só popula a base — senão eu levo 50 notificações de uma vez.
  Idem quando eu adicionar um monitor novo a um banco já existente: trate a
  base inicial *por monitor*, não globalmente.
- **Falha isolada.** Erro de rede, HTML inesperado ou falha do Telegram em um
  monitor não pode derrubar o processo nem os outros monitores. Log do erro,
  backoff exponencial nas falhas repetidas de uma mesma fonte, e segue.
- **Logging útil** — timestamp, monitor, quantos anúncios vieram, quantos foram
  filtrados e por qual regra, quantos notificados. Preciso conseguir depurar
  filtro bom demais ou frouxo demais só lendo o log.

## Mensagem do Telegram

Deve conter: marcador de prioridade, título, preço formatado em BRL,
localização, nome do monitor que disparou e o link do anúncio (com preview
ativado). O link precisa ser clicável e me levar direto ao anúncio — cada
segundo conta.

## Entregáveis

- Código organizado em módulos, refletindo os estágios do pipeline.
- `monitores.yaml.example` com os três monitores acima como referência.
- `requirements.txt` (ou `pyproject.toml`).
- `.env.example` e `.gitignore` cobrindo `.env`, o `.db` e o `monitores.yaml` real.
- `README.md` com: instalação, como criar o bot no Telegram e descobrir o
  `chat_id`, como montar uma URL de busca da OLX com filtros, e como adicionar
  um produto novo.
- Arquivo de serviço `systemd` com `Restart=always`, para deploy 24/7 em VPS
  ou Raspberry Pi.
- Testes para a camada de **filtro** e para o **parser de normalização** — são as
  duas partes com lógica real e as que mais vão quebrar. Não precisa testar as
  chamadas de rede.

## Não-objetivos (não construa agora)

Estas coisas podem parecer naturais de já incluir. **Não inclua.** Elas devem ser
desenhadas quando a dor existir e com informação que eu ainda não tenho:

- Adaptadores para Mercado Livre, Facebook Marketplace ou qualquer outra fonte.
  Só deixe a *interface* pronta para recebê-los.
- Canais de alerta além do Telegram. Idem: interface pronta, implementação não.
- Interface web, dashboard, banco de dados que não seja SQLite.
- Compra ou negociação automatizada. A aplicação só notifica; eu decido e ajo.
- Histórico de preços, análise de tendência, sugestão de preço.

## Como quero que você trabalhe

Antes de escrever código, me mostre a estrutura de arquivos e módulos que você
pretende criar e as assinaturas das interfaces de fonte e de canal de alerta.
Quero validar as fronteiras antes da implementação. Depois disso, pode
implementar tudo de uma vez.

Se em algum ponto a estrutura real do `__NEXT_DATA__` da OLX contrariar o que
descrevi aqui, siga o que você encontrar de fato e me avise da divergência — a
descrição acima é a minha melhor informação, não uma certeza.
