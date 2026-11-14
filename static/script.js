/* =========================================================================
   Chatbot MEI — página do chat
   ========================================================================= */
const areaMensagens = document.getElementById("mensagens");
const formulario = document.getElementById("formulario");
const campo = document.getElementById("pergunta");
const botao = document.getElementById("enviar");
const botaoNova = document.getElementById("nova-conversa");
const statusEl = document.getElementById("status");
const modeloBoasVindas = document.getElementById("modelo-boas-vindas");

let pronto = false;
let enviando = false;
// Conversa atual: [{papel: "user"|"assistant", texto, fontes, acoes, sugestoes}]
// Fica salva no navegador, então sobrevive a recarregar a página.
let mensagens = [];
const MAX_HISTORICO = 6;          // mensagens enviadas ao servidor como contexto
const MAX_SALVAS = 40;            // mensagens guardadas no navegador
const CHAVE_ARMAZENAMENTO = "chatbot-mei-conversa-v1";
// Em celular/tablet o Enter quebra linha; o envio é pelo botão.
const telaDeToque = window.matchMedia("(pointer: coarse)").matches;

/* =========================================================================
   Carregamento com barra de progresso
   ========================================================================= */
const tela = document.getElementById("tela-carregando");
const tituloCarregando = document.getElementById("carregando-titulo");
const msgCarregando = document.getElementById("carregando-msg");
const etapaAtualEl = document.getElementById("etapa-atual");
const pctEl = document.getElementById("progresso-pct");
const barra = document.getElementById("barra");
const barraPreenchida = document.getElementById("barra-preenchida");
const itensEtapas = [...document.querySelectorAll("#etapas li")];
const inicioCarregamento = Date.now();

// Etapas informadas pelo servidor (/api/status -> "etapa") => [regex, %, índice da lista]
const MAPA_ETAPAS = [
  [/cliente groq/, 92, 4],
  [/carregando indice|montando busca/, 80, 3],
  [/criando indice/, 70, 3],
  [/indice no disco/, 66, 3],
  [/modelo de embeddings/, 45, 2],
  [/importando groq/, 36, 1],
  [/rag importado/, 30, 1],
  [/importando numpy/, 20, 1],
  [/executando rag|rag\.py/, 14, 1],
  [/iniciando thread|aguardando/, 8, 0],
];

let progresso = 0;     // valor exibido
let alvo = 2;          // último marco real informado pelo servidor
let teto = 10;         // até onde a barra pode "andar sozinha" enquanto espera o próximo marco
let etapaIdx = 0;
let falhou = false;
let animacao = null;

function desenharProgresso() {
  const pct = Math.round(progresso);
  barraPreenchida.style.width = pct + "%";
  pctEl.textContent = pct + "%";
  barra.setAttribute("aria-valuenow", String(pct));
  itensEtapas.forEach((li, i) => {
    li.classList.toggle("feita", i < etapaIdx);
    li.classList.toggle("ativa", i === etapaIdx && !falhou);
  });
  if (itensEtapas[etapaIdx]) etapaAtualEl.textContent = itensEtapas[etapaIdx].textContent;
}

function animarProgresso() {
  // Sobe rápido até o marco real e depois avança devagar (sem nunca voltar).
  if (progresso < alvo) progresso += Math.max((alvo - progresso) * 0.25, 0.4);
  else if (progresso < teto) progresso += (teto - progresso) * 0.025;
  progresso = Math.min(progresso, 100);
  desenharProgresso();
}

function definirMarco(pct, idx) {
  alvo = Math.max(alvo, pct);
  teto = Math.min(Math.max(teto, pct + 12), 97);
  etapaIdx = Math.max(etapaIdx, idx);
}

function atualizarMensagemTempo() {
  const s = (Date.now() - inicioCarregamento) / 1000;
  if (s > 240) {
    tela.classList.add("falhou");
    tituloCarregando.textContent = "Demorando mais que o normal";
    msgCarregando.textContent = "O servidor ainda não terminou de iniciar. Aguarde mais um pouco ou tente novamente.";
  } else if (s > 40) {
    msgCarregando.textContent = "Quase lá… o servidor gratuito demora um pouco para acordar.";
  } else if (s > 12) {
    msgCarregando.textContent = "Carregando os documentos sobre MEI…";
  }
}

