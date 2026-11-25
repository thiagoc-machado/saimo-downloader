# Saimo local

Aplicação local em Python para sincronizar o catálogo configurado para SQLite, pesquisar filmes e séries no navegador e organizar downloads em uma pasta definida no ambiente.

## Requisitos

- Python 3.10 ou mais recente
- `ffmpeg` instalado no sistema para juntar fontes HLS (`.m3u8`)
- Acesso ao catálogo configurado em `SAIMO_CATALOGO`

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; fi
python -m playwright install chromium
```

Edite `.env` para escolher a pasta de destino e, se necessário, ajustar os endereços do catálogo e do proxy. Os caminhos relativos são resolvidos a partir da pasta do projeto.

```dotenv
SAIMO_DOWNLOAD_DIR=./downloads
SAIMO_DB=./data/catalog.db
SAIMO_POSTERS=./static/posters
SAIMO_QUALITY=1080
SAIMO_PARALLEL=2
SAIMO_SEGMENTOS=8
SAIMO_TOKEN=
SAIMO_CATALOGO=https://filmes-saimo.pages.dev
SAIMO_PROXY=https://saimo-tv.pages.dev
SAIMO_TMDB_TOKEN=
SAIMO_TMDB_API_KEY=
SAIMO_EN_MAX_H=1080
SAIMO_OPENSUBTITLES_API_KEY=
```

Para baixar via fontes HLS, instale `ffmpeg` no sistema. Para testar a aba **EN**, configure `SAIMO_TMDB_TOKEN` ou `SAIMO_TMDB_API_KEY` no `.env`; a chave `SAIMO_OPENSUBTITLES_API_KEY` é opcional e habilita a busca de legendas PT-BR.

Sincronize o catálogo PT se quiser carregar essa aba também. A opção `--imagens` salva cópias locais das capas para que a grade não dependa das imagens remotas.

```bash
python sync_catalog.py --imagens
```

Inicie a aplicação:

```bash
mkdir -p data downloads static/posters
uvicorn app:app --host 127.0.0.1 --port 8787
```

Abra <http://127.0.0.1:8787>, selecione **EN** e clique em **Atualizar catálogo EN**. O progresso aparece na própria aba; depois, abra um título e escolha **Original (EN)** para criar um download. Encerre o servidor com `Ctrl+C`. Para acessar a partir de outros dispositivos na rede, use `--host 0.0.0.0` e configure `SAIMO_TOKEN` antes de iniciar o servidor.

## Executar com Docker Compose

Instale Docker Engine com o plugin Compose. Crie `.env` somente se ainda não existir e ajuste os IDs do usuário para que os arquivos gravados nas pastas montadas pertençam à sua conta:

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
id -u
id -g
```

Preencha `SAIMO_UID` e `SAIMO_GID` no `.env` com os valores mostrados acima. Para testar somente o catálogo EN, configure também `SAIMO_TMDB_TOKEN` ou `SAIMO_TMDB_API_KEY`. Depois, construa a imagem e inicie o servidor:

```bash
mkdir -p data downloads static/posters
docker compose build
docker compose up -d
```

Abra <http://127.0.0.1:8787>, selecione **EN** e clique em **Atualizar catálogo EN**. Não é necessário sincronizar o catálogo PT para testar essa aba. Para carregar também o catálogo PT antes de iniciar a aplicação, rode `docker compose run --rm saimo python sync_catalog.py --imagens` entre `build` e `up -d`.

A sincronização do catálogo PT é opcional para testar a aba EN. Para carregar ou atualizar o catálogo PT depois de iniciar o servidor, use o comando da seção “Para atualizar o catálogo depois”.

Se o log mostrar `sqlite3.OperationalError: unable to open database file`, confira se `data/`, `downloads/` e `static/posters/` pertencem ao mesmo UID/GID configurado no Compose. Para corrigir pastas existentes:

```bash
sudo chown -R "$(id -u):$(id -g)" data downloads static/posters
```

Depois recrie o container com `docker compose up -d --force-recreate`.

A interface fica em <http://127.0.0.1:8787>. Banco, capas e arquivos baixados ficam nas pastas `data/`, `static/posters/` e `downloads/` do projeto. O container inclui `ffmpeg`, necessário para fontes HLS.

Para salvar downloads em outra pasta do servidor, configure `SAIMO_DOWNLOADS_PATH` no `.env`. Por exemplo, `SAIMO_DOWNLOADS_PATH=/mnt/storage` monta essa pasta do host dentro do container em `/downloads`. Crie a pasta antes de iniciar e dê permissão de escrita ao usuário configurado por `SAIMO_UID` e `SAIMO_GID`. Filmes são organizados como `/downloads/movies/Título (Ano)/Título (Ano) [dub].mp4`; séries, como `/downloads/series/Série/Season 01/Série - S01E01 [dub].mp4`. A versão de áudio fica no nome do arquivo (`[dub]`, `[leg]` ou `[orig]`).

