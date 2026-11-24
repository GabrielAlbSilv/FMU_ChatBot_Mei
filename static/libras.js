/* =========================================================================
   FiscOn — Libras (Língua Brasileira de Sinais) com o VLibras
   O VLibras é o tradutor oficial e gratuito do governo federal (vlibras.gov.br): um avatar 3D
   traduz textos para Libras. O script oficial (vlibras-plugin.js) é carregado nas duas páginas
   (chat.html e index.html) e cria sozinho o ícone flutuante no lado direito da tela.

   Por que este arquivo mudou: o VLibras 7.x trocou a forma de funcionar. O ícone agora fica num
   Shadow DOM (#vlibras-access-wrapper) e os elementos antigos ([vw-access-button],
   [vw-plugin-wrapper]) não existem mais — o FiscOn procurava por eles, não achava e dizia que o
   tradutor "não carregou". A versão nova expõe uma API:
     window.VLibrasWidget.open()               -> abre o tradutor
     window.vlibras.translateAndPlay(texto)    -> traduz um texto e o avatar sinaliza
   (window.plugin.translate(texto) é o nome antigo da mesma função; usamos o que existir.)

   Atalhos do FiscOn:
     FiscOnLibras.abrir()          -> abre o tradutor (mesmo efeito de clicar no ícone)
     FiscOnLibras.traduzir(balao)  -> abre o tradutor e já traduz o texto daquela resposta
   ========================================================================= */
(function () {
  "use strict";

  const ESPERA_SCRIPT_MS = 10000;     // o script oficial é assíncrono (vem de vlibras.gov.br)
  const ESPERA_AVATAR_MS = 60000;     // na 1ª vez o avatar 3D é baixado (alguns MB; conexão lenta demora)
  const INICIO_SAUDACAO_MS = 1200;    // o widget começa a saudação ~0,5 s depois de carregar o avatar
  const MAX_CHARS = 1500;             // textos muito longos demoram demais para sinalizar

  // Versões antigas do widget (antes da 7.x): botão no DOM normal
  const SELETORES_ANTIGOS = ["[vw-access-button]", "div[vw] .access-button", ".vw-access-button"];

  function widget() {
    const w = window.VLibrasWidget;
    return w && typeof w.open === "function" ? w : null;
  }

  function botaoAntigo() {
    for (const sel of SELETORES_ANTIGOS) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function funcaoTraduzir() {
    const v = window.vlibras;
    if (v && typeof v.translateAndPlay === "function") {
      return (t) => {
        // Para a saudação do avatar (na 1ª abertura) ou a tradução anterior; senão elas têm prioridade.
        if (typeof v.stop === "function") v.stop();
        v.translateAndPlay(t);
      };
    }
    if (window.plugin && typeof window.plugin.translate === "function") return (t) => window.plugin.translate(t);
    return null;
  }

  function aberto() {
    const raiz = document.getElementById("vlibras-app-root");                 // 7.x
    if (raiz && raiz.getAttribute("data-active") === "true") return true;
    const janela = document.querySelector("[vw-plugin-wrapper]");            // versões antigas
    return Boolean(janela && (janela.classList.contains("active") || janela.offsetWidth > 0));
  }

  function esperar(condicao, limiteMs) {
    return new Promise((ok) => {
      const inicio = Date.now();
      (function checar() {
        const r = condicao();
        if (r || Date.now() - inicio > limiteMs) return ok(r || null);
        setTimeout(checar, 200);
      })();
    });
  }

  async function abrir() {
    if (aberto()) return true;
    const pronto = await esperar(() => widget() || botaoAntigo(), ESPERA_SCRIPT_MS);
    if (!pronto) {
      avisarSeguro("O tradutor de Libras (VLibras) não carregou agora. Verifique a internet e tente de novo.", "erro");
      return false;
    }
    if (widget()) widget().open();
    else pronto.click();
    return true;
  }

  // Texto da resposta como a pessoa lê (sem os botões "Ouvir", "Libras", "Copiar"...)
  function textoDoBalao(balao) {
    const copia = balao.cloneNode(true);
    copia.querySelectorAll("button, .ferramentas, .digitando, .status-etapa, svg").forEach((el) => el.remove());
    return (copia.innerText || copia.textContent || "").replace(/\s+/g, " ").trim().slice(0, MAX_CHARS);
  }

  function destacar(balao) {
    balao.classList.remove("libras-alvo");
    void balao.offsetWidth;                        // reinicia a animação
    balao.classList.add("libras-alvo");
    balao.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => balao.classList.remove("libras-alvo"), 7000);
  }

  async function traduzir(balao) {
    const ok = await abrir();
    if (!ok || !balao) return;
    destacar(balao);
    const texto = textoDoBalao(balao);
    if (!texto) return;
    avisarSeguro("Abrindo o tradutor de Libras…");
    // A função de tradução só existe depois que o tradutor abre; o avatar 3D carrega em seguida.
    const jaCarregado = Boolean(window.vlibras && window.vlibras.isLoaded);
    const traduzirTexto = await esperar(funcaoTraduzir, ESPERA_AVATAR_MS);
    if (traduzirTexto && !jaCarregado) {
      // Avatar acabou de carregar: espera a saudação automática começar, para pará-la e não ser
      // atropelado por ela (senão a pessoa vê "Olá, tudo bem" no lugar da resposta).
      await esperar(() => window.vlibras && window.vlibras.isLoaded, ESPERA_AVATAR_MS);
      await new Promise((ok) => setTimeout(ok, INICIO_SAUDACAO_MS));
    }
    if (traduzirTexto) {
      try {
        traduzirTexto(texto);
        avisarSeguro("Traduzindo a resposta para Libras.");
        return;
      } catch (erro) {
        console.warn("[Libras] tradução direta falhou:", erro);
      }
    }
    // Sem a API (versão diferente do widget): com o tradutor aberto, tocar no texto também traduz.
    avisarSeguro("Toque no texto da resposta destacada para vê-la em Libras.");
  }

  function avisarSeguro(texto, tipo) {
    if (typeof avisar === "function") avisar(texto, tipo);
  }

  window.FiscOnLibras = { abrir, traduzir, aberto, disponivel: () => Boolean(widget() || botaoAntigo()) };
})();