function concluirCarregamento() {
  definirMarco(100, itensEtapas.length);
  teto = 100;
  const finalizar = () => {
    if (progresso < 99.5) { progresso += (100 - progresso) * 0.35 + 1; desenharProgresso(); return; }
    progresso = 100; desenharProgresso();
    clearInterval(animacao);
    setTimeout(() => {
      tela.classList.add("oculta");
      setTimeout(() => tela.remove(), 500);
      if (!telaDeToque) campo.focus();
    }, 350);
  };
  clearInterval(animacao);
  animacao = setInterval(finalizar, 40);
}

function mostrarFalha() {
  falhou = true;
  tela.classList.add("falhou");
  tituloCarregando.textContent = "Não foi possível iniciar";
  msgCarregando.textContent = "O chatbot está indisponível no momento. Tente novamente em alguns instantes.";
  clearInterval(animacao);
  desenharProgresso();
}

async function verificarStatus() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    const d = await r.json();
    if (d.erro) {
      definirStatus("Indisponível", "erro");
      mostrarFalha();
      return;
    }
    if (d.pronto) {
      pronto = true;
      definirStatus("Online", "pronto");
      atualizarBotoes();
      concluirCarregamento();
      return;
    }
    const etapa = String(d.etapa || "").toLowerCase();
    const regra = MAPA_ETAPAS.find(([re]) => re.test(etapa));
    if (regra) definirMarco(regra[1], regra[2]);
    else definirMarco(8, 0);
    if (/criando indice/.test(etapa)) {
      msgCarregando.textContent = "Processando os PDFs pela primeira vez. Isso pode levar alguns minutos.";
    } else {
      atualizarMensagemTempo();
    }
  } catch (_) {
    // Servidor ainda acordando (ex.: plano gratuito do Render)
    etapaAtualEl.textContent = "Acordando o servidor";
    atualizarMensagemTempo();
  }
  definirStatus("Preparando…", "");
  setTimeout(verificarStatus, 1500);
}

/* =========================================================================
   Utilidades
   ========================================================================= */
function definirStatus(texto, classe) {
  statusEl.textContent = texto;
  statusEl.className = ("pilula-status " + classe).trim();
}

function escapar(texto) {
  return String(texto).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Markdown simples e seguro: títulos, negrito, itálico, listas e parágrafos
function markdownSimples(texto) {
  const linhas = escapar(texto).split("\n");
  let html = "", lista = null;
  const inline = (t) => t
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\s)\*(?!\s)(.+?)\*(?=\s|$|[.,;:!?])/g, "$1<em>$2</em>");
  const fechar = () => { if (lista) { html += `</${lista}>`; lista = null; } };

  for (const linha of linhas) {
    const titulo = linha.match(/^\s*#{1,6}\s+(.*)/);
    if (titulo) { fechar(); html += `<p><strong>${inline(titulo[1])}</strong></p>`; continue; }
    const ul = linha.match(/^\s*[-*•]\s+(.*)/);
    const ol = linha.match(/^\s*\d+[.)]\s+(.*)/);
    if (ul || ol) {
      const tipo = ul ? "ul" : "ol";
      if (lista !== tipo) { fechar(); html += `<${tipo}>`; lista = tipo; }
      html += `<li>${inline((ul || ol)[1])}</li>`;
    } else if (linha.trim() === "") {
      fechar();
    } else {
      fechar();
      html += `<p>${inline(linha)}</p>`;
    }
  }
  fechar();
  return html;
}

// Remove marcadores de citação [1], [2, 3], 【4†...】 (inclusive um incompleto no fim,
// enquanto a resposta ainda está chegando).
function limparCitacoes(texto) {
  return texto
    .replace(/\s?[\[【]\s*\d{1,2}(?:\s*[,;–-]\s*\d{1,2})*\s*(?:†[^\]】]*)?[\]】]/g, "")
    .replace(/\s?[\[【][\d\s,;–†-]*$/, "");
}

