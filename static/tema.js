/* =========================================================================
   Tema claro/escuro
   - Sem escolha: segue o navegador/sistema (automático).
   - Botão sol/lua: troca o tema e lembra a escolha neste navegador.
   - Se a escolha for igual ao tema do sistema, volta ao automático.
   (A escolha salva é aplicada logo no <head> de cada página, para não piscar.)
   ========================================================================= */
(function () {
  const CHAVE = "chatbot-mei-tema";
  const raiz = document.documentElement;
  const sistemaEscuro = window.matchMedia("(prefers-color-scheme: dark)");

  function temaAtual() {
    const escolhido = raiz.getAttribute("data-theme");
    if (escolhido === "dark" || escolhido === "light") return escolhido;
    return sistemaEscuro.matches ? "dark" : "light";
  }

  function atualizarBotoes() {
    const atual = temaAtual();
    raiz.setAttribute("data-tema-atual", atual);
    const rotulo = atual === "dark" ? "Mudar para o tema claro" : "Mudar para o tema escuro";
    document.querySelectorAll(".botao-tema").forEach((b) => {
      b.setAttribute("aria-label", rotulo);
      b.title = rotulo;
    });
    // Cor da barra do navegador no celular
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", atual === "dark" ? "#1d181a" : "#c8102e");
  }

  function salvar(valor) {
    try {
      if (valor) localStorage.setItem(CHAVE, valor);
      else localStorage.removeItem(CHAVE);
    } catch (_) { /* armazenamento bloqueado: vale só nesta página */ }
  }

  function alternar() {
    const novo = temaAtual() === "dark" ? "light" : "dark";
    // Transição suave de cores só durante a troca
    raiz.classList.add("trocando-tema");
    setTimeout(() => raiz.classList.remove("trocando-tema"), 400);

    const doSistema = sistemaEscuro.matches ? "dark" : "light";
    if (novo === doSistema) {
      raiz.removeAttribute("data-theme");   // igual ao sistema: volta ao automático
      salvar(null);
    } else {
      raiz.setAttribute("data-theme", novo);
      salvar(novo);
    }
    atualizarBotoes();
  }

  document.querySelectorAll(".botao-tema").forEach((b) => b.addEventListener("click", alternar));
  // Se o sistema mudar de tema (ex.: modo noturno automático), o ícone acompanha.
  if (sistemaEscuro.addEventListener) sistemaEscuro.addEventListener("change", atualizarBotoes);
  atualizarBotoes();
})();
