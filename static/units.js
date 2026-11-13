/* Unidades de medida no navegador (sistema imperial: in, ft, lb...). Os valores são sempre guardados no
   sistema métrico; no imperial, este arquivo:
   * converte os campos de medida ao abrir a tela e de volta ao enviar o formulário. O campo indica a
     unidade métrica em data-u="cm" (ou pelo final do nome: x_m, gap_cm, area_m2, peso_kg);
   * converte as medidas dos textos montados no JavaScript (_t passa por nkU.text);
   * oferece nkU.get(campo) / nkU.set(campo, valorMétrico) para os scripts que leem e escrevem os campos.
   No métrico nada muda (nkU.get só lê o número no formato do idioma). */
(function () {
  const I = window.NK_I18N || { units: 'metric', dec: ',', th: '.', locale: 'pt-BR' };
  const ON = I.units === 'imperial';
  // unidade métrica -> [imperial, fator (métrico = imperial × fator), casas ao exibir]
  const MAP = {
    'cm': ['in', 2.54, 1], 'm': ['ft', 0.3048, 1], 'km': ['mi', 1.609344, 2], 'kg': ['lb', 0.45359237, 1],
    'm²': ['ft²', 0.09290304, 0], 'm2': ['ft²', 0.09290304, 0], 'cm²': ['in²', 6.4516, 1],
    'm³': ['ft³', 0.028316846592, 1], 'cm³': ['in³', 16.387064, 0], 'L': ['ft³', 28.316846592, 2],
    'm/s': ['ft/s', 0.3048, 1], 'm/min': ['ft/min', 0.3048, 0],
    'kg/m²': ['lb/ft²', 4.882427636, 1], 'kg/m³': ['lb/ft³', 16.01846337, 1],
  };
  // nos textos soltos "50 L" fica (aparece em nomes); o rótulo "(L)" troca
  const TEXT_UNITS = ['kg/m²', 'kg/m³', 'm/min', 'm/s', 'cm²', 'cm³', 'm²', 'm³', 'cm', 'km', 'kg', 'm'];
  const reEsc = s => s.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');
  const UNIT_ALT = TEXT_UNITS.map(reEsc).join('|');
  const TH = I.th === ' ' ? '[ \\u202f\\u00a0]' : reEsc(I.th);
  const DEC = reEsc(I.dec);
  const NUM = `-?\\d{1,3}(?:${TH}\\d{3})+(?:${DEC}\\d+)?|-?\\d+(?:${DEC}\\d+)?`;
  const SEP = '\\s*[–×-]\\s*|\\s+(?:a|to|à|bis|y|et|e|and|und|or|ou|o|oder)\\s+|\\s*到\\s*';
  const CHAIN = new RegExp(`(?<![\\w.,/])((?:${NUM})(?:(?:${SEP})(?:${NUM}))*)(\\s?)(${UNIT_ALT})(?![A-Za-zÀ-ÿ²³/\\d])`, 'g');
  const NUM_G = new RegExp(NUM, 'g');
  const LABEL = new RegExp(`\\((${UNIT_ALT}|L)(?=[\\s,;)])`, 'g');

  // texto no formato do idioma -> número (aceita também o ponto decimal)
  function parse(v) {
    v = (v == null ? '' : String(v)).trim();
    if (!v) return null;
    if (I.th === ' ') v = v.replace(/[   ]/g, '');
    if (I.dec === ',') { if (v.includes(',')) v = v.replace(/\./g, '').replace(',', '.'); }
    else v = v.replace(/,/g, '');
    const n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  const fmtIn = (n, d) => (n == null || isNaN(n)) ? '' :
    String(Math.round(n * 10 ** d) / 10 ** d).replace('.', I.dec);
  const fmtTxt = (n, d) => nkNum(Math.round(n * 10 ** d) / 10 ** d, 0, d);

  function text(s) {
    if (!ON || s == null) return s;
    s = String(s).replace(CHAIN, (m, nums, sp, unit) => {
      const [imp, f, d0] = MAP[unit];
      return nums.replace(NUM_G, n => {
        const v = parse(n) / f;
        const d = (Math.abs(v) < 1 && v !== 0) ? d0 + 1 : d0;
        return fmtTxt(v, d);
      }) + sp + imp;
    });
    return s.replace(LABEL, (m, u) => '(' + MAP[u][0]);
  }

  // unidade de um campo: data-u, ou o fim do nome (z_x_m_3, p_side_gap_cm, sec_area_m2_1, weight_kg)
  function unitOf(el) {
    if (!el) return null;
    if (el.dataset && el.dataset.u) return el.dataset.u === 'none' ? null : el.dataset.u;
    const m = /_(m2|m|cm|kg)(?:_|$)/.exec(el.name || '');
    return m ? m[1] : null;
  }
  // lista de números ("45; 40; 35") ou um número
  function convertValue(str, unit, toMetric) {
    const [, f, d0] = MAP[unit];
    return String(str).split(/(\s*;\s*)/).map(part => {
      if (/^\s*;\s*$/.test(part) || !part.trim()) return part;
      const n = parse(part);
      if (n == null) return part;
      return toMetric ? fmtIn(n * f, 4).replace(I.dec, '.') : fmtIn(n / f, d0 + 1);
    }).join('');
  }
  // tela aberta: métrico -> imperial (guarda o original para devolver exato se não for alterado)
  function show(el) {
    if (!ON || el.dataset.nkU) return;
    const u = unitOf(el);
    if (!u || !MAP[u]) return;
    el.dataset.nkU = '1';
    el.dataset.nkM = el.value;
    el.value = el.value === '' ? '' : convertValue(el.value, u, false);
    el.dataset.nkD = el.value;
    if (el.type === 'number') { el.step = 'any'; el.removeAttribute('min'); el.removeAttribute('max'); }
    if (el.placeholder) el.placeholder = text(el.placeholder);
  }
  // valor métrico (texto) que vai para o servidor
  function metricValue(el) {
    const u = unitOf(el);
    if (!ON || !u || !MAP[u] || !el.dataset.nkU) return el.value;
    if (el.value === el.dataset.nkD) return el.dataset.nkM;
    return el.value === '' ? '' : convertValue(el.value, u, true);
  }
  function scan(root) {
    if (!ON) return;
    (root || document).querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea')
      .forEach(el => { if (unitOf(el)) show(el); });
  }
  // envio: troca os campos pelo valor métrico e, se a página continuar aberta, volta a exibir no imperial
  function toMetric(form) {
    if (!ON || !form || !form.querySelectorAll) return;
    form.querySelectorAll('[data-nk-u]').forEach(el => {
      const mv = metricValue(el);
      el.dataset.nkU = '';
      el.value = mv;
    });
    setTimeout(() => scan(form), 0);
  }
  if (ON) {
    window.addEventListener('submit', e => toMetric(e.target), true);
    const nativeSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function () { toMetric(this); return nativeSubmit.call(this); };
    document.addEventListener('DOMContentLoaded', () => {
      scan(document);
      // linhas novas (modelos clonados) e conteúdo carregado depois
      new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => { if (n.nodeType === 1) scan(n.parentNode || n); })))
        .observe(document.body, { childList: true, subtree: true });
    });
  }

  window.nkU = {
    on: ON,
    unit: u => (ON && MAP[u] ? MAP[u][0] : u),
    // métrico -> sistema exibido (número)
    toDisp: (v, u) => (v == null || !ON || !MAP[u]) ? v : v / MAP[u][1],
    // sistema exibido -> métrico (número)
    toMetric: (v, u) => (v == null || !ON || !MAP[u]) ? v : v * MAP[u][1],
    parse,
    text,
    // número métrico de um campo (lê o que está na tela e converte se for imperial)
    get(el) {
      if (!el) return null;
      const n = parse(el.value);
      const u = unitOf(el);
      return (n == null || !ON || !u || !MAP[u] || !el.dataset.nkU) ? n : n * MAP[u][1];
    },
    // grava no campo um valor métrico (formatado no sistema exibido); d = casas decimais
    set(el, v, d) {
      if (!el) return;
      const u = unitOf(el);
      if (v == null || v === '' || isNaN(v)) { el.value = ''; if (el.dataset.nkU) el.dataset.nkD = '\u0000'; return; }
      if (ON && u && MAP[u]) {
        if (!el.dataset.nkU) el.dataset.nkU = '1';
        el.value = fmtIn(v / MAP[u][1], d == null ? MAP[u][2] + 1 : d);
        el.dataset.nkD = '\u0000';  // alterado: converte de volta ao enviar
      } else {
        el.value = fmtIn(v, d == null ? 2 : d);
      }
    },
    // FormData do formulário com as medidas no sistema métrico (para fetch)
    formData(form) {
      const fd = new FormData(form);
      if (ON) form.querySelectorAll('[data-nk-u="1"]').forEach(el => { if (el.name && !el.disabled) fd.set(el.name, metricValue(el)); });
      return fd;
    },
    scan,
  };
})();
