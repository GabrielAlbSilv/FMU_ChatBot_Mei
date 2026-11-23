/* =========================================================================
   FiscOn — voz
   - Falar a pergunta: grava no navegador (MediaRecorder), para sozinho quando a pessoa fica em
     silêncio e envia ao servidor (/api/voz/transcrever -> Groq Whisper). Se o navegador não
     tiver gravador, usa o reconhecimento de fala do próprio navegador (Web Speech API).
   - Ouvir a resposta: síntese de voz do navegador (speechSynthesis), em português do Brasil.
   Carregado depois do script.js (usa campo, enviarPergunta, avisar, ajustarAltura...).
   ========================================================================= */
(function () {
  "use strict";

  /* ---------------- preferências (guardadas neste navegador) ---------------- */
  const CHAVE_PREFS = "fiscon-voz-v1";
  const PADRAO = { lerTodas: false, lerQuandoFalar: true, velocidade: 1 };
  let prefs = { ...PADRAO };
  try { prefs = { ...PADRAO, ...JSON.parse(localStorage.getItem(CHAVE_PREFS) || "{}") }; } catch (_) { /* padrão */ }
  const salvarPrefs = () => { try { localStorage.setItem(CHAVE_PREFS, JSON.stringify(prefs)); } catch (_) { /* ok */ } };

  /* =======================================================================
     OUVIR: leitura em voz alta
     ======================================================================= */
  const sintese = window.speechSynthesis;
  const temLeitura = Boolean(sintese && window.SpeechSynthesisUtterance);
  let vozPt = null;
  let botaoLendo = null;
  let filaFala = 0;   // identifica a leitura atual (cancelar uma leitura não afeta a próxima)

  function escolherVoz() {
    if (!temLeitura) return;
    const vozes = sintese.getVoices().filter((v) => /^pt[-_]BR/i.test(v.lang) || /^pt/i.test(v.lang));
    // prefere vozes naturais/neurais e do Brasil
    const nota = (v) => (/^pt[-_]BR/i.test(v.lang) ? 4 : 0) + (/natural|neural|online/i.test(v.name) ? 3 : 0) +
      (/google/i.test(v.name) ? 2 : 0) + (/francisca|thalita|luciana|antonio|daniel/i.test(v.name) ? 1 : 0);
    vozPt = vozes.sort((a, b) => nota(b) - nota(a))[0] || null;
  }
  if (temLeitura) {
    escolherVoz();
    if ("onvoiceschanged" in sintese) sintese.addEventListener("voiceschanged", escolherVoz);
  }

  // Texto da resposta -> texto bom de ouvir (sem markdown, siglas e endereços falados direito)
  function textoParaFala(texto) {
    return String(texto || "")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")                       // [texto](link) -> texto
      .replace(/https?:\/\//g, "")
      .replace(/\b((?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:gov\.br|com\.br|br))(\/[^\s)]*)?/gi, (m, dominio, caminho) =>
        dominio.replace(/^www\./, "").replace(/\./g, " ponto ") + (caminho ? " barra " + caminho.slice(1).replace(/[/-]/g, " ") : ""))
      .replace(/\*\*|__|`|#+\s/g, "")
      .replace(/^\s*[-*•]\s+/gm, "")
      .replace(/\bDASN-SIMEI\b/gi, "DASN Simei")
      .replace(/\be-CAC\b/gi, "e-Cac")
      .replace(/\bNFS-e\b/gi, "nota fiscal de serviço eletrônica")
      .replace(/\bR\$\s?/g, "R$ ")
      .replace(/\s*\n+\s*/g, ". ")
      .replace(/\.\s*\./g, ".")
      .trim();
  }

  // Divide em frases curtas: o Chrome corta falas longas (> ~15 s) sem avisar.
  function emPartes(texto, max = 200) {
    const frases = texto.match(/[^.!?;:]+[.!?;:]*/g) || [texto];
    const partes = [];
    let atual = "";
    for (const f of frases) {
      if ((atual + f).length > max && atual) { partes.push(atual.trim()); atual = ""; }
      atual += f;
    }
    if (atual.trim()) partes.push(atual.trim());
    return partes;
  }

  function marcarBotao(botao, lendo) {
    if (!botao) return;
    botao.classList.toggle("lendo", lendo);
    botao.innerHTML = lendo ? `${ICONE_PARAR}<span>Parar</span>` : `${ICONE_OUVIR}<span>Ouvir</span>`;
    botao.title = lendo ? "Parar a leitura" : "Ouvir a resposta em voz alta";
  }

  function pararFala() {
    filaFala++;
    if (temLeitura) sintese.cancel();
    marcarBotao(botaoLendo, false);
    botaoLendo = null;
  }

  function falar(texto, botao) {
    if (!temLeitura) { avisar("Este navegador não tem leitura em voz alta.", "erro"); return; }
    pararFala();
    const partes = emPartes(textoParaFala(texto));
    if (!partes.length) return;
    const minha = ++filaFala;
    botaoLendo = botao || null;
    marcarBotao(botaoLendo, true);
    partes.forEach((parte, i) => {
      const u = new SpeechSynthesisUtterance(parte);
      u.lang = "pt-BR";
      if (vozPt) u.voice = vozPt;
      u.rate = Number(prefs.velocidade) || 1;
      if (i === partes.length - 1) {
        u.onend = u.onerror = () => { if (minha === filaFala) { marcarBotao(botaoLendo, false); botaoLendo = null; } };
      }
      sintese.speak(u);
    });
  }

  function alternarLeitura(texto, botao) {
    if (botao && botao === botaoLendo) { pararFala(); return; }
    falar(texto, botao);
  }

  // Chamado pelo script.js quando uma resposta nova termina de aparecer
  function aoResponder(dados, partes, porVoz) {
    if (!temLeitura || !dados || !dados.texto) return;
    if (prefs.lerTodas || (porVoz && prefs.lerQuandoFalar)) {
      falar(dados.texto, partes && partes.corpo.querySelector(".botao-ouvir"));
    }
  }

  /* =======================================================================
     FALAR: microfone
     ======================================================================= */
  const botaoMic = document.getElementById("microfone");
  const caixaEntrada = document.getElementById("formulario");
  const Reconhecimento = window.SpeechRecognition || window.webkitSpeechRecognition;
  const temGravador = Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  const TIPOS_GRAVACAO = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg"];
  const SILENCIO_MS = 1600;      // para sozinho depois desse silêncio (quando já houve fala)
  const SEM_FALA_MS = 8000;      // desiste se ninguém falar
  const MAX_MS = 60000;          // limite de uma pergunta falada

  let estadoMic = "parado";      // parado | gravando | transcrevendo
  let perguntaPorVoz = false;
  let gravacao = null;           // {recorder, stream, ctx, intervalo, partes, tipo, inicio, cancelada}
  let reconhecimento = null;
  const placeholderOriginal = campo.getAttribute("placeholder");

  function definirEstadoMic(novo, texto) {
    estadoMic = novo;
    if (!botaoMic) return;
    botaoMic.classList.toggle("gravando", novo === "gravando");
    botaoMic.classList.toggle("transcrevendo", novo === "transcrevendo");
    caixaEntrada.classList.toggle("ouvindo", novo === "gravando");
    botaoMic.setAttribute("aria-pressed", String(novo === "gravando"));
    botaoMic.title = novo === "gravando" ? "Parar e enviar" : novo === "transcrevendo" ? "Transcrevendo…" : "Falar a pergunta";
    botaoMic.setAttribute("aria-label", botaoMic.title);
    botaoMic.disabled = novo === "transcrevendo";
    campo.placeholder = texto || placeholderOriginal;
    campo.readOnly = novo !== "parado";
    if (novo !== "gravando") botaoMic.style.removeProperty("--nivel");
  }

  function tempoFormatado(ms) {
    const s = Math.floor(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  async function iniciarGravacao() {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (erro) {
      const negado = erro && (erro.name === "NotAllowedError" || erro.name === "SecurityError");
      avisar(negado ? "Permita o uso do microfone no navegador para falar com o FiscOn." : "Não encontrei um microfone neste dispositivo.", "erro");
      return;
    }
    const tipo = TIPOS_GRAVACAO.find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
    let recorder;
    try {
      recorder = tipo ? new MediaRecorder(stream, { mimeType: tipo }) : new MediaRecorder(stream);
    } catch (_) {
      stream.getTracks().forEach((t) => t.stop());
      return iniciarReconhecimento();
    }
    gravacao = { recorder, stream, partes: [], tipo: recorder.mimeType || tipo || "audio/webm", inicio: Date.now(), cancelada: false };
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) gravacao.partes.push(e.data); };
    recorder.onstop = finalizarGravacao;
    recorder.start(250);
    pararFala();
    definirEstadoMic("gravando", "Ouvindo… fale a sua pergunta");
    monitorarSilencio();
  }

  // Mede o volume para: animar o botão, parar sozinho no silêncio e desistir se ninguém falar.
  function monitorarSilencio() {
    const g = gravacao;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    let analisador = null;
    let amostra = null;
    if (Ctx) {
      try {
        g.ctx = new Ctx();
        const fonte = g.ctx.createMediaStreamSource(g.stream);
        analisador = g.ctx.createAnalyser();
        analisador.fftSize = 1024;
        fonte.connect(analisador);
        amostra = new Uint8Array(analisador.fftSize);
      } catch (_) { analisador = null; }
    }
    let falou = false;
    let ultimoSom = Date.now();
    let ruido = 0;
    let medicoes = 0;
    g.intervalo = setInterval(() => {
      const agora = Date.now();
      const decorrido = agora - g.inicio;
      campo.placeholder = `Ouvindo… ${tempoFormatado(decorrido)} — toque no microfone para enviar`;
      if (analisador) {
        analisador.getByteTimeDomainData(amostra);
        let soma = 0;
        for (let i = 0; i < amostra.length; i++) { const v = (amostra[i] - 128) / 128; soma += v * v; }
        const rms = Math.sqrt(soma / amostra.length);
        if (medicoes < 4) { ruido = Math.max(ruido, rms); medicoes++; }        // ruído do ambiente (0,4 s)
        const limiar = Math.max(0.02, ruido * 2.2);
        botaoMic.style.setProperty("--nivel", Math.min(1, rms / 0.25).toFixed(2));
        if (rms > limiar) { falou = true; ultimoSom = agora; }
        if (falou && agora - ultimoSom > SILENCIO_MS) return pararGravacao(true);
        if (!falou && decorrido > SEM_FALA_MS) {
          avisar("Não ouvi nada. Toque no microfone e fale a sua pergunta.");
          return pararGravacao(false);
        }
      }
      if (decorrido > MAX_MS) pararGravacao(true);
    }, 100);
  }

  function pararGravacao(enviar) {
    const g = gravacao;
    if (!g) return;
    g.cancelada = !enviar;
    clearInterval(g.intervalo);
    try { if (g.recorder.state !== "inactive") g.recorder.stop(); } catch (_) { finalizarGravacao(); }
  }

  async function finalizarGravacao() {
    const g = gravacao;
    gravacao = null;
    if (!g) return;
    g.stream.getTracks().forEach((t) => t.stop());
    if (g.ctx) g.ctx.close().catch(() => {});
    if (g.cancelada || !g.partes.length) { definirEstadoMic("parado"); return; }
    const blob = new Blob(g.partes, { type: g.tipo });
    await enviarAudio(blob, g.tipo);
  }

  async function enviarAudio(blob, tipo) {
    definirEstadoMic("transcrevendo", "Transcrevendo o áudio…");
    const ext = /ogg/.test(tipo) ? "ogg" : /mp4|m4a|aac/.test(tipo) ? "m4a" : /wav/.test(tipo) ? "wav" : "webm";
    const dados = new FormData();
    dados.append("audio", blob, `pergunta.${ext}`);
    let texto = "";
    try {
      const r = await fetch("/api/voz/transcrever", { method: "POST", body: dados });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) avisar(d.erro || "Não foi possível transcrever o áudio.", "erro");
      texto = (d.texto || "").trim();
    } catch (_) {
      avisar("Sem conexão com o servidor para transcrever o áudio.", "erro");
    }
    definirEstadoMic("parado");
    if (texto) enviarTextoFalado(texto);
  }

  // Mostra o que foi entendido na caixa de texto por um instante e envia.
  function enviarTextoFalado(texto) {
    campo.value = texto;
    ajustarAltura();
    atualizarBotoes();
    perguntaPorVoz = true;
    setTimeout(() => {
      if (campo.value.trim() === texto) enviarPergunta(texto);
      else perguntaPorVoz = false;      // a pessoa mexeu no texto: ela mesma envia
    }, 450);
  }

  /* ---- alternativa: reconhecimento de fala do próprio navegador (Web Speech API) ---- */
  function iniciarReconhecimento() {
    if (!Reconhecimento) { avisar("Este navegador não permite falar com o FiscOn. Digite a sua pergunta.", "erro"); return; }
    reconhecimento = new Reconhecimento();
    reconhecimento.lang = "pt-BR";
    reconhecimento.interimResults = true;
    reconhecimento.continuous = false;
    let final = "";
    reconhecimento.onresult = (e) => {
      let parcial = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) final += e.results[i][0].transcript;
        else parcial += e.results[i][0].transcript;
      }
      campo.value = (final + " " + parcial).trim();
      ajustarAltura();
    };
    reconhecimento.onerror = (e) => {
      if (e.error === "not-allowed") avisar("Permita o uso do microfone no navegador para falar com o FiscOn.", "erro");
      else if (e.error === "no-speech") avisar("Não ouvi nada. Toque no microfone e fale a sua pergunta.");
    };
    reconhecimento.onend = () => {
      reconhecimento = null;
      definirEstadoMic("parado");
      const texto = (final || campo.value).trim();
      if (texto) enviarTextoFalado(texto);
    };
    pararFala();
    definirEstadoMic("gravando", "Ouvindo… fale a sua pergunta");
    try { reconhecimento.start(); } catch (_) { definirEstadoMic("parado"); }
  }

  if (botaoMic) {
    if (!temGravador && !Reconhecimento) {
      botaoMic.hidden = true;           // navegador sem nenhuma forma de ouvir
    } else {
      botaoMic.hidden = false;
      botaoMic.addEventListener("click", () => {
        if (estadoMic === "gravando") {
          if (gravacao) pararGravacao(true);
          else if (reconhecimento) reconhecimento.stop();
          return;
        }
        if (estadoMic !== "parado" || enviando || !pronto) return;
        if (temGravador) iniciarGravacao();
        else iniciarReconhecimento();
      });
      // Esc cancela a gravação sem enviar
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && estadoMic === "gravando") {
          if (gravacao) pararGravacao(false);
          else if (reconhecimento) reconhecimento.abort();
        }
      });
    }
  }

  /* =======================================================================
     Menu de acessibilidade (voz + Libras)
     ======================================================================= */
  const botaoMenu = document.getElementById("botao-acessibilidade");
  const painel = document.getElementById("painel-acessibilidade");

  function abrirPainel(abrir) {
    if (!painel || !botaoMenu) return;
    painel.hidden = !abrir;
    botaoMenu.setAttribute("aria-expanded", String(abrir));
    if (abrir) (painel.querySelector("input, button") || painel).focus();
  }

  if (painel && botaoMenu) {
    const lerTodas = painel.querySelector("#pref-ler-todas");
    const lerFalar = painel.querySelector("#pref-ler-falar");
    lerTodas.checked = Boolean(prefs.lerTodas);
    lerFalar.checked = Boolean(prefs.lerQuandoFalar);
    lerTodas.addEventListener("change", () => { prefs.lerTodas = lerTodas.checked; salvarPrefs(); if (!lerTodas.checked) pararFala(); });
    lerFalar.addEventListener("change", () => { prefs.lerQuandoFalar = lerFalar.checked; salvarPrefs(); });
    painel.querySelectorAll("[data-velocidade]").forEach((b) => {
      b.setAttribute("aria-pressed", String(Number(b.dataset.velocidade) === Number(prefs.velocidade)));
      b.addEventListener("click", () => {
        prefs.velocidade = Number(b.dataset.velocidade);
        salvarPrefs();
        painel.querySelectorAll("[data-velocidade]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        falar("Esta é a velocidade da voz do FiscOn.", null);
      });
    });
    const testar = painel.querySelector("#testar-voz");
    if (testar) testar.addEventListener("click", () => falar("Olá! Eu sou o FiscOn. Posso tirar suas dúvidas sobre MEI e Receita Federal.", null));
    if (!temLeitura) painel.classList.add("sem-leitura");
    if (!temGravador && !Reconhecimento) painel.classList.add("sem-microfone");

    botaoMenu.addEventListener("click", (e) => { e.stopPropagation(); abrirPainel(painel.hidden); });
    document.addEventListener("click", (e) => { if (!painel.hidden && !painel.contains(e.target) && e.target !== botaoMenu) abrirPainel(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !painel.hidden) { abrirPainel(false); botaoMenu.focus(); } });
    const abrirLibras = painel.querySelector("#abrir-libras");
    if (abrirLibras) abrirLibras.addEventListener("click", () => { abrirPainel(false); if (window.FiscOnLibras) window.FiscOnLibras.abrir(); });
  }

  window.addEventListener("pagehide", pararFala);

  window.FiscOnVoz = {
    temLeitura,
    falar,
    pararFala,
    alternarLeitura,
    aoResponder,
    consumirPerguntaPorVoz() { const v = perguntaPorVoz; perguntaPorVoz = false; return v; },
    _textoParaFala: textoParaFala,     // exposto para testes
  };
})();
