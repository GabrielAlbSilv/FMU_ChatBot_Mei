/* =========================================================================
   FiscOn (MEI e Receita Federal) — página do chat
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

// A tela de carregamento fica visível por pelo menos este tempo, mesmo que o servidor
// já esteja pronto: a barra avança no máximo proporcionalmente ao tempo decorrido.
const TEMPO_MINIMO_MS = 6000;
// % da barra em que cada item da lista de etapas começa
const INICIO_ETAPAS = [0, 14, 45, 66, 92];

let progresso = 0;     // valor exibido
let alvo = 2;          // último marco real informado pelo servidor
let teto = 10;         // até onde a barra pode "andar sozinha" enquanto espera o próximo marco
let etapaIdx = 0;      // etapa real informada pelo servidor
let servidorPronto = false;
let falhou = false;
let animacao = null;

// Etapa mostrada: acompanha a barra, mas nunca passa da etapa real do servidor.
function etapaVisivel() {
  if (progresso >= 100) return Math.min(itensEtapas.length, etapaIdx);
  let idx = 0;
  INICIO_ETAPAS.forEach((inicio, i) => { if (progresso >= inicio) idx = i; });
  return Math.min(idx, etapaIdx);
}

function desenharProgresso() {
  const pct = Math.round(progresso);
  const idx = etapaVisivel();
  barraPreenchida.style.width = pct + "%";
  pctEl.textContent = pct + "%";
  barra.setAttribute("aria-valuenow", String(pct));
  itensEtapas.forEach((li, i) => {
    li.classList.toggle("feita", i < idx);
    li.classList.toggle("ativa", i === idx && !falhou);
  });
  if (itensEtapas[idx]) etapaAtualEl.textContent = itensEtapas[idx].textContent;
  else etapaAtualEl.textContent = "Pronto!";
}

function animarProgresso() {
  const decorrido = Date.now() - inicioCarregamento;
  const limiteTempo = Math.min(100, (decorrido / TEMPO_MINIMO_MS) * 100);
  // Sobe rápido até o marco real e depois avança devagar (sem nunca voltar),
  // respeitando o ritmo mínimo de TEMPO_MINIMO_MS.
  let novo = progresso;
  if (novo < alvo) novo += Math.max((alvo - novo) * 0.25, 0.4);
  else if (novo < teto) novo += (teto - novo) * 0.025;
  progresso = Math.max(progresso, Math.min(novo, limiteTempo, 100));
  desenharProgresso();
  if (servidorPronto && progresso >= 99.5 && decorrido >= TEMPO_MINIMO_MS) esconderTelaCarregando();
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
    msgCarregando.textContent = "Carregando os documentos sobre MEI e Receita Federal…";
  }
}

// Servidor pronto: a barra segue até 100% no ritmo mínimo e a tela some sozinha.
function concluirCarregamento() {
  servidorPronto = true;
  definirMarco(100, itensEtapas.length);
  teto = 100;
}

function esconderTelaCarregando() {
  clearInterval(animacao);
  progresso = 100;
  desenharProgresso();
  setTimeout(() => {
    tela.classList.add("oculta");
    setTimeout(() => tela.remove(), 500);
    if (!telaDeToque) campo.focus();
  }, 450);
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
      const baseData = document.getElementById("base-data");
      if (baseData) {
        const partesData = [];
        const dt = new Date(d.base_atualizada_em || "");
        if (!isNaN(dt)) partesData.push("Base de documentos atualizada em " + dt.toLocaleDateString("pt-BR") + ".");
        const web = d.base_web && d.base_web.atualizada_em ? new Date(d.base_web.atualizada_em) : null;
        if (web && !isNaN(web)) {
          partesData.push("Sites oficiais consultados em " + web.toLocaleDateString("pt-BR") + " às " +
            web.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) + ".");
        }
        baseData.textContent = partesData.join(" ");
      }
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

// Links na resposta: só viram clicáveis endereços oficiais (gov.br, Sebrae, FMU), sempre em https.
const DOMINIO_LINK = /^(?:[a-z0-9-]+\.)*(?:gov\.br|sebrae\.com\.br|fmu\.br)$/i;
const DOMINIO_NO_TEXTO = /(^|[^@\w.\/-])((?:https?:\/\/)?(?:[a-z0-9-]+\.)*(?:gov\.br|sebrae\.com\.br|fmu\.br)(?:\/[^\s<>"*]*)?)/gi;

function linkSeguro(endereco) {
  try {
    const bruto = String(endereco).replace(/&amp;/g, "&");
    const u = new URL(/^https?:\/\//i.test(bruto) ? bruto : "https://" + bruto);
    if (!DOMINIO_LINK.test(u.hostname) || u.username || u.password) return null;
    u.protocol = "https:";
    return u.href;
  } catch (_) { return null; }
}

function linkificar(t, guardados) {
  const guardar = (href, rotulo) => {
    guardados.push(`<a href="${escapar(href)}" target="_blank" rel="noopener">${rotulo}</a>`);
    return `\u0000${guardados.length - 1}\u0000`;
  };
  return t
    // [texto](endereço)
    .replace(/\[([^\]\n]{1,120})\]\(([^\s)]{4,300})\)/g, (m, rotulo, url) => {
      const href = linkSeguro(url);
      return href ? guardar(href, rotulo.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")) : rotulo;
    })
    // endereço solto: gov.br/mei, https://www.gov.br/receitafederal/...
    .replace(DOMINIO_NO_TEXTO, (m, antes, url) => {
      let resto = "";
      const entidade = url.search(/&(?:quot|lt|gt|#39);/);       // aspas/sinais já escapados
      if (entidade >= 0) { resto = url.slice(entidade); url = url.slice(0, entidade); }
      const sobra = (url.match(/[.,;:!?)\]]+$/) || [""])[0];      // pontuação do fim da frase fica fora
      const limpo = sobra ? url.slice(0, -sobra.length) : url;
      const href = limpo.includes("\u0000") ? null : linkSeguro(limpo);
      return href ? antes + guardar(href, limpo) + sobra + resto : m;
    });
}

// Markdown simples e seguro: títulos, negrito, itálico, listas, links oficiais e parágrafos
function markdownSimples(texto) {
  const linhas = escapar(texto).split("\n");
  let html = "", lista = null;
  const inline = (t) => {
    const links = [];
    return linkificar(t, links)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|\s)\*(?!\s)(.+?)\*(?=\s|$|[.,;:!?])/g, "$1<em>$2</em>")
      .replace(/\u0000(\d+)\u0000/g, (m, i) => links[Number(i)] || "");
  };
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

// Ícones de traço (mesmo estilo dos demais): svg("conteúdo do desenho")
const svg = (d, extra = "") => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extra}>${d}</svg>`;
const ICONE_LIVRO = svg('<path d="M4 19.5A2.5 2.5 0 016.5 17H20V3H6.5A2.5 2.5 0 004 5.5z"/><path d="M4 19.5A2.5 2.5 0 006.5 22H20v-5"/>');
const ICONE_EMAIL = svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>');
const ICONE_CALENDARIO = svg('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18M8 14h2M14 14h2M8 18h2"/>');
const ICONE_REPETIR = svg('<path d="M3 12a9 9 0 0115.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 01-15.5 6.2L3 16"/><path d="M3 21v-5h5"/>');
const ICONE_FONE = svg('<path d="M4 14v-2a8 8 0 0116 0v2"/><rect x="2" y="14" width="5" height="7" rx="2"/><rect x="17" y="14" width="5" height="7" rx="2"/>');

// Botões de atalho (o servidor manda o nome do ícone em "icone"; ver ACOES_PORTAL no rag.py)
const ICONES_ACAO = {
  boleto: svg('<path d="M4 3h16v18l-3-2-3 2-2-2-2 2-3-2-3 2z"/><path d="M8 8h8M8 12h8M8 16h5"/>'),
  declaracao: svg('<path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V9z"/><path d="M14 3v6h6"/><path d="M9 15l2 2 4-4"/>'),
  abrir: svg('<path d="M3 9l1.5-5h15L21 9"/><path d="M3 9h18v1.5a3 3 0 01-6 0 3 3 0 01-6 0 3 3 0 01-6 0z"/><path d="M5 13v8h14v-8"/><path d="M10 21v-5h4v5"/>'),
  servicos: svg('<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 012-2h4a2 2 0 012 2v2M3 13h18"/>'),
  nota: svg('<path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>'),
  parcelar: ICONE_CALENDARIO,
  leao: svg('<path d="M3 21h18M4 10h16M12 3l8 5H4z"/><path d="M6 10v8M10 10v8M14 10v8M18 10v8"/>'),
  cpf: svg('<rect x="2" y="5" width="20" height="14" rx="2"/><circle cx="8" cy="11" r="2.5"/><path d="M4.5 16.5a4 4 0 017 0M14 10h5M14 14h3"/>'),
  cnpj: svg('<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M9 21v-4h6v4M8 7h2M14 7h2M8 11h2M14 11h2"/>'),
};

// Selo da conferência com Sebrae/Receita Federal
const ICONES_VERIFICACAO = {
  ok: svg('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>'),
  atualizada: svg('<path d="M3 12a9 9 0 0115.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 01-15.5 6.2L3 16"/><path d="M3 21v-5h5"/>'),
  neutra: svg('<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>'),
};

// Chips das fontes: cada página abre o PDF exatamente nela.
function htmlFontes(fontes, paginasOficiais) {
  if ((!fontes || !fontes.length) && (!paginasOficiais || !paginasOficiais.length)) return "";
  const chips = [];
  for (const f of fontes || []) {
    if (typeof f === "string") {           // formato antigo salvo no navegador
      chips.push(`<span class="fonte">${ICONE_DOC}${escapar(f)}</span>`);
      continue;
    }
    const url = "/documentos/" + encodeURIComponent(f.arquivo);
    const rotulo = f.rotulo || nomeDocumento(f.arquivo);
    const titulo = f.titulo || nomeDocumento(f.arquivo);
    for (const p of f.paginas || []) {
      chips.push(
        `<a class="fonte" href="${url}#page=${Number(p)}" target="_blank" rel="noopener" ` +
        `title="${escapar(titulo)} — abrir na página ${Number(p)}">${ICONE_DOC}${escapar(rotulo)} · p. ${Number(p)}</a>`
      );
    }
  }
  for (const p of paginasOficiais || []) {
    if (!p || !urlOficial(p.url)) continue;
    const quando = p.data ? ` · ${escapar(p.data)}` : "";
    chips.push(
      `<a class="fonte fonte-site" href="${escapar(p.url)}" target="_blank" rel="noopener" ` +
      `title="${escapar(p.titulo || "Página oficial")} — abrir no site">${ICONE_EXTERNO}${escapar(p.site || "Site oficial")} · ` +
      `${escapar(String(p.titulo || "página oficial").slice(0, 48))}${quando}</a>`
    );
  }
  if (!chips.length) return "";
  const extras = chips.length > MAX_FONTES ? `<span class="fonte fonte-mais">+${chips.length - MAX_FONTES}</span>` : "";
  return `<div class="fontes"><span class="fontes-titulo">${ICONE_LIVRO}Fontes consultadas (clique para abrir)</span>` +
    chips.slice(0, MAX_FONTES).join("") + extras + "</div>";
}

// Só links dos sites do Sebrae e da Receita Federal são exibidos (o servidor já filtra; aqui é uma 2ª barreira).
function urlOficial(url) {
  try {
    const u = new URL(url);
    const h = u.hostname.toLowerCase();
    if (u.protocol !== "https:") return false;
    if (h === "sebrae.com.br" || h.endsWith(".sebrae.com.br")) return true;
    if (h === "receita.fazenda.gov.br" || h.endsWith(".receita.fazenda.gov.br")) return true;
    return (h === "gov.br" || h === "www.gov.br") && /^\/receitafederal(\/|$)/i.test(u.pathname);
  } catch (_) { return false; }
}

// Selo "conferido com Sebrae e Receita Federal": mostra se os PDFs foram confirmados ou atualizados.
function htmlVerificacao(v) {
  if (!v || !v.status || v.status === "nao_aplicavel") return "";
  const links = (v.fontes_web || []).filter((f) => urlOficial(f.url)).map((f) =>
    `<a class="fonte" href="${escapar(f.url)}" target="_blank" rel="noopener" title="Abrir a página oficial">` +
    `${ICONE_EXTERNO}${escapar(f.site)} · ${escapar(String(f.titulo || "página oficial").slice(0, 60))}</a>`
  ).join("");
  const data = v.data ? ` em ${escapar(v.data)}` : "";
  let classe, texto, extra = "";
  if (v.status === "confirmado") {
    classe = "ok";
    texto = v.origem === "base_local"
      ? `<strong>Conferido com o Sebrae e a Receita Federal.</strong> As informações dos documentos batem com as páginas oficiais coletadas${data}.`
      : `<strong>Conferido com o Sebrae e a Receita Federal${data}.</strong> As informações dos documentos continuam válidas.`;
  } else if (v.status === "atualizada") {
    classe = "atualizada";
    const sites = [...new Set((v.fontes_web || []).map((f) => f.site))].join(" e ") || "Sebrae e Receita Federal";
    const coleta = v.origem === "base_local" ? " coletadas" : "";
    texto = `<strong>Resposta atualizada.</strong> Os documentos (PDFs) estavam desatualizados; usei o que consta nas páginas oficiais (${escapar(sites)})${coleta}${data}.`;
    const divs = (v.divergencias || []).filter((d) => d && (d.pdf || d.site));
    if (divs.length) {
      extra = '<details class="verif-detalhes"><summary>O que mudou</summary><ul>' + divs.map((d) =>
        `<li><strong>${escapar(d.tema || "Informação")}:</strong> os PDFs diziam <em>${escapar(d.pdf || "—")}</em>; ` +
        `o site informa <em>${escapar(d.site || "—")}</em>.</li>`).join("") + "</ul></details>";
    }
  } else if (v.status === "inconclusivo") {
    classe = "neutra";
    const n = Number(v.consultadas) || 0;
    texto = `Li ${n || "algumas"} página(s) do Sebrae e da Receita Federal, mas nenhuma trata diretamente deste ponto; a resposta se baseia nos documentos (PDFs).`;
    if (v.motivo) extra = `<p class="verif-motivo">${escapar(v.motivo)}</p>`;
  } else {
    classe = "neutra";
    const leu = Number(v.consultadas) > 0;
    texto = leu
      ? "Consegui abrir páginas do Sebrae e da Receita Federal, mas a conferência não terminou. A resposta se baseia nos documentos (PDFs) e pode estar desatualizada."
      : "Não consegui ler os sites do Sebrae e da Receita Federal agora. A resposta se baseia nos documentos (PDFs) e pode estar desatualizada.";
    if (v.motivo) extra = `<p class="verif-motivo">${escapar(v.motivo)}</p>`;
  }
  const icone = `<span class="verif-icone">${ICONES_VERIFICACAO[classe] || ICONES_VERIFICACAO.neutra}</span>`;
  return `<div class="verificacao ${classe}"><div class="verif-linha">${icone}<p>${texto}</p></div>${extra}${links ? `<div class="fontes-web">${links}</div>` : ""}</div>`;
}

// Botões que levam ao site oficial para resolver o assunto (emitir DAS, declarar...).
// Mesma regra do resto da página: só endereços https.
function htmlAcoes(acoes) {
  const validas = (acoes || []).filter((a) => a && /^https:\/\//i.test(String(a.url || "")));
  if (!validas.length) return "";
  return '<div class="acoes-portal">' + validas.map((a) =>
    `<a class="acao-portal" href="${escapar(a.url)}" target="_blank" rel="noopener" ` +
    `title="Abre o site oficial">${ICONES_ACAO[a.icone] || ""}<span>${escapar(a.rotulo)}</span>${ICONE_EXTERNO}</a>`
  ).join("") + "</div>";
}

// Contato do NAF (Núcleo de Apoio Fiscal) exibido ao fim de cada resposta.
const NAF_EMAIL = "naf@fmu.br";
const NAF_FORMULARIO = "https://forms.cloud.microsoft/r/qweuKC6AxF";   // o mesmo link do QR Code
const NAF_QR = "Qr_Code_NAF.jpg";

function htmlContatoNaf() {
  return '<div class="contato-naf">' +
    `<a class="naf-qr" href="${NAF_FORMULARIO}" target="_blank" rel="noopener" title="Abrir o formulário de agendamento">` +
      `<img src="${NAF_QR}" alt="QR Code do formulário de agendamento do NAF" width="96" height="96" loading="lazy">` +
    '</a>' +
    '<div class="naf-texto">' +
      `<strong><span class="naf-icone">${ICONE_FONE}</span>Para agendamento presencial ou online, entre em contato com o NAF - Núcleo de Apoio Fiscal</strong>` +
      `<span class="naf-linha">${ICONE_EMAIL}E-mail: <a href="mailto:${NAF_EMAIL}">${NAF_EMAIL}</a></span>` +
      `<span class="naf-linha">${ICONE_CALENDARIO}<a href="${NAF_FORMULARIO}" target="_blank" rel="noopener">Formulário agendamento</a></span>` +
      '<small>Ou aponte a câmera do celular para o QR Code.</small>' +
    '</div></div>';
}

function rolarParaBaixo(suave = true) {
  requestAnimationFrame(() => {
    areaMensagens.scrollTo({ top: areaMensagens.scrollHeight, behavior: suave ? "smooth" : "auto" });
  });
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
  const partes = criarMensagem("user");
  partes.balao.innerHTML = `<p>${escapar(texto)}</p>`;
  return partes.msg;
}

// Erro com botão "Tentar novamente": refaz a mesma pergunta (sem precisar digitar de novo).
function mostrarErro(texto, pergunta, msgUsuario) {
  const partes = criarMensagem("bot", "erro-msg");
  partes.balao.innerHTML = `<p>${escapar(texto)}</p>`;
  if (!pergunta) return;
  const repetir = document.createElement("button");
  repetir.type = "button";
  repetir.className = "botao-ferramenta botao-repetir";
  repetir.innerHTML = `${ICONE_REPETIR}<span>Tentar novamente</span>`;
  repetir.addEventListener("click", () => {
    if (enviando || !pronto) return;
    partes.msg.remove();
    if (msgUsuario) msgUsuario.remove();
    enviarPergunta(pergunta);
  });
  const ferramentas = document.createElement("div");
  ferramentas.className = "msg-ferramentas";
  ferramentas.appendChild(repetir);
  partes.corpo.appendChild(ferramentas);
}

// Preenche uma mensagem do bot já concluída: texto, fontes, atalhos e botão copiar.
// "surgir" = fontes, atalhos e botão copiar aparecem com um leve fade, um após o outro.
function finalizarBot(partes, dados, surgir = false) {
  partes.balao.innerHTML = markdownSimples(dados.texto) + htmlFontes(dados.fontes, dados.paginas_oficiais) + htmlVerificacao(dados.verificacao) + htmlAcoes(dados.acoes) + htmlContatoNaf();
  removerIndicadorConferencia(partes);
  const ferramentas = document.createElement("div");
  ferramentas.className = "msg-ferramentas";
  if (surgir) {
    [...partes.balao.querySelectorAll(".fontes, .verificacao, .acoes-portal, .contato-naf"), ferramentas].forEach((el, i) => {
      el.classList.add("surgir");
      el.style.animationDelay = `${i * 120}ms`;
    });
    rolarParaBaixo();
  }
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

/* ---------- "Digitação" suave ----------
   O texto chega da rede em rajadas; aqui ele é revelado num ritmo constante.
   Velocidade: VELOCIDADE_DIGITACAO caracteres/s, acelerando aos poucos quando há muito
   texto acumulado (respostas longas não demoram demais). */
