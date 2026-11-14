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
// Memória da conversa: o bot entende perguntas de continuação ("e quanto custa?")
let historico = [];
const MAX_HISTORICO = 6;
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

// "3-Perguntas&Respostas2_MEI.pdf (p. 28)" -> "Perguntas & Respostas 2 MEI · p. 28"
function nomeFonte(fonte) {
  const m = String(fonte).match(/^(.*?)(?:\s*\((p\.[^)]*)\))?$/);
  const nome = m[1]
    .replace(/^\d+-/, "")
    .replace(/(\.pdf)+$/i, "")
    .replace(/&/g, " & ")
    .replace(/_/g, " ")
    .replace(/([a-zà-ú])(\d)/gi, "$1 $2")
    .replace(/\s+/g, " ")
    .trim();
  return m[2] ? `${nome} · ${m[2]}` : nome;
}

const ICONE_DOC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V9z"/><path d="M14 3v6h6"/></svg>';

function rolarParaBaixo() {
  requestAnimationFrame(() => { areaMensagens.scrollTop = areaMensagens.scrollHeight; });
}

function adicionarMensagem(tipo, conteudoHtml, extraClasse = "") {
  const msg = document.createElement("div");
  msg.className = `msg ${tipo} ${extraClasse}`.trim();
  if (tipo === "bot") {
    const av = document.createElement("img");
    av.src = "icon.svg"; av.alt = ""; av.className = "mini-avatar";
    msg.appendChild(av);
  }
  const balao = document.createElement("div");
  balao.className = "balao";
  balao.innerHTML = conteudoHtml;
  msg.appendChild(balao);
  areaMensagens.appendChild(msg);
  rolarParaBaixo();
  return msg;
}

function atualizarBotoes() {
  botao.disabled = !pronto || enviando || !campo.value.trim();
  botaoNova.disabled = !pronto || enviando || historico.length === 0;
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
   Envio de perguntas
   ========================================================================= */
async function enviarPergunta(pergunta) {
  if (!pergunta || enviando || !pronto) return;
  enviando = true;
  const sugestoes = document.getElementById("sugestoes");
  if (sugestoes) sugestoes.remove();

  adicionarMensagem("user", `<p>${escapar(pergunta)}</p>`);
  campo.value = "";
  ajustarAltura();
  atualizarBotoes();

  const digitando = adicionarMensagem(
    "bot", '<span class="digitando" aria-label="Digitando"><span></span><span></span><span></span></span>'
  );

  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pergunta, historico: historico.slice(-MAX_HISTORICO) }),
    });
    const d = await r.json().catch(() => ({}));
    digitando.remove();

    if (!r.ok) {
      adicionarMensagem("bot", `<p>${escapar(d.erro || "Erro inesperado.")}</p>`, "erro-msg");
    } else {
      historico.push({ papel: "user", texto: pergunta }, { papel: "assistant", texto: d.resposta });
      let html = markdownSimples(d.resposta);
      if (d.fontes && d.fontes.length) {
        html += '<div class="fontes"><span class="fontes-titulo">Fontes consultadas</span>' +
          d.fontes.map((f) => `<span class="fonte" title="${escapar(f)}">${ICONE_DOC}${escapar(nomeFonte(f))}</span>`).join("") +
          "</div>";
      }
      adicionarMensagem("bot", html);
    }
  } catch (_) {
    digitando.remove();
    adicionarMensagem("bot", "<p>Sem conexão com o servidor. Verifique sua internet e tente novamente.</p>", "erro-msg");
  } finally {
    enviando = false;
    atualizarBotoes();
    if (!telaDeToque) campo.focus();
  }
}

function ajustarAltura() {
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
  historico = [];
  mostrarBoasVindas();
  atualizarBotoes();
  campo.value = "";
  ajustarAltura();
});

// Mantém a última mensagem visível quando o teclado do celular abre
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => {
    if (document.activeElement === campo) rolarParaBaixo();
  });
}

mostrarBoasVindas();
atualizarBotoes();
desenharProgresso();
animacao = setInterval(animarProgresso, 120);
verificarStatus();
