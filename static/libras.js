/* =========================================================================
   FiscOn — Libras (Língua Brasileira de Sinais) com o VLibras
   O VLibras é o tradutor oficial e gratuito do governo federal (vlibras.gov.br): um avatar 3D
   traduz para Libras o texto que a pessoa toca/clica na página. O script oficial é carregado nas
   duas páginas (ver chat.html e index.html) e cria o ícone flutuante no lado direito da tela.

   Aqui ficam os atalhos do FiscOn:
     FiscOnLibras.abrir()          -> abre o tradutor (mesmo efeito de clicar no ícone)
     FiscOnLibras.traduzir(balao)  -> abre o tradutor e destaca a resposta para a pessoa tocar nela
   O VLibras não oferece um comando oficial para "traduzir este texto"; a tradução começa quando a
   pessoa toca no texto com o tradutor aberto (é assim que o widget funciona em qualquer site).
   ========================================================================= */
(function () {
  "use strict";

  // Possíveis seletores do botão flutuante (mudaram entre versões do widget)
  const SELETORES_BOTAO = ["[vw-access-button]", "div[vw] .access-button", ".vw-access-button", "#vlibras-access-button"];

  function botaoVLibras() {
    for (const sel of SELETORES_BOTAO) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function aberto() {
    const janela = document.querySelector("[vw-plugin-wrapper]");
    return Boolean(janela && (janela.classList.contains("active") || janela.offsetWidth > 0));
  }

  // Espera o widget terminar de carregar (o script oficial é assíncrono e vem de vlibras.gov.br)
  function esperarBotao(limiteMs = 6000) {
    return new Promise((ok) => {
      const inicio = Date.now();
      (function checar() {
        const b = botaoVLibras();
        if (b || Date.now() - inicio > limiteMs) return ok(b);
        setTimeout(checar, 200);
      })();
    });
  }

  async function abrir() {
    if (aberto()) return true;
    const b = await esperarBotao();
    if (!b) {
      avisarSeguro("O tradutor de Libras (VLibras) não carregou agora. Verifique a internet e tente de novo.", "erro");
      return false;
    }
    b.click();
    return true;
  }

  async function traduzir(balao) {
    const ok = await abrir();
    if (!ok || !balao) return;
    // destaca a resposta: é nela que a pessoa toca para ver a tradução
    balao.classList.remove("libras-alvo");
    void balao.offsetWidth;                        // reinicia a animação
    balao.classList.add("libras-alvo");
    balao.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => balao.classList.remove("libras-alvo"), 7000);
    avisarSeguro("Toque no texto da resposta destacada para vê-la em Libras.");
  }

  function avisarSeguro(texto, tipo) {
    if (typeof avisar === "function") avisar(texto, tipo);
  }

  window.FiscOnLibras = { abrir, traduzir, disponivel: () => Boolean(botaoVLibras()) };
})();