const VELOCIDADE_DIGITACAO = 70;    // caracteres por segundo (ritmo base)
const FOLGA_ACELERACAO_S = 6;       // acumulado é escrito em ~6 s, no máximo, quando grande
const PAUSA_ANTES_DO_BOT_MS = 350;  // tempo entre a mensagem do usuário e o "digitando..."

const esperar = (ms) => new Promise((r) => setTimeout(r, ms));
// Quem pediu "reduzir movimento" no sistema recebe o texto inteiro, sem animação.
const movimentoReduzido = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function criarDigitador(balao) {
  let alvo = "";          // texto disponível para mostrar
  let mostrado = 0;       // quantos caracteres já aparecem (fracionário)
  let fechado = false;    // não virá mais texto
  let parado = false;
  let rodando = false;
  let ultimo = 0;
  let concluir;
  const terminou = new Promise((r) => { concluir = r; });
  const CURSOR = '<span class="cursor" aria-hidden="true"></span>';

  function desenhar() {
    let parcial = alvo.slice(0, Math.floor(mostrado));
    // Não deixa "**" sem par aparecer cru enquanto o negrito ainda está sendo escrito.
    if ((parcial.match(/\*\*/g) || []).length % 2) parcial += "**";
    const html = markdownSimples(parcial);
    // O cursor fica dentro do último parágrafo/item, logo após a última letra.
    const comCursor = html.replace(/<\/(p|li)>(<\/[uo]l>)?$/, `${CURSOR}</$1>$2`);
    balao.innerHTML = comCursor === html ? html + CURSOR : comCursor;
    acompanharRolagem();
  }

  function quadro(agora) {
    if (parado) return;
    const dt = Math.min(agora - ultimo, 100) / 1000;   // evita saltos após aba em segundo plano
    ultimo = agora;
    const pendente = alvo.length - mostrado;
    if (pendente > 0) {
      const velocidade = movimentoReduzido ? Infinity
        : Math.max(VELOCIDADE_DIGITACAO, pendente / FOLGA_ACELERACAO_S);
      const antes = Math.floor(mostrado);
      mostrado = Math.min(alvo.length, mostrado + velocidade * dt);
      if (Math.floor(mostrado) !== antes) desenhar();
    }
    if (fechado && mostrado >= alvo.length) { rodando = false; concluir(); return; }
    requestAnimationFrame(quadro);
  }

  function iniciar() {
    if (rodando || parado) return;
    rodando = true;
    ultimo = performance.now();
    requestAnimationFrame(quadro);
  }

  return {
    atualizar(texto) {
      alvo = texto;
      if (mostrado > alvo.length) mostrado = alvo.length;
      iniciar();
    },
    fechar() {
      fechado = true;
      // Aba em segundo plano não anima (o navegador pausa): mostra tudo de uma vez.
      if (document.hidden) {
        mostrado = alvo.length;
        parado = true;
        desenhar();
        concluir();
        return;
      }
      iniciar();
    },
    parar() { parado = true; concluir(); },
    terminou,
  };
}

