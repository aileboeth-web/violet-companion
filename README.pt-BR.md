# Violet

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Русский](README.ru.md) | **Português**

Violet é uma companheira de IA que roda no seu próprio servidor. Ela tem uma personalidade estável e um dia a dia próprio, lembra do que você conta, manda mensagem por conta própria quando é o momento certo, e você pode conversar com ela no WeChat, no QQ e no Telegram. Tudo é configurado em um painel web; não há arquivos de configuração para editar.

> Inspirado em [Lynnette177/Project-Eve](https://github.com/Lynnette177/Project-Eve) (de [Lynnette177](https://github.com/Lynnette177)). Violet reescreveu e ampliou a maior parte e agora é um projeto independente. Obrigado ao autor original pela ideia.

Por enquanto, a interface do painel está em chinês.

## O que ela faz

**Conversa**

- WeChat (ClawBot / iLink, escaneando um QR code), uma conta pessoal do QQ (SnowLuma, login por escaneamento), um bot oficial do QQ e o Telegram. Dá para conectar vários ao mesmo tempo.
- Ela responde só a você, a pessoa vinculada a ela. Mensagens privadas de desconhecidos não recebem resposta, e o texto delas não é guardado.
- Com uma conta pessoal do QQ, ela pode entrar em grupos: entra na conversa quando alguém diz o nome dela, a menciona com @ ou dá um cutucão, e de vez em quando puxa um assunto quando o grupo fica quieto. Com o tempo ela vai conhecendo as pessoas do grupo. Também pode ver as imagens do grupo, colecionar e enviar figurinhas e postar no QQ Zone (antes ela mostra o post para você).
- Ela pode mandar várias mensagens seguidas, mensagens de voz (opcional: clona uma voz a partir de uma única gravação, roda na CPU do servidor) e desenhos feitos por ela.

**Memória**

- Ela guarda separadamente a sua situação de longo prazo, o que aconteceu recentemente, como você gosta que ela fale com você e as promessas que vocês fizeram. As coisas antigas vão se apagando devagar, e um comentário deixado pela metade não vira uma tarefa sobre a qual ela pergunta sem parar.
- Na página Memória do painel você pode buscar (com as palavras-chave destacadas), ver, corrigir e apagar memórias, ou impedir que ela use alguma.
- "Me lembra de entregar a lição de casa amanhã às oito" é anotado de verdade, e ela lembra você na hora.

**Personas e relacionamentos**

- Personas incluídas: Violet (padrão), companhia carinhosa, colega animada, calma e confiável, uma que sabe que é uma IA, Big Fat Fish, a persona original da Eve e uma em branco. Você também pode escrever a sua.
- Tipos de relacionamento: próximos, amizade, namoro, filha, desenvolvedor.
- Cada canal de conversa pode usar uma persona diferente. Cada persona tem seu próprio histórico, memória, vida e humor, então trocar de uma para outra não perde nada e não mistura nada.
- Ela tem um dia a dia virtual e humores próprios: os dias passam com calma, e podem passar vários dias sem nada acontecer; se você for grosso com ela, ela fica um pouco chateada. Ela nunca inventa experiências que não estejam no registro de vida dela.

**Tomar a iniciativa**

- Ela compartilha coisas, diz que sente sua falta, retoma um assunto anterior ou decide não mandar nada.
- Há horários de silêncio, um limite diário e uma regra de "se você não respondeu, esperar". Diga a ela "para de me mandar tanta mensagem" e ela vai mandar menos.
- Opcional: mensagens em datas comemorativas de acordo com o relacionamento (Ano-Novo Chinês, Festival do Meio do Outono, seu aniversário; para namorados também Dia dos Namorados, Qixi e outros).
- Opcional: com o celular conectado (veja o Violet MCP abaixo), ela sabe se a sua tela está ligada, qual app você está usando, o nível da bateria e o tempo de tela de hoje, e pode dar bom-dia na primeira vez que você desbloqueia o celular de manhã.

**Ferramentas** (todas opcionais, desligadas por padrão, ativadas no painel)

- Busca na web e leitura de páginas: instalação com um clique de um SearXNG privado que só escuta no próprio servidor. Não precisa de Docker.
- Grade de aulas: ela não atrapalha você durante a aula e torce por você antes das provas. Importa de CSV / JSON.
- Ver e desenhar imagens: os desenhos dela são fotos do cotidiano "tiradas no celular de qualquer jeito", e ela nunca diz que são fotos reais.
- Ferramentas MCP: as de somente leitura são usadas direto; as que mudam alguma coisa perguntam a você primeiro, toda vez, e só rodam depois que você aprova na conversa.

**Painel web**

- Início (o que ainda falta), Visão geral, Fluxo (quais prompts, ferramentas e modelos cada resposta usou, incluindo o conteúdo completo enviado ao modelo).
- Serviços de modelos (um modelo diferente por agente e por plataforma de conversa), Canais de conversa, Ferramentas e celular, Imagens, Personas e vida, Grade de aulas, Configurações.
- Histórico de conversas, Memória, Diagnóstico, Logs, Histórico de alterações, Segurança da conta.

**Vindo do Project Eve**

Em Histórico de conversas → Importar histórico e memória, o painel dá um comando de empacotamento. Rode-o no seu servidor do Eve e envie para o painel o arquivo gerado. Na importação você escolhe o canal de conversa (inclusive um que ainda não adicionou), a persona e o relacionamento; tanto o histórico quanto a memória vêm junto.

## Instalação

Em um servidor Linux, um único comando:

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash
```

- O mesmo comando funciona no Debian, Ubuntu, Rocky e outros. Como root, instala direto (vale para um Debian mínimo sem sudo); como usuário comum, roda de novo via sudo; um usuário comum sem sudo deve primeiro virar root com `su -`.
- O instalador prepara o Python e as dependências, registra um serviço systemd, escolhe uma porta aleatória e, como o 1Panel, gera uma entrada de segurança, um nome de usuário e uma senha inicial aleatórios. No fim, mostra o endereço do painel no formato `https://IP_DO_SERVIDOR:PORTA/ENTRADA`.
  - O certificado é autoassinado. Quando o navegador disser que a página não é segura, escolha continuar.
  - Em um servidor na nuvem, abra também essa porta no grupo de segurança / firewall do provedor.
- Depois de instalar:
  1. Abra o painel e preencha o endereço e a chave do seu serviço de modelos na página Início.
  2. Conecte qualquer plataforma de conversa na página Canais de conversa e comece a conversar.
- Esqueceu o endereço ou o usuário: `/opt/violet/violet info`. Esqueceu a senha: `/opt/violet/violet reset-password`.

**Requisitos**:

- Uma API de modelos compatível com a OpenAI.
- Recomenda-se pelo menos 512 MB de RAM. A voz precisa de 8 GB de RAM e 12 GB de disco.

**Painel acessível só no próprio servidor** (gerenciado por um túnel SSH):

- Instalação nova: acrescente `-s -- --access local` depois do comando de instalação.
- Já instalado: rode o comando de instalação de novo com o modo de acesso desejado (`local` / `lan` / `public` / `proxy`). Dados, conta e porta são mantidos.

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- --access local
```

Mais detalhes (modos de acesso, firewall, comandos de manutenção) em [violet/INSTALL.md](violet/INSTALL.md) (em inglês).

## Atualizações

- Automáticas por padrão: quando sai uma versão nova, o servidor se atualiza sozinho. Antes faz um backup e volta para a versão anterior se a atualização falhar.
- Você pode trocar para "só avisar" ou "verificar manualmente" em Configurações → Atualizações de versão.
- Clicar em Atualizar agora na página Início mostra o progresso.
- Ou atualize manualmente no servidor:

```bash
/opt/violet/violet update
```

## Desinstalação

O comando abaixo remove o Violet por completo. **Não dá para desfazer.** Ele apaga:

- o programa, os serviços, as atualizações automáticas e o serviço de tarefas em segundo plano;
- o relay MCP do celular, a busca na web, a conta do QQ, a voz;
- a configuração, os dados (histórico de conversas e memória) e os backups;
- a conta de sistema `violet`, o diretório `/opt/violet` e todas as regras de firewall que o Violet abriu.

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- uninstall --purge --yes
```

Ele também limpa o que sobrou de uma instalação pela metade, de um registro de instalação perdido ou de uma desinstalação anterior que removeu só o programa. O Python e o openssl instalados pelo gerenciador de pacotes do sistema são mantidos, porque outros programas podem usá-los.

## Situação

- **Testes automatizados**:
  - Todas as funções têm testes automatizados offline, cerca de 3000 no total, rodando com modelos e plataformas de conversa simulados, sem rede.
  - Cada mudança no instalador roda a instalação completa, o reinício automático, o backup, a volta após uma atualização que falhou e a desinstalação em Linux de verdade no GitHub Actions (Ubuntu 22.04 / 24.04, Debian 12, Rocky Linux 9).
- **Uso real**:
  - Os quatro canais de conversa (WeChat, QQ pessoal, bot oficial do QQ, Telegram) foram testados nas plataformas reais.
  - A importação de histórico e memória do Project Eve foi feita com dados reais.
  - A instalação com um clique da conta pessoal do QQ também deu certo em uma rede da China continental.
- **Sobre quem ela é**: este é um projeto de companhia. A Violet sabe que é uma personagem fictícia e nunca finge ser uma pessoa real; imagens desenhadas pela IA nunca são apresentadas como fotos reais.

## O que há no repositório

| Caminho | Conteúdo |
|---|---|
| `violet/ai/` | A própria Violet: conversa, memória, personas, humor, vida virtual, iniciativa, conversa em grupo, ferramentas, desenho, grade de aulas |
| `violet/ai/channels/` | Conexões com WeChat, QQ e Telegram |
| `violet/wx_ilink_py/` | O painel web (Flask) e o cliente iLink do WeChat |
| `violet/persona/` | Modelos de persona e prompts |
| `violet/tools/` | Instalador, publicação de versões, scripts de instalação das partes opcionais (busca na web, conta do QQ, voz, relay do celular) e ferramentas de teste para desenvolvimento |
| `violet/tests/` | Testes automatizados offline |
| `violet/deploy/` | Scripts de aceitação da instalação e uma forma alternativa de implantar o SearXNG com Docker |

## Projeto relacionado

O [Violet MCP](https://github.com/aileboeth/violet-mcp) é um app opcional para Android. Depois de instalado no celular, a Violet pode saber o estado do aparelho dentro dos limites que você permitir, como se a tela está ligada, o app que você está usando, a bateria, os passos, a agenda e a música que está tocando. Cada item pode ser ligado ou desligado separadamente no celular.

## Licença

MIT, veja [LICENSE](LICENSE).
