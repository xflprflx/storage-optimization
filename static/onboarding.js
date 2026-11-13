/* Onboarding integrado: tours de cada tela (catálogo em /onboarding/tours.json, services/onboarding.py).
   - Começa pelo menu "Guia" da barra (Tour desta tela) ou por um link com #nk-tour=<id> (o guia passo a passo usa).
   - Cada etapa destaca o 1º elemento visível entre os seletores; etapas sem elemento na tela são puladas. Se o
     elemento estiver numa etapa escondida de um passo a passo (data-stepper), a etapa é aberta.
   - Teclado: → / Enter avança, ← volta, Esc fecha. O tour visto fica marcado neste navegador (nk-tour-seen:<id>).
   window.nkTour = { start(id), here(), seen(id), list() } */
(function () {
  const SEEN = 'nk-tour-seen:', RETURN = 'nk-onb-return';
  let tours = null, loading = null, cur = null;
  const store = {
    get: (k, s) => { try { return (s ? sessionStorage : localStorage).getItem(k); } catch (e) { return null; } },
    set: (k, v, s) => { try { (s ? sessionStorage : localStorage).setItem(k, v); } catch (e) { /* sem armazenamento */ } },
    del: (k, s) => { try { (s ? sessionStorage : localStorage).removeItem(k); } catch (e) { /* idem */ } },
  };
  function load() {
    if (tours) return Promise.resolve(tours);
    if (!loading) {
      loading = fetch('/onboarding/tours.json', { credentials: 'same-origin' }).then(r => r.json())
        .then(d => (tours = d)).catch(() => (tours = []));
    }
    return loading;
  }
  const forPath = (list, path) => list.find(t => new RegExp(t.p).test(path));
  const visible = el => {
    if (!el || el.closest('.nk-tour-card')) return false;
    if (el.closest('.nk-step-hidden')) return true;  // a etapa do passo a passo é aberta ao chegar nela
    if (el.closest('.navbar-collapse:not(.show)') && getComputedStyle(el.closest('.navbar-collapse')).display === 'none') return true;  // menu recolhido (celular)
    const r = el.getBoundingClientRect();
    return (r.width > 0 || r.height > 0) && getComputedStyle(el).visibility !== 'hidden';
  };
  const pick = sels => {
    for (const s of sels || []) {
      for (const el of document.querySelectorAll(s)) if (visible(el)) return el;
    }
    return null;
  };
  function reveal(el) {
    const hidden = el.closest('.nk-step-hidden');
    if (hidden) {
      const root = hidden.closest('[data-stepper]');
      if (root && root.nkShowStep) root.nkShowStep(hidden);
    }
    // celular: o item está no menu recolhido da barra; abre o menu enquanto o tour durar
    const nav = el.closest('.navbar-collapse');
    document.querySelectorAll('.navbar-collapse.nk-tour-open').forEach(n => { if (n !== nav) n.classList.remove('show', 'nk-tour-open'); });
    if (nav && !nav.classList.contains('show') && getComputedStyle(nav).display === 'none') nav.classList.add('show', 'nk-tour-open');
    // campos dentro de um rótulo/form-check: destaca o bloco inteiro do campo
    const box = el.matches('input[type=checkbox], input[type=radio]') ? (el.closest('.form-check') || el) : el;
    return box;
  }

  function build() {
    const hole = document.createElement('div');
    hole.className = 'nk-tour-hole';
    const card = document.createElement('div');
    card.className = 'nk-tour-card';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-live', 'polite');
    card.innerHTML = '<div class="d-flex justify-content-between align-items-start gap-2">' +
      '<div class="nk-tour-t"></div><button type="button" class="btn-close btn-sm nk-tour-x"></button></div>' +
      '<div class="nk-tour-d"></div>' +
      '<div class="d-flex justify-content-between align-items-center gap-2 mt-2">' +
      '<span class="nk-tour-n small text-muted"></span><div class="d-flex gap-1">' +
      '<button type="button" class="btn btn-sm btn-outline-secondary nk-tour-prev"></button>' +
      '<button type="button" class="btn btn-sm btn-primary nk-tour-next"></button></div></div>';
    card.querySelector('.nk-tour-x').setAttribute('aria-label', _t('Fechar'));
    card.querySelector('.nk-tour-prev').innerHTML = '<i class="bi bi-arrow-left"><\/i> ' + _t('Anterior');
    card.querySelector('.nk-tour-x').addEventListener('click', () => stop(false));
    card.querySelector('.nk-tour-prev').addEventListener('click', () => go(cur.i - 1, -1));
    card.querySelector('.nk-tour-next').addEventListener('click', () => go(cur.i + 1, 1));
    document.body.append(hole, card);
    return { hole, card };
  }

  function place() {
    if (!cur) return;
    const { hole, card } = cur.ui, el = cur.el, pad = 6, gap = 12;
    const vw = window.innerWidth, vh = window.innerHeight;
    if (!el) {  // sem elemento: cartão no meio da tela, fundo escurecido
      hole.style.cssText = 'left:50%;top:50%;width:0;height:0';
      card.style.left = Math.max(8, (vw - card.offsetWidth) / 2) + 'px';
      card.style.top = Math.max(8, (vh - card.offsetHeight) / 2) + 'px';
      return;
    }
    const r = el.getBoundingClientRect();
    hole.style.cssText = `left:${r.left - pad}px;top:${r.top - pad}px;width:${r.width + 2 * pad}px;height:${r.height + 2 * pad}px`;
    const cw = card.offsetWidth, ch = card.offsetHeight;
    let top = r.bottom + pad + gap, left = Math.min(vw - cw - 8, Math.max(8, r.left + r.width / 2 - cw / 2));
    if (top + ch > vh - 8) top = r.top - pad - gap - ch;  // sem espaço embaixo: em cima
    if (top < 8) {  // elemento alto: ao lado dele, se couber; senão, por cima
      top = Math.min(vh - ch - 8, Math.max(8, r.top + 8));
      if (r.right + pad + gap + cw <= vw - 8) left = r.right + pad + gap;
      else if (r.left - pad - gap - cw >= 8) left = r.left - pad - gap - cw;
    }
    card.style.left = left + 'px';
    card.style.top = top + 'px';
  }

  function go(i, dir) {
    if (!cur) return;
    const steps = cur.tour.s;
    // pula as etapas cujo elemento não está nesta tela
    while (i >= 0 && i < steps.length && steps[i].s.length && !pick(steps[i].s)) i += dir || 1;
    if (i >= steps.length) return stop(true);
    if (i < 0) return;
    cur.i = i;
    const st = steps[i];
    cur.el = st.s.length ? reveal(pick(st.s)) : null;
    const { card, hole } = cur.ui;
    card.querySelector('.nk-tour-t').textContent = st.t;
    card.querySelector('.nk-tour-d').textContent = st.d;
    const shown = steps.filter(x => !x.s.length || pick(x.s));
    card.querySelector('.nk-tour-n').textContent = _t('{i} de {n}', { i: shown.indexOf(st) + 1, n: shown.length });
    const prev = card.querySelector('.nk-tour-prev'), next = card.querySelector('.nk-tour-next');
    let hasPrev = false;
    for (let j = i - 1; j >= 0; j--) if (!steps[j].s.length || pick(steps[j].s)) { hasPrev = true; break; }
    prev.style.visibility = hasPrev ? '' : 'hidden';
    let last = true;
    for (let j = i + 1; j < steps.length; j++) if (!steps[j].s.length || pick(steps[j].s)) { last = false; break; }
    next.innerHTML = last ? '<i class="bi bi-check-lg"><\/i> ' + _t('Concluir') : _t('Próximo') + ' <i class="bi bi-arrow-right"><\/i>';
    hole.classList.toggle('nk-tour-center', !cur.el);
    if (cur.el) {
      const r = cur.el.getBoundingClientRect();
      if (r.top < 70 || r.bottom > window.innerHeight - 40) {
        if (r.height > window.innerHeight - 140) window.scrollBy({ top: r.top - 80, behavior: 'instant' });  // alto: mostra o início
        else cur.el.scrollIntoView({ block: 'center', behavior: 'instant' });
      }
    }
    place();
    next.focus({ preventScroll: true });
  }

  function key(e) {
    if (!cur) return;
    if (e.key === 'Escape') { e.preventDefault(); stop(false); }
    else if (e.key === 'ArrowRight' || (e.key === 'Enter' && e.target.closest('.nk-tour-card'))) { e.preventDefault(); go(cur.i + 1, 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(cur.i - 1, -1); }
  }

  function stop(finished) {
    if (!cur) return;
    const id = cur.tour.id;
    cur.ui.hole.remove(); cur.ui.card.remove();
    document.querySelectorAll('.navbar-collapse.nk-tour-open').forEach(n => n.classList.remove('show', 'nk-tour-open'));
    window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true);
    document.removeEventListener('keydown', key, true);
    cur = null;
    store.set(SEEN + id, finished ? 'done' : 'seen');
    document.dispatchEvent(new CustomEvent('nk-tour-end', { detail: { id, finished } }));
    markDot();
    if (finished && store.get(RETURN, true)) returnBanner();
  }

  /* veio do guia passo a passo: oferece voltar para a próxima etapa */
  function returnBanner() {
    if (document.querySelector('.nk-tour-return')) return;
    const b = document.createElement('div');
    b.className = 'nk-tour-return alert alert-info shadow d-flex align-items-center gap-2 py-2 mb-0';
    b.innerHTML = '<i class="bi bi-signpost-split"><\/i><span></span><a class="btn btn-sm btn-primary ms-2" href="/onboarding"></a>' +
      '<button type="button" class="btn-close btn-sm ms-1"></button>';
    b.querySelector('span').textContent = _t('Quando terminar nesta tela, volte ao guia para a próxima etapa.');
    b.querySelector('a').textContent = _t('Voltar ao guia');
    b.querySelector('button').setAttribute('aria-label', _t('Fechar'));
    b.querySelector('button').addEventListener('click', () => { store.del(RETURN, true); b.remove(); });
    document.body.appendChild(b);
  }

  function start(id) {
    return load().then(list => {
      const tour = typeof id === 'string' ? list.find(t => t.id === id) : id;
      if (!tour) return false;
      if (cur) stop(false);
      if (!tour.s.some(st => !st.s.length || pick(st.s))) return false;
      cur = { tour, i: 0, el: null, ui: build() };
      window.addEventListener('resize', place); window.addEventListener('scroll', place, true);
      document.addEventListener('keydown', key, true);
      go(0, 1);
      return true;
    });
  }
  const here = () => load().then(list => {
    const t = forPath(list, location.pathname);
    return t ? start(t) : false;
  });
  const seen = id => !!store.get(SEEN + id);

  /* ponto no botão Guia quando esta tela tem tour ainda não visto; o item "Tour desta tela" some quando não há */
  function markDot() {
    load().then(list => {
      const t = forPath(list, location.pathname);
      const item = document.getElementById('nkTourHere');
      if (item) {
        item.classList.toggle('disabled', !t);
        item.querySelector('.nk-tour-name').textContent = t ? t.t : _t('esta tela não tem tour');
      }
      const dot = document.getElementById('nkGuideDot');
      if (dot) dot.hidden = !t || seen(t.id);
    });
  }

  window.nkTour = { start, here, seen, list: load };
  document.addEventListener('DOMContentLoaded', () => {
    const here_ = document.getElementById('nkTourHere');
    if (here_) here_.addEventListener('click', e => { e.preventDefault(); here(); });
    markDot();
    if (!fromHash(250) && store.get(RETURN, true) && !location.pathname.startsWith('/onboarding')) returnBanner();
    if (location.pathname.startsWith('/onboarding')) store.del(RETURN, true);
  });
  // link #nk-tour=<id> na própria tela (ex.: "Tour rápido" no Painel): só o endereço muda, sem recarregar
  window.addEventListener('hashchange', () => fromHash(0));

  function fromHash(delay) {
    const m = /(?:^#|&)nk-tour=([\w-]+)/.exec(location.hash);
    if (!m) return false;
    history.replaceState(null, '', location.pathname + location.search);
    if (document.referrer.includes('/onboarding')) store.set(RETURN, '1', true);
    // espera o layout assentar (passo a passo, tabelas e desenhos montados pelo JavaScript)
    setTimeout(() => start(m[1]).then(ok => { if (!ok) here(); }), delay);
    return true;
  }
})();
