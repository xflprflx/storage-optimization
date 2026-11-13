/* Traduções no JavaScript das telas. _t('Texto {x}', {x: valor}) devolve o texto no idioma atual
   (catálogo em window.NK_T, carregado de /i18n/<idioma>.js; sem tradução, fica o português).
   nkNum(valor, casas) formata números no padrão do idioma (window.NK_I18N.locale). */
(function () {
  const I = window.NK_I18N || { lang: 'pt-BR', locale: 'pt-BR', units: 'metric', dec: ',', th: '.' };
  window.NK_I18N = I;
  window._t = function (s, p) {
    let r = (window.NK_T && window.NK_T[s]) || s;
    if (p) r = r.replace(/\{(\w+)\}/g, (m, k) => (k in p ? (p[k] == null ? '' : String(p[k])) : m));
    return window.nkU ? window.nkU.text(r) : r;  // medidas no sistema escolhido (units.js)
  };
  const fmts = {};
  window.nkNum = function (v, min, max) {
    if (v == null || v === '' || isNaN(v)) return '';
    const key = min + ':' + (max === undefined ? min : max);
    if (!fmts[key]) {
      const o = {};
      if (min != null) { o.minimumFractionDigits = min; o.maximumFractionDigits = max === undefined ? min : max; }
      fmts[key] = new Intl.NumberFormat(I.locale, o);
    }
    return fmts[key].format(v);
  };
})();