// "3-Perguntas&Respostas2_MEI.pdf" -> "Perguntas & Respostas 2 MEI"
function nomeDocumento(arquivo) {
  return String(arquivo)
    .replace(/^\d+-/, "")
    .replace(/(\.pdf)+$/i, "")
    .replace(/&/g, " & ")
    .replace(/_/g, " ")
    .replace(/([a-zà-ú])(\d)/gi, "$1 $2")
    .replace(/\s+/g, " ")
    .trim();
}

const ICONE_DOC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V9z"/><path d="M14 3v6h6"/></svg>';
const ICONE_EXTERNO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/></svg>';
const ICONE_COPIAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 01-1-1V4a1 1 0 011-1h10a1 1 0 011 1v1"/></svg>';
const ICONE_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5L20 7"/></svg>';
const MAX_FONTES = 6;

// Chips das fontes: cada página abre o PDF exatamente nela.
function htmlFontes(fontes) {
  if (!fontes || !fontes.length) return "";
  const chips = [];
  for (const f of fontes) {
    if (typeof f === "string") {           // formato antigo salvo no navegador
      chips.push(`<span class="fonte">${ICONE_DOC}${escapar(f)}</span>`);
      continue;
    }
    const url = "/documentos/" + encodeURIComponent(f.arquivo);
    for (const p of f.paginas || []) {
      chips.push(
        `<a class="fonte" href="${url}#page=${Number(p)}" target="_blank" rel="noopener" ` +
        `title="Abrir o documento na página ${Number(p)}">${ICONE_DOC}${escapar(nomeDocumento(f.arquivo))} · p. ${Number(p)}</a>`
      );
    }
  }
  return '<div class="fontes"><span class="fontes-titulo">Fontes consultadas (clique para abrir)</span>' +
    chips.slice(0, MAX_FONTES).join("") + "</div>";
}

// Botões que levam ao site oficial para resolver o assunto (emitir DAS, declarar...).
function htmlAcoes(acoes) {
  if (!acoes || !acoes.length) return "";
  return '<div class="acoes-portal">' + acoes.map((a) =>
    `<a class="acao-portal" href="${escapar(a.url)}" target="_blank" rel="noopener" ` +
    `title="Abre o site oficial do governo">${escapar(a.rotulo)}${ICONE_EXTERNO}</a>`
  ).join("") + "</div>";
}

function rolarParaBaixo() {
  requestAnimationFrame(() => { areaMensagens.scrollTop = areaMensagens.scrollHeight; });
}

/* =========================================================================
   Mensagens na tela
   ========================================================================= */
function criarMensagem(tipo, extraClasse = "") {
  const msg = document.createElement("div");
  msg.className = `msg ${tipo} ${extraClasse}`.trim();
  if (tipo === "bot") {
    const av = document.createElement("img");
    av.src = "icon.svg"; av.alt = ""; av.className = "mini-avatar";
    msg.appendChild(av);
  }
  const corpo = document.createElement("div");
  corpo.className = "msg-corpo";
  const balao = document.createElement("div");
  balao.className = "balao";
  corpo.appendChild(balao);
  msg.appendChild(corpo);
  areaMensagens.appendChild(msg);
  rolarParaBaixo();
  return { msg, corpo, balao };
}

function mostrarUsuario(texto) {
  criarMensagem("user").balao.innerHTML = `<p>${escapar(texto)}</p>`;
}

function mostrarErro(texto) {
  criarMensagem("bot", "erro-msg").balao.innerHTML = `<p>${escapar(texto)}</p>`;
}

// Preenche uma mensagem do bot já concluída: texto, fontes, atalhos e botão copiar.
function finalizarBot(partes, dados) {
  partes.balao.innerHTML = markdownSimples(dados.texto) + htmlFontes(dados.fontes) + htmlAcoes(dados.acoes);
  const ferramentas = document.createElement("div");
  ferramentas.className = "msg-ferramentas";
  const copiar = document.createElement("button");
  copiar.type = "button";
  copiar.className = "botao-ferramenta";
  copiar.innerHTML = `${ICONE_COPIAR}<span>Copiar</span>`;
  copiar.title = "Copiar resposta";
  copiar.addEventListener("click", () => copiarTexto(dados.texto, copiar));
  ferramentas.appendChild(copiar);
  partes.corpo.appendChild(ferramentas);
}