// Durante a digitação, só acompanha o fim se a pessoa não rolou para cima para ler.
function acompanharRolagem() {
  const distancia = areaMensagens.scrollHeight - areaMensagens.scrollTop - areaMensagens.clientHeight;
  if (distancia < 140) areaMensagens.scrollTop = areaMensagens.scrollHeight;
}

// Enquanto a resposta (já visível) é conferida nos sites oficiais, mostra um aviso discreto abaixo.
function mostrarIndicadorConferencia(partes, texto) {
  let el = partes.corpo.querySelector(".conferindo");
  if (!el) {
    el = document.createElement("div");
    el.className = "conferindo";
    el.setAttribute("role", "status");
    partes.corpo.appendChild(el);
  }
  el.innerHTML = `<span class="conferindo-icone">${ICONES_VERIFICACAO.ok}</span><span>${escapar(texto)}</span>`;
  acompanharRolagem();
}

function removerIndicadorConferencia(partes) {
  const el = partes.corpo.querySelector(".conferindo");
  if (el) el.remove();
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
  const msgUsuario = mostrarUsuario(pergunta);
  campo.value = "";
  ajustarAltura();
  atualizarBotoes();
  atualizarContador();
  rolarParaBaixo();
  // Leitores de tela: espera a resposta terminar para anunciar (não lê cada letra digitada).
  areaMensagens.setAttribute("aria-busy", "true");

  // Pequena pausa antes do bot "começar a digitar", como numa conversa real.
  await esperar(PAUSA_ANTES_DO_BOT_MS);
  const partes = criarMensagem("bot");
  partes.balao.innerHTML = '<span class="digitando" aria-label="Digitando"><span></span><span></span><span></span></span>';

  const digitador = criarDigitador(partes.balao);
  let texto = "";
  let final = null;
  let erro = null;

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
        if (ev.tipo === "status") {
          // etapa em andamento (ex.: "Consultando os documentos…" / "Conferindo com o Sebrae…")
          if (!texto) {
            partes.balao.innerHTML = '<span class="digitando" aria-hidden="true"><span></span><span></span><span></span></span>' +
              `<span class="status-etapa">${escapar(ev.conteudo)}</span>`;
          } else {
            mostrarIndicadorConferencia(partes, ev.conteudo);   // a resposta já está na tela
          }
        } else if (ev.tipo === "texto") {
          texto += ev.conteudo;
          digitador.atualizar(limparCitacoes(texto));
        } else if (ev.tipo === "fim") {
          final = ev;
          // Resposta trocada pela versão corrigida com os sites: não "redigita", troca de uma vez.
          if (!ev.substituida) digitador.atualizar(ev.resposta);   // texto final já limpo pelo servidor
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
    if (final.substituida) {
      digitador.parar();
    } else {
      // Espera a "digitação" terminar antes de mostrar fontes, atalhos e sugestões.
      digitador.fechar();
      await digitador.terminou;
    }
    const dados = {
      papel: "assistant", texto: final.resposta, fontes: final.fontes, paginas_oficiais: final.paginas_oficiais,
      acoes: final.acoes, sugestoes: final.sugestoes, verificacao: final.verificacao,
    };
    finalizarBot(partes, dados, true);
    if (final.substituida) {
      partes.balao.classList.add("substituida");   // destaque rápido: "esta resposta foi atualizada"
      setTimeout(() => partes.balao.classList.remove("substituida"), 2200);
    }
    mensagens.push({ papel: "user", texto: pergunta }, dados);
    salvarConversa();
    await esperar(250);
    mostrarSugestoesContinuacao(dados.sugestoes);
  } else {
    digitador.parar();
    partes.msg.remove();
    mostrarErro(erro, pergunta, msgUsuario);
  }

  areaMensagens.removeAttribute("aria-busy");
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

campo.addEventListener("input", () => { ajustarAltura(); atualizarBotoes(); atualizarContador(); });

/* ---------- Contador de caracteres (aparece perto do limite) ---------- */
const contador = document.getElementById("contador");
const LIMITE_CARACTERES = Number(campo.getAttribute("maxlength")) || 1000;

function atualizarContador() {
  if (!contador) return;
  const n = campo.value.length;
  contador.hidden = n < LIMITE_CARACTERES * 0.8;
  contador.textContent = `${n}/${LIMITE_CARACTERES}`;
  contador.classList.toggle("no-limite", n >= LIMITE_CARACTERES);
}

/* ---------- Botão "ir para o fim" (quando a pessoa sobe para reler) ---------- */
const botaoFim = document.getElementById("ir-fim");

function atualizarBotaoFim() {
  if (!botaoFim) return;
  const distancia = areaMensagens.scrollHeight - areaMensagens.scrollTop - areaMensagens.clientHeight;
  botaoFim.classList.toggle("visivel", distancia > 240);
}

if (botaoFim) {
  areaMensagens.addEventListener("scroll", atualizarBotaoFim, { passive: true });
  botaoFim.addEventListener("click", () => rolarParaBaixo());
}

/* ---------- Confirmação antes de começar uma nova conversa ---------- */
const dialogoNova = document.getElementById("dialogo-nova");

function fecharDialogo(valor) {
  if (!dialogoNova.open || dialogoNova.classList.contains("fechando")) return;
  // Animação de saída e depois fecha de verdade
  dialogoNova.classList.add("fechando");
  setTimeout(() => {
    dialogoNova.classList.remove("fechando");
    dialogoNova.close(valor);
  }, movimentoReduzido ? 0 : 160);
}

function pedirConfirmacaoNovaConversa() {
  if (typeof dialogoNova.showModal !== "function") {   // navegador muito antigo
    if (window.confirm("Iniciar uma nova conversa? A conversa atual será apagada.")) novaConversa();
    return;
  }
  dialogoNova.returnValue = "";
  dialogoNova.showModal();
  // Foco no "Cancelar": evita apagar a conversa com um Enter sem querer.
  dialogoNova.querySelector('[value="cancelar"]').focus();
}

// Os botões do formulário (method="dialog") passam pelo fechamento animado
dialogoNova.querySelector("form").addEventListener("submit", (e) => {
  e.preventDefault();
  fecharDialogo(e.submitter ? e.submitter.value : "cancelar");
});
// Clique fora do cartão ou tecla Esc = cancelar
dialogoNova.addEventListener("click", (e) => { if (e.target === dialogoNova) fecharDialogo("cancelar"); });
dialogoNova.addEventListener("cancel", (e) => { e.preventDefault(); fecharDialogo("cancelar"); });
dialogoNova.addEventListener("close", () => {
  if (dialogoNova.returnValue === "confirmar") novaConversa();
  else botaoNova.focus();
});

botaoNova.addEventListener("click", () => {
  if (enviando || !mensagens.length) return;
  pedirConfirmacaoNovaConversa();
});

function novaConversa() {
  if (enviando) return;
  mensagens = [];
  apagarConversaSalva();
  mostrarBoasVindas();
  atualizarBotoes();
  campo.value = "";
  ajustarAltura();
  if (!telaDeToque) campo.focus();
}

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