No Radarr e no Sonarr, mantenha as bibliotecas finais apontando para `/movies` e `/tv`, respectivamente. Os arquivos baixados pelo Saimo ficam na pasta compartilhada `/downloads`; importe-os pelo fluxo de importação manual do Radarr ou Sonarr para que cada programa os organize na biblioteca. O Saimo não se registra como cliente de download nesses programas.

O Compose publica a porta na rede local usando `8787:8787`. Defina `SAIMO_TOKEN` para proteger o acesso quando a porta estiver disponível na rede.

Para atualizar o catálogo depois:

```bash
docker compose exec saimo python sync_catalog.py --imagens
```

Se o catálogo não fornecer pôsteres associados, o sincronizador pode buscá-los no TMDb. Configure `SAIMO_TMDB_TOKEN` no `.env` com o API Read Access Token da sua conta TMDb e rode:

```bash
docker compose up -d --build
docker compose exec saimo python sync_catalog.py --imagens --tmdb-fallback
```

As fichas do catálogo também guardam o ID TMDb. Com `SAIMO_TMDB_TOKEN` ou `SAIMO_TMDB_API_KEY` configurado, ao abrir os detalhes de um título a aplicação busca e armazena em cache resumo, gêneros, pôster e elenco quando esses dados estiverem faltando.

Se o catálogo já estiver sincronizado, processe somente as capas para evitar refazer a sincronização completa:

```bash
docker compose exec saimo python sync_catalog.py --somente-capas --imagens --tmdb-fallback
```

Essa etapa mostra o progresso das buscas e downloads. Resultados sem pôster também ficam em cache; para refazer essas buscas após atualizar a credencial ou o catálogo, apague a tabela `tmdb_poster_cache` do SQLite.

Para repetir somente as buscas que já tinham sido feitas sem encontrar capa, acrescente `--refazer-cache-negativo` ao comando de capas.

O fallback só consulta títulos sem capa e grava as imagens em `static/posters/`. A busca usa o tipo (filme/série), o título e o ano quando disponível; títulos sem correspondência confiável ficam sem capa. A utilização da API e das imagens deve seguir os [termos do TMDb](https://www.themoviedb.org/api-terms-of-use).

Para acompanhar o serviço ou encerrá-lo:

```bash
docker compose logs -f saimo
docker compose down
```

## Uso

- A busca aceita título e filtro por filmes ou séries.
- A aba **EN** consulta e sincroniza separadamente títulos com áudio original em inglês. Configure `SAIMO_TMDB_TOKEN` ou `SAIMO_TMDB_API_KEY`; os detalhes TMDb ficam em cache por 24 horas. Para busca de legendas PT-BR, configure `SAIMO_OPENSUBTITLES_API_KEY`; sem ela, o vídeo baixa normalmente e o job indica que a legenda não foi consultada. A imagem Docker também instala yt-dlp e Chromium para os resolvedores EN.
- Clique em um título para ver pôster, gêneros, resumo, elenco e versões/fontes disponíveis. Para séries, a janela lista temporadas e episódios com fontes, e permite escolher a temporada e episódios (`primeiro`, `todos`, `1,3,5` ou `1-3`).
- A aba **Downloads** mostra o percentual, a velocidade e a estimativa de término dos downloads, além de um resumo de velocidades e estados da fila. Arquivos concluídos aparecem separados entre filmes e séries.
- Na fila, use **Pausar**, **Retomar** e **Cancelar e apagar dados** para controlar cada job.
- **Limpar completos** oculta da fila os jobs concluídos sem apagar os vídeos nem a marca de download no catálogo. **Limpar lista** pede confirmação, oculta o histórico e remove jobs em fila ou pausados; downloads ativos e arquivos completos são preservados.
- A pasta de destino é carregada ao iniciar o servidor. Reinicie o processo para aplicar uma mudança no `.env`.
- A API de documentação fica em <http://127.0.0.1:8787/docs>.

## Notas

- A primeira sincronização pode levar alguns minutos. Execute novamente para atualizar o catálogo.
- `SAIMO_PARALLEL` controla quantos arquivos são baixados ao mesmo tempo; `SAIMO_SEGMENTOS` controla os segmentos de cada fonte HLS.
- Jobs enfileirados e pausados são persistidos no SQLite e retomados quando o servidor reinicia. Pausas preservam partes concluídas; downloads HTTP retomam do ponto salvo quando a fonte aceita requisições Range, e HLS reutiliza segmentos já baixados.
- A busca usa o tokenizer trigram do FTS5 quando a versão de SQLite o oferece; em versões sem esse recurso, usa busca SQLite convencional.
- Fontes DASH (`.mpd`) e playlists HLS criptografadas ou com mapas/faixas de bytes não são suportadas; a fila informa esse motivo no erro do job.
- `SAIMO_TOKEN` protege a API e os arquivos servidos por `/media`. Deixe o servidor acessível apenas a pessoas de confiança.
- O serviço lê as listas e fontes do catálogo configurado. Use fontes e baixe arquivos apenas quando tiver autorização para isso.