function mostrarBot(dados) {
  finalizarBot(criarMensagem("bot"), dados);
}

// Sugestões de próximas perguntas (só abaixo da última resposta)
function mostrarSugestoesContinuacao(sugestoes) {
  removerSugestoesContinuacao();
  if (!sugestoes || !sugestoes.length) return;
  const caixa = document.createElement("div");
  caixa.className = "sugestoes-continuacao";
  caixa.id = "sugestoes-continuacao";
  caixa.setAttribute("aria-label", "Sugestões de próximas perguntas");
  for (const s of sugestoes) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = s;
    b.addEventListener("click", () => enviarPergunta(s));
    caixa.appendChild(b);
  }
  areaMensagens.appendChild(caixa);
  rolarParaBaixo();
}

function removerSugestoesContinuacao() {
  const antiga = document.getElementById("sugestoes-continuacao");
  if (antiga) antiga.remove();
}

async function copiarTexto(texto, botaoCopiar) {
  const limpo = texto.replace(/\*\*(.+?)\*\*/g, "$1");
  try {
    await navigator.clipboard.writeText(limpo);
  } catch (_) {
    // Navegadores antigos ou página sem HTTPS
    const area = document.createElement("textarea");
    area.value = limpo;
    area.style.position = "fixed"; area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } catch (_) { /* sem suporte */ }
    area.remove();
  }
  botaoCopiar.innerHTML = `${ICONE_OK}<span>Copiado!</span>`;
  botaoCopiar.classList.add("copiado");
  setTimeout(() => {
    botaoCopiar.innerHTML = `${ICONE_COPIAR}<span>Copiar</span>`;
    botaoCopiar.classList.remove("copiado");
  }, 1600);
}

function atualizarBotoes() {
  botao.disabled = !pronto || enviando || !campo.value.trim();
  botaoNova.disabled = !pronto || enviando || mensagens.length === 0;
}

function mostrarBoasVindas() {
  areaMensagens.innerHTML = "";
  areaMensagens.appendChild(modeloBoasVindas.content.cloneNode(true));
  document.getElementById("sugestoes").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) enviarPergunta(b.dataset.pergunta);
  });
}

/* =========================================================================
   Conversa salva no navegador
   ========================================================================= */
function salvarConversa() {
  try {
    localStorage.setItem(CHAVE_ARMAZENAMENTO, JSON.stringify(mensagens.slice(-MAX_SALVAS)));
  } catch (_) { /* modo anônimo ou armazenamento bloqueado: segue sem salvar */ }
}

function carregarConversa() {
  try {
    const dados = JSON.parse(localStorage.getItem(CHAVE_ARMAZENAMENTO) || "[]");
    if (Array.isArray(dados)) {
      return dados.filter((m) => m && (m.papel === "user" || m.papel === "assistant") && typeof m.texto === "string");
    }
  } catch (_) { /* ignora dados inválidos */ }
  return [];
}

function apagarConversaSalva() {
  try { localStorage.removeItem(CHAVE_ARMAZENAMENTO); } catch (_) { /* ignora */ }
}

function restaurarConversa() {
  mensagens = carregarConversa();
  if (!mensagens.length) { mostrarBoasVindas(); return; }
  areaMensagens.innerHTML = "";
  areaMensagens.classList.add("restaurando");   // sem animação de entrada
  for (const m of mensagens) {
    if (m.papel === "user") mostrarUsuario(m.texto);
    else mostrarBot(m);
  }
  const ultima = mensagens[mensagens.length - 1];
  if (ultima.papel === "assistant") mostrarSugestoesContinuacao(ultima.sugestoes);
  setTimeout(() => areaMensagens.classList.remove("restaurando"), 50);
}

/* =========================================================================
   Envio de perguntas (resposta chega aos poucos)
   ========================================================================= */
async function lerEventos(resposta, aoReceber) {
  // O servidor manda uma linha JSON por evento (NDJSON).
  const leitor = resposta.body.getReader();
  const decodificador = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await leitor.read();
    if (done) break;
    buffer += decodificador.decode(value, { stream: true });
    let quebra;
    while ((quebra = buffer.indexOf("\n")) >= 0) {
      const linha = buffer.slice(0, quebra).trim();
      buffer = buffer.slice(quebra + 1);
      if (linha) aoReceber(JSON.parse(linha));
    }
  }
  if (buffer.trim()) aoReceber(JSON.parse(buffer));
}

