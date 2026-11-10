const areaMensagens = document.getElementById("mensagens");
const formulario = document.getElementById("formulario");
const campo = document.getElementById("pergunta");
const botao = document.getElementById("enviar");
const statusEl = document.getElementById("status");
const sugestoes = document.getElementById("sugestoes");

const telaCarregando = document.getElementById("tela-carregando");
const msgCarregando = document.getElementById("carregando-msg");
const inicioCarregamento = Date.now();

function esconderTelaCarregando() {
  telaCarregando.classList.add("oculta");
  setTimeout(() => telaCarregando.remove(), 500);
  campo.focus();
}

function atualizarMensagemCarregando() {
  const segundos = (Date.now() - inicioCarregamento) / 1000;
  if (segundos > 40) {
    msgCarregando.textContent = "Quase lá… o servidor gratuito demora um pouco para acordar.";
  } else if (segundos > 10) {
    msgCarregando.textContent = "Carregando os documentos sobre MEI…";
  }
}

let pronto = false;
let enviando = false;

/* ---------- utilidades ---------- */
function escapar(texto) {
  return texto.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Markdown simples e seguro: negrito, itálico, listas e parágrafos
function markdownSimples(texto) {
  const linhas = escapar(texto).split("\n");
  let html = "", lista = null;
  const inline = (t) => t
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\s)\*(?!\s)(.+?)\*(?=\s|$)/g, "$1<em>$2</em>");
  const fechar = () => { if (lista) { html += `</${lista}>`; lista = null; } };

  for (const linha of linhas) {
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

function rolarParaBaixo() {
  areaMensagens.scrollTop = areaMensagens.scrollHeight;
}

function adicionarMensagem(tipo, conteudoHtml, extraClasse = "") {
  const msg = document.createElement("div");
  msg.className = `msg ${tipo} ${extraClasse}`.trim();
  const balao = document.createElement("div");
  balao.className = "balao";
  balao.innerHTML = conteudoHtml;
  msg.appendChild(balao);
  areaMensagens.appendChild(msg);
  rolarParaBaixo();
  return msg;
}

function atualizarBotao() {
  botao.disabled = !pronto || enviando;
}

/* ---------- status do servidor ---------- */
async function verificarStatus() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    if (d.erro) {
      statusEl.textContent = "Indisponível";
      statusEl.className = "status erro";
      telaCarregando.classList.add("falhou");
      telaCarregando.querySelector("h2").textContent = "Não foi possível iniciar";
      msgCarregando.textContent = "Recarregue a página em alguns instantes. Se o problema continuar, tente mais tarde.";
      return;
    }
    if (d.pronto) {
      pronto = true;
      statusEl.textContent = "Online";
      statusEl.className = "status pronto";
      atualizarBotao();
      esconderTelaCarregando();
      return;
    }
  } catch (_) { /* servidor acordando; tenta de novo */ }
  statusEl.textContent = "Preparando…";
  statusEl.className = "status carregando";
  atualizarMensagemCarregando();
  setTimeout(verificarStatus, 2000);
}

/* ---------- envio de perguntas ---------- */
async function enviarPergunta(pergunta) {
  if (!pergunta || enviando || !pronto) return;
  enviando = true;
  atualizarBotao();
  if (sugestoes) sugestoes.remove();

  adicionarMensagem("user", `<p>${escapar(pergunta)}</p>`);
  campo.value = "";
  campo.style.height = "auto";

  const digitando = adicionarMensagem(
    "bot", '<span class="digitando"><span></span><span></span><span></span></span>'
  );

  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pergunta }),
    });
    const d = await r.json().catch(() => ({}));
    digitando.remove();

    if (!r.ok) {
      adicionarMensagem("bot", `<p>${escapar(d.erro || "Erro inesperado.")}</p>`, "erro-msg");
    } else {
      let html = markdownSimples(d.resposta);
      if (d.fontes && d.fontes.length) {
        html += `<div class="fontes">Fontes: ${d.fontes.map(escapar).join(", ")}</div>`;
      }
      adicionarMensagem("bot", html);
    }
  } catch (_) {
    digitando.remove();
    adicionarMensagem("bot", "<p>Sem conexão com o servidor. Tente novamente.</p>", "erro-msg");
  } finally {
    enviando = false;
    atualizarBotao();
    campo.focus();
  }
}

formulario.addEventListener("submit", (e) => {
  e.preventDefault();
  enviarPergunta(campo.value.trim());
});

campo.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formulario.requestSubmit();
  }
});

campo.addEventListener("input", () => {
  campo.style.height = "auto";
  campo.style.height = Math.min(campo.scrollHeight, 140) + "px";
});

if (sugestoes) {
  sugestoes.addEventListener("click", (e) => {
    if (e.target.tagName === "BUTTON") enviarPergunta(e.target.textContent);
  });
}

atualizarBotao();
verificarStatus();