async function enviarPergunta(pergunta) {
  if (!pergunta || enviando || !pronto) return;
  enviando = true;
  const sugestoes = document.getElementById("sugestoes");
  if (sugestoes) sugestoes.remove();
  const boasVindas = areaMensagens.querySelector(".boas-vindas");
  if (boasVindas) boasVindas.remove();
  removerSugestoesContinuacao();

  const historico = mensagens.slice(-MAX_HISTORICO).map(({ papel, texto }) => ({ papel, texto }));
  mostrarUsuario(pergunta);
  campo.value = "";
  ajustarAltura();
  atualizarBotoes();

  const partes = criarMensagem("bot");
  partes.balao.innerHTML = '<span class="digitando" aria-label="Digitando"><span></span><span></span><span></span></span>';

  let texto = "";
  let final = null;
  let erro = null;
  let agendado = false;
  const desenhar = () => {
    agendado = false;
    // O cursor vai dentro do último parágrafo/item, logo após a última palavra.
    const cursor = '<span class="cursor" aria-hidden="true"></span>';
    const html = markdownSimples(limparCitacoes(texto));
    const comCursor = html.replace(/<\/(p|li)>(<\/[uo]l>)?$/, `${cursor}</$1>$2`);
    partes.balao.innerHTML = comCursor === html ? html + cursor : comCursor;
    rolarParaBaixo();
  };

  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pergunta, historico }),
    });
    if (!r.ok || !r.body) {
      const d = await r.json().catch(() => ({}));
      erro = d.erro || "Erro inesperado.";
    } else {
      await lerEventos(r, (ev) => {
        if (ev.tipo === "texto") {
          texto += ev.conteudo;
          if (!agendado) { agendado = true; requestAnimationFrame(desenhar); }
        } else if (ev.tipo === "fim") {
          final = ev;
        } else if (ev.tipo === "erro") {
          erro = ev.mensagem;
        }
      });
      if (!final && !erro) erro = "A resposta foi interrompida. Tente novamente.";
    }
  } catch (_) {
    erro = "Sem conexão com o servidor. Verifique sua internet e tente novamente.";
  }

  if (final) {
    const dados = {
      papel: "assistant", texto: final.resposta, fontes: final.fontes,
      acoes: final.acoes, sugestoes: final.sugestoes,
    };
    agendado = true;   // impede um último "desenhar" atrasado
    finalizarBot(partes, dados);
    mensagens.push({ papel: "user", texto: pergunta }, dados);
    salvarConversa();
    mostrarSugestoesContinuacao(dados.sugestoes);
  } else {
    agendado = true;
    partes.msg.remove();
    mostrarErro(erro);
  }

  enviando = false;
  atualizarBotoes();
  rolarParaBaixo();
  if (!telaDeToque) campo.focus();
}

function ajustarAltura() {
  if (!campo.value) { campo.style.height = ""; return; }   // vazio: volta a 1 linha
  campo.style.height = "auto";
  campo.style.height = Math.min(campo.scrollHeight, 140) + "px";
}

formulario.addEventListener("submit", (e) => {
  e.preventDefault();
  enviarPergunta(campo.value.trim());
});

campo.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !telaDeToque && !e.isComposing) {
    e.preventDefault();
    formulario.requestSubmit();
  }
});

campo.addEventListener("input", () => { ajustarAltura(); atualizarBotoes(); });

botaoNova.addEventListener("click", () => {
  if (enviando) return;
  mensagens = [];
  apagarConversaSalva();
  mostrarBoasVindas();
  atualizarBotoes();
  campo.value = "";
  ajustarAltura();
  if (!telaDeToque) campo.focus();
});

// Mantém a última mensagem visível quando o teclado do celular abre
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => {
    if (document.activeElement === campo) rolarParaBaixo();
  });
}

restaurarConversa();
atualizarBotoes();
desenharProgresso();
animacao = setInterval(animarProgresso, 120);
verificarStatus();
