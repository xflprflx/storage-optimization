/* Ajuda nos campos: ao passar o mouse (ou focar) num campo, mostra um cartão com o nome, a explicação e um desenho.
   O texto de cada campo vem do catálogo do servidor (/help/fields.json, services/field_help.py); os desenhos ficam
   aqui (SVG, viewBox 300 × 130), com a parte de que o campo trata em destaque (vermelho). */
(function () {
  'use strict';
  const HI = '#e11d48', HIF = 'rgba(225,29,72,.16)', LN = '#7b8a95', FL = '#e9eef2', TX = '#35424a', BL = '#2f6db5', GD = 'rgba(245,190,40,.45)';
  const a = o => Object.entries(o).map(([k, v]) => `${k}="${v}"`).join(' ');
  const R = (x, y, w, h, on, o = {}) => `<rect ${a({ x, y, width: w, height: h, fill: on ? HIF : FL, stroke: on ? HI : LN, 'stroke-width': on ? 2 : 1, ...o })}/>`;
  const L = (x1, y1, x2, y2, on, o = {}) => `<line ${a({ x1, y1, x2, y2, stroke: on ? HI : LN, 'stroke-width': on ? 2 : 1, ...o })}/>`;
  const T = (x, y, s, on, o = {}) => `<text ${a({ x, y, 'font-size': 10, fill: on ? HI : TX, 'text-anchor': 'middle', 'font-weight': on ? 700 : 400, stroke: '#f7f9fa', 'stroke-width': 2.5, 'paint-order': 'stroke', 'stroke-linejoin': 'round', ...o })}>${s}</text>`;
  const C = (x, y, r, on, o = {}) => `<circle ${a({ cx: x, cy: y, r, fill: on ? HI : LN, ...o })}/>`;
  // cota (seta dupla) com rótulo
  function dim(x1, y1, x2, y2, label, on, below) {
    const c = on ? HI : LN, v = Math.abs(x1 - x2) < 1, t = 3;
    const ends = v ? L(x1 - t, y1, x1 + t, y1, on) + L(x2 - t, y2, x2 + t, y2, on) : L(x1, y1 - t, x1, y1 + t, on) + L(x2, y2 - t, x2, y2 + t, on);
    const lab = v ? T(x1 + 5, (y1 + y2) / 2 + 3, label, on, { 'text-anchor': 'start' }) : T((x1 + x2) / 2, below ? y1 + 12 : y1 - 4, label, on);
    return `<line ${a({ x1, y1, x2, y2, stroke: c, 'stroke-width': on ? 1.6 : 1 })}/>` + ends + lab;
  }
  const arrow = (x1, y1, x2, y2, on, o = {}) => {
    const an = Math.atan2(y2 - y1, x2 - x1), s = 5, c = o.stroke || (on ? HI : LN);
    const p = k => `${x2 - s * Math.cos(an + k)},${y2 - s * Math.sin(an + k)}`;
    return L(x1, y1, x2, y2, on, o) + `<polygon points="${x2},${y2} ${p(0.45)} ${p(-0.45)}" fill="${c}"/>`;
  };
  // caixa em perspectiva (bin, produto): frente w × h, profundidade d
  function box(x, y, w, h, d, o = {}) {
    const f = o.fill || FL, s = o.stroke || LN, k = d * 0.5;
    return `<polygon points="${x},${y} ${x + k},${y - k} ${x + w + k},${y - k} ${x + w},${y}" fill="${f}" stroke="${s}"/>` +
      `<polygon points="${x + w},${y} ${x + w + k},${y - k} ${x + w + k},${y + h - k} ${x + w},${y + h}" fill="${f}" stroke="${s}"/>` +
      `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${f}" stroke="${s}"/>`;
  }

  const D = {};
  // ------------------------------------------------------------------ endereço: corredor-estante-nível-posição-compartimento
  D.addr = h => {
    const seg = [['aisle', 'A', _t('Corredor')], ['bay', '07', _t('Estante')], ['level', '02', _t('Nível')], ['position', '03', _t('Posição')], ['compartment', 'B', 'Compart.']];
    let s = '';
    seg.forEach(([k, v, lab], i) => {
      const x = 14 + i * 56, on = h === k || h === 'all' || (h === 'generic' && i >= 2);
      s += R(x, 6, 46, 28, on) + T(x + 23, 25, v, on, { 'font-size': 14, 'font-weight': 700 }) + T(x + 23, 46, lab, on, { 'font-size': 9 });
      if (i < 4) s += T(x + 51, 25, '-', false, { 'font-size': 14 });
    });
    // estante de frente: níveis e posições
    s += R(20, 76, 120, 46, false, { fill: '#fff' });
    [91, 106].forEach(y => { s += L(20, y, 140, y); });
    [60, 100].forEach(x => { s += L(x, 76, x, 122, false, { 'stroke-dasharray': '2 2' }); });
    if (h === 'level') s += R(20, 91, 120, 15, true);
    if (h === 'position') s += R(100, 91, 40, 15, true);
    if (h === 'compartment') s += R(100, 91, 20, 15, true);
    if (h === 'bay' || h === 'all') s += R(20, 76, 120, 46, true, { fill: 'none' });
    if (h === 'generic') s += R(20, 76, 120, 46, true) + T(80, 103, _t('ocupa os níveis'), true, { 'font-size': 9 });
    s += T(80, 70, _t('estante vista de frente'), false, { 'font-size': 8 });
    // corredor visto de cima
    s += R(170, 82, 110, 10) + R(170, 110, 110, 10) + T(225, 104, h === 'aisle' ? 'corredor A' : 'corredor', h === 'aisle', { 'font-size': 9 });
    if (h === 'aisle') s += R(170, 92, 110, 18, true);
    s += T(225, 70, _t('visto de cima'), false, { 'font-size': 8 });
    return s;
  };
  // ------------------------------------------------------------------ estrutura (módulo): frente, altura, níveis, vão, 1º nível, zona de ouro
  D.rack = h => {
    const x = 28, w = 82, top = 12, flo = 112, base = h === 'base' ? 16 : 0, lv = [0, 1, 2, 3].map(i => flo - base - i * 24);
    let s = L(10, flo, 290, flo, false, { stroke: '#35424a' });
    if (h === 'gold') s += `<rect x="${x - 6}" y="${flo - 74}" width="${w + 12}" height="34" fill="${GD}" stroke="#d4a017"/>` + T(x + w + 10, flo - 54, _t('zona de ouro'), true, { 'font-size': 9, 'text-anchor': 'start' });
    if (h === 'pallet') {
      [0, 1].forEach(i => { s += R(x + 6, flo - 8 - i * 44, w - 12, 8, false, { fill: '#b8864b', stroke: '#7a5528' }) + R(x + 12, flo - 40 - i * 44, w - 24, 32, i === 1); });
      s += T(8, top + 2, _t('sem montantes: camadas empilhadas'), true, { 'font-size': 8, 'text-anchor': 'start' });
    } else {
      s += R(x, top, 4, flo - top, false, { fill: LN }) + R(x + w - 4, top, 4, flo - top, false, { fill: LN });
      lv.forEach((y, i) => { s += R(x, y - 3, w, 3, h === 'levels', { fill: h === 'levels' ? HI : '#5b6770' }) + T(x - 9, y - 8, i + 1, h === 'levels', { 'font-size': 9 }); });
      // bins num nível
      [0, 1, 2].forEach(i => { s += R(x + 7 + i * 23, lv[1] - 19, 21, 16, h === 'cap'); });
    }
    if (h === 'width') s += dim(x, flo + 5, x + w, flo + 5, 'frente', true, true);
    if (h === 'height') s += dim(x + w + 10, top, x + w + 10, flo, _t('altura total'), true);
    if (h === 'vao') s += dim(x + w + 10, lv[2] - 3, x + w + 10, lv[3], _t('vão'), true) + R(x + 4, lv[3], w - 8, 21, true);
    if (h === 'vaos') [60, 40, 35].forEach((v, i) => { s += T(x + w + 8, lv[i] - 8, v + ' cm', true, { 'font-size': 9, 'text-anchor': 'start' }); });
    if (h === 'base') s += dim(x + w + 10, flo - base, x + w + 10, flo, _t('1º nível'), true);
    if (h === 'load') s += arrow(x + w / 2, lv[2] - 22, x + w / 2, lv[2] - 5, true) + T(x + w / 2 + 22, lv[2] - 14, 'kg', true);
    if (h === 'gap') s += dim(x + w + 10, lv[2], x + w + 10, lv[1] - 19, 'folga', true);
    if (h === 'cap') s += T(x + w + 8, lv[1] - 8, _t('cabem 3'), true, { 'font-size': 9, 'text-anchor': 'start' });
    if (h === 'extra') s += R(x + 8, lv[3] - 19, 24, 16, true) + arrow(x + 62, lv[1] - 22, x + 30, lv[3] - 4, true, { 'stroke-dasharray': '3 2' }) + T(x + w + 8, lv[3] - 8, _t('nível extra'), true, { 'font-size': 9, 'text-anchor': 'start' });
    if (h === 'fill') s += R(x + 4, lv[1] - 21, w * 0.7, 18, true) + T(x + w + 8, lv[1] - 8, _t('70% da frente'), true, { 'font-size': 8, 'text-anchor': 'start' });
    if (h === 'side') s += dim(x + 28, lv[1] - 24, x + 30, lv[1] - 24, '', true) + T(x + w + 8, lv[1] - 8, _t('folga lateral'), true, { 'font-size': 8, 'text-anchor': 'start' });
    // vista de cima: frente × profundidade
    s += T(244, 30, _t('visto de cima'), false, { 'font-size': 8 }) + R(206, 40, 76, 40, h === 'depth' || h === 'top' || h === 'width');
    s += L(206, 80, 282, 80, false, { stroke: '#f28e2b', 'stroke-width': 3 }) + T(244, 94, 'corredor', false, { 'font-size': 8 });
    if (h === 'depth') s += dim(290, 40, 290, 80, '', true) + T(244, 63, 'profundidade', true, { 'font-size': 9 });
    if (h === 'width') s += dim(206, 104, 282, 104, 'frente', true, true);
    return s;
  };
  // ------------------------------------------------------------------ bin (caixa) e produto (unidade)
  D.bin = h => {
    let s = box(60, 50, 90, 50, 44, { fill: h === 'vol' ? HIF : FL, stroke: h === 'vol' ? HI : LN });
    if (h === 'w') s += dim(60, 108, 150, 108, 'largura', true, true);
    if (h === 'h') s += dim(48, 50, 48, 100, '', true) + T(30, 78, 'altura', true);
    if (h === 'l') s += dim(156, 104, 178, 82, '', true) + T(196, 100, 'comprimento', true);
    if (h === 'vol') s += T(105, 80, 'volume (L)', true);
    if (h === 'weight') s += arrow(105, 12, 105, 26, true) + T(140, 20, _t('peso máx. (kg)'), true);
    if (h === 'divw' || h === 'divl' || h === 'parent') {
      s += h !== 'divl' ? L(105, 50, 105, 100, true) + L(105, 50, 127, 28, true) : L(71, 39, 161, 39, true);
      s += T(230, 50, h === 'divw' ? _t('2 na largura') : h === 'divl' ? _t('2 no comprimento') : _t('bin pai dividida'), true, { 'font-size': 9 }) + T(230, 64, _t('em frações'), false, { 'font-size': 9 });
    }
    if (h === 'cost') s += T(230, 60, _t('R$ por bin'), true, { 'font-size': 12 });
    if (h === 'type') s += box(206, 70, 40, 26, 22, { fill: HIF, stroke: HI }) + T(226, 112, _t('outro tipo'), true, { 'font-size': 9 });
    if (h === 'qty') s += box(196, 60, 34, 24, 20) + box(236, 60, 34, 24, 20, { fill: HIF, stroke: HI }) + T(234, 104, _t('2 bins do tipo'), true, { 'font-size': 9 });
    return s;
  };
  D.prod = h => {
    let s = box(96, 54, 60, 40, 30, { fill: '#fff' }) + T(126, 78, 'unidade', false, { 'font-size': 9 });
    if (h === 'w') s += dim(96, 102, 156, 102, 'largura', true, true);
    if (h === 'h') s += dim(86, 54, 86, 94, '', true) + T(66, 78, 'altura', true);
    if (h === 'l') s += dim(162, 98, 177, 83, '', true) + T(204, 96, 'comprimento', true);
    if (h === 'weight') s += arrow(126, 16, 126, 32, true) + T(160, 26, 'peso (kg)', true);
    if (h === 'sku') s += R(190, 44, 90, 26, true) + T(235, 61, 'SKU 10045', true, { 'font-size': 11 }) + L(156, 60, 190, 57, true, { 'stroke-dasharray': '2 2' });
    if (h === 'name') s += R(190, 44, 100, 26, true) + T(240, 61, _t('Parafuso M8'), true, { 'font-size': 10 });
    if (h === 'material') s += [0, 1, 2].map(i => box(196 + i * 30, 70, 20, 14, 10, { fill: i ? FL : HIF, stroke: i ? LN : HI })).join('') + T(240, 100, _t('mesmo tipo de material'), true, { 'font-size': 8 });
    return s;
  };
  // ------------------------------------------------------------------ saldo, mínimo e máximo
  D.stock = h => {
    let s = R(120, 14, 50, 100, false, { fill: '#fff' }) + R(120, 64, 50, 50, h === 'qty' || h === 'basis') + T(145, 94, 'saldo', h === 'qty');
    s += L(110, 84, 180, 84, h === 'min', { 'stroke-dasharray': '4 2' }) + T(96, 87, _t('mín.'), h === 'min');
    s += L(110, 30, 180, 30, h === 'max', { 'stroke-dasharray': '4 2' }) + T(96, 33, _t('máx.'), h === 'max');
    if (h === 'growth') s += R(120, 50, 50, 14, true) + T(214, 60, '+ margem (%)', true);
    if (h === 'fill') s += L(110, 30, 180, 30, true) + T(222, 33, _t('enchimento máx. (%)'), true, { 'font-size': 9 });
    if (h === 'factor') s += T(222, 60, '× fator (%)', true) + arrow(184, 64, 184, 34, true);
    if (h === 'neg') s += T(232, 70, '−5  →  5', true, { 'font-size': 13 });
    if (h === 'basis') s += T(232, 50, _t('qual quantidade'), true, { 'font-size': 9 }) + T(232, 62, _t('dimensiona as bins'), true, { 'font-size': 9 });
    return s;
  };
  // ------------------------------------------------------------------ planta da filial (vista de cima)
  D.plan = h => {
    let s = R(8, 8, 284, 114, h === 'L' || h === 'W', { fill: '#fbfcfd' });
    const row = (x, y, on, n = 5) => Array.from({ length: n }, (_, i) => R(x + i * 24, y, 24, 16, on)).join('');
    s += row(80, 28, h === 'row' || h === 'side' || h === 'rev') + row(80, 84, h === 'pair' || h === 'push');
    s += L(80, 44, 200, 44, false, { stroke: '#f28e2b', 'stroke-width': 3 }) + L(80, 84, 200, 84, false, { stroke: '#f28e2b', 'stroke-width': 3 });
    s += R(80, 46, 120, 36, h === 'aisle', { fill: h === 'aisle' ? HIF : '#dbe7f5', stroke: 'none' }) + L(80, 64, 200, 64, false, { stroke: BL, 'stroke-dasharray': '5 3' });
    if (h === 'gap') s += dim(212, 44, 212, 84, 'largura', true);
    if (h === 'flow') s += arrow(100, 64, 180, 64, true) + T(246, 68, _t('mão única'), true, { 'font-size': 9 });
    if (h === 'pair') s += arrow(140, 46, 140, 82, true) + T(246, 96, _t('vizinho de frente'), true, { 'font-size': 9 });
    if (h === 'push') s += arrow(140, 102, 140, 118, true) + T(246, 114, _t('as seguintes andam'), true, { 'font-size': 8 });
    if (h === 'both') s += L(80, 28, 200, 28, true, { 'stroke-dasharray': '6 3', 'stroke-width': 3 }) + arrow(140, 12, 140, 26, true) + T(246, 22, _t('pega por trás'), true, { 'font-size': 9 });
    if (h === 'depth') s += dim(72, 28, 72, 44, '', true) + T(40, 40, 'profund.', true, { 'font-size': 9 });
    if (h === 'bayw') s += dim(80, 22, 104, 22, 'frente', true);
    if (h === 'x') s += dim(8, 36, 80, 36, 'X', true);
    if (h === 'y') s += dim(90, 8, 90, 28, 'Y', true);
    if (h === 'ref' || h === 'front') s += C(32, 64, 5, true) + T(36, 80, h === 'ref' ? _t('referência') : _t('frente livre'), true, { 'font-size': 9 });
    if (h === 'front') s += dim(8, 96, 80, 96, '', true);
    if (h === 'obs') s += R(226, 50, 30, 26, true, { fill: '#2b2f33' }) + T(241, 90, _t('obstáculo'), true, { 'font-size': 9 });
    if (h === 'L') s += dim(8, 128, 292, 128, '', true) + T(246, 22, 'comprimento', true, { 'font-size': 9 });
    if (h === 'W') s += dim(286, 8, 286, 122, '', true) + T(252, 64, 'largura', true, { 'font-size': 9 });
    if (h === 'side') s += [0, 1, 2, 3, 4].map(i => T(92 + i * 24, 40, i * 2 + 1, true, { 'font-size': 8 })).join('') + [0, 1, 2, 3, 4].map(i => T(92 + i * 24, 96, i * 2 + 2, false, { 'font-size': 8 })).join('');
    if (h === 'rev') s += [0, 1, 2, 3, 4].map(i => T(92 + i * 24, 40, 5 - i, true, { 'font-size': 8 })).join('') + arrow(190, 20, 100, 20, true);
    if (h === 'auto') s += T(246, 60, _t('gerada dos'), true, { 'font-size': 9 }) + T(246, 72, _t('endereços'), true, { 'font-size': 9 });
    if (h === 'widths') s += T(246, 60, 'A = 3,5 m', true, { 'font-size': 9 }) + T(246, 72, 'C = 2,8 m', true, { 'font-size': 9 });
    return s;
  };
  // ------------------------------------------------------------------ rotas de separação
  D.route = h => {
    let s = R(8, 8, 284, 114, false, { fill: '#fbfcfd' });
    [30, 62, 94].forEach(y => { s += R(70, y, 190, 10); });
    const stops = [[100, 46], [230, 46], [120, 78], [240, 78]];
    const P = { list: [[30, 64], [100, 46], [230, 46], [266, 46], [266, 78], [240, 78], [120, 78], [56, 78], [30, 64]], s: [[30, 64], [56, 46], [100, 46], [230, 46], [266, 46], [266, 78], [240, 78], [120, 78], [56, 78], [30, 64]], opt: [[30, 64], [56, 46], [100, 46], [230, 46], [266, 46], [266, 78], [240, 78], [120, 78], [56, 78], [30, 64]] };
    const col = { list: '#d62728', s: BL, opt: '#1a9850' }, k = P[h] ? h : 'opt';
    s += `<polyline points="${P[k].map(p => p.join(',')).join(' ')}" fill="none" stroke="${col[k]}" stroke-width="2.5" stroke-linejoin="round"/>`;
    stops.forEach(([x, y], i) => { s += C(x, y, 6, false, { fill: h === 'stops' ? HI : '#4b5560' }) + T(x, y + 3, i + 1, false, { fill: '#fff', 'font-size': 8, stroke: 'none' }); });
    s += C(30, 64, 6, h === 'ref', { fill: h === 'ref' ? HI : '#111' }) + T(34, 84, _t('expedição'), h === 'ref', { 'font-size': 8 });
    const lab = { list: _t('lista: ordem dos endereços'), s: _t('serpentina: corredor a corredor'), opt: _t('otimizada: menor caminho'), order: 'um pedido = uma rota', stops: _t('paradas do pedido') }[h];
    if (lab) s += T(165, 118, lab, true, { 'font-size': 9 });
    return s;
  };
  // ------------------------------------------------------------------ tempo do pedido
  D.time = h => {
    const parts = [['v', 'andar', 70, '#dbe7f5'], ['stop', 'parada', 40, '#e9eef2'], ['piece', _t('peças'), 40, '#e9eef2'], ['gold', _t('fora do ouro'), 62, '#fde68a'], ['v', 'andar', 56, '#dbe7f5']];
    let s = '', x = 14;
    parts.forEach(([k, lab, w, f]) => {
      const on = h === k || (h === 'goldp' && k === 'gold');
      s += R(x, 44, w, 26, on, { fill: on ? HIF : f }) + T(x + w / 2, 60, lab, on, { 'font-size': 8 });
      x += w;
    });
    s += dim(14, 86, x, 86, '', false) + T(150, 102, 'tempo do pedido = soma das partes', false, { 'font-size': 9 });
    const lab = { v: 'caminhada ÷ velocidade (m/s)', stop: _t('tempo fixo por parada (s)'), piece: _t('peças × tempo por peça (s)'), gold: _t('itens fora do ouro × s'), goldp: _t('peças fora do ouro × s'), speed: _t('o vídeo roda N× mais rápido') }[h];
    if (lab) s += T(150, 28, lab, true);
    if (h === 'speed') s += arrow(40, 118, 260, 118, true);
    return s;
  };
  // ------------------------------------------------------------------ planilha (importações)
  D.sheet = h => {
    let s = '';
    const cols = [_t('Pedido'), _t('Código'), _t('Qtd'), _t('Data')], x0 = 40, cw = 56;
    cols.forEach((c, i) => {
      const on = (h === 'col' && i === 1) || (h === 'qty' && i === 2) || (h === 'neg' && i === 2);
      s += R(x0 + i * cw, 22, cw, 18, h === 'header' || on, { fill: h === 'header' || on ? HIF : '#dfe6ea' }) + T(x0 + i * cw + cw / 2, 35, c, h === 'header' || on, { 'font-size': 9 });
      [0, 1, 2].forEach(r => { s += R(x0 + i * cw, 40 + r * 16, cw, 16, on, { fill: on ? HIF : '#fff' }); });
    });
    [['P1', 'SKU-7', h === 'neg' ? '-3' : '3', '02/10'], ['P1', 'SKU-2', '1', '02/10'], ['P2', 'SKU-7', '5', '03/10']].forEach((r, ri) => r.forEach((v, i) => { s += T(x0 + i * cw + cw / 2, 52 + ri * 16, v, false, { 'font-size': 8 }); }));
    s += R(x0, 94, 50, 14, h === 'tab', { fill: h === 'tab' ? HIF : '#fff' }) + T(x0 + 25, 104, _t('Pedidos'), h === 'tab', { 'font-size': 8 }) + R(x0 + 52, 94, 44, 14) + T(x0 + 74, 104, 'SKUs', false, { 'font-size': 8 });
    const lab = { file: 'arquivo CSV / XLSX enviado', header: _t('1ª linha = nomes das colunas'), col: _t('coluna da planilha → campo'), tab: _t('aba da planilha'), qty: _t('colunas de quantidade (somadas)'), neg: _t('negativo vira positivo'), dup: _t('linhas repetidas: somar ou a última'), opt: _t('opção da importação'), def: _t('valor quando a coluna está vazia') }[h];
    if (lab) s += T(150, 124, lab, true, { 'font-size': 9 });
    if (h === 'file') s += R(x0 - 6, 16, cols.length * cw + 12, 96, true, { fill: 'none' });
    return s;
  };
  // ------------------------------------------------------------------ filtros e buscas em listas
  D.find = h => {
    let s = R(20, 12, 180, 22, h === 'text' || h === 'select', { fill: '#fff' }) + T(60, 27, h === 'select' ? _t('opção ▾') : '10*', h === 'text' || h === 'select', { 'font-size': 11 });
    s += C(214, 23, 8, false, { fill: 'none', stroke: LN, 'stroke-width': 2 }) + L(220, 29, 228, 37, false, { 'stroke-width': 2 });
    ['1001  ' + _t('Parafuso'), '2040  ' + _t('Arruela'), '1077  ' + _t('Porca'), '3300  ' + _t('Bucha')].forEach((t, i) => {
      const on = i === 0 || i === 2;
      s += R(20, 44 + i * 19, 260, 17, on && h !== 'check' && h !== 'page', { fill: on && h !== 'check' ? HIF : '#fff' }) + T(30, 56 + i * 19, t, false, { 'text-anchor': 'start', 'font-size': 9 });
      if (h === 'check' || h === 'all') s += R(262, 47 + i * 19, 11, 11, h === 'all' || i < 2, { fill: (h === 'all' || i < 2) ? HI : '#fff' });
    });
    const lab = { text: _t('só as linhas que combinam ficam'), select: _t('filtra a lista pela opção'), check: _t('marca as linhas para a ação em massa'), all: _t('marca todas as linhas da página'), status: _t('filtra pela situação') }[h];
    if (lab) s += T(150, 126, lab, true, { 'font-size': 9 });
    return s;
  };
  // ------------------------------------------------------------------ escopo: filial -> armazém -> endereços
  D.scope = h => {
    let s = R(110, 10, 80, 24, h === 'branch') + T(150, 26, 'Filial 01', h === 'branch');
    s += L(150, 34, 90, 56) + L(150, 34, 210, 56);
    s += R(50, 56, 80, 24, h === 'wh') + T(90, 72, _t('Armazém CD1'), h === 'wh', { 'font-size': 9 }) + R(170, 56, 80, 24, h === 'wh2') + T(210, 72, _t('Armazém CD2'), h === 'wh2', { 'font-size': 9 });
    [60, 84, 108].forEach(x => { s += L(90, 80, x + 6, 98) + R(x, 98, 14, 14, h === 'locs'); });
    [180, 204, 228].forEach(x => { s += L(210, 80, x + 6, 98) + R(x, 98, 14, 14); });
    const lab = { branch: _t('tudo vale para esta filial'), wh: _t('restringe a um armazém da filial'), locs: _t('endereços do escopo'), link: _t('armazém ligado à filial'), new: _t('a filial nova criada pelo projeto') }[h];
    if (h === 'link') s += L(150, 34, 90, 56, true, { 'stroke-width': 3 });
    if (h === 'new') s += R(110, 10, 80, 24, true);
    if (lab) s += T(150, 126, lab, true, { 'font-size': 9 });
    return s;
  };
  // ------------------------------------------------------------------ otimização: antes -> depois das bins
  D.swap = h => {
    let s = T(70, 16, _t('antes'), false, { 'font-size': 9 }) + T(230, 16, _t('depois'), false, { 'font-size': 9 }) + arrow(128, 66, 170, 66, true);
    [0, 1, 2, 3].forEach(i => { s += box(20 + (i % 2) * 50, 40 + Math.floor(i / 2) * 40, 34, 24, 16); });
    if (h === 'unlimited') { s += box(196, 44, 60, 44, 24, { fill: HIF, stroke: HI }) + T(226, 110, _t('pode usar bins novas'), true, { 'font-size': 8 }); }
    else { [0, 1].forEach(i => { s += box(190 + i * 54, 52, 40, 30, 18, { fill: HIF, stroke: HI }); }); }
    const lab = { objective: _t('o que o plano deve melhorar'), prio: _t('desempate entre planos iguais'), existing: _t('só as bins que já existem'), free: _t('só bins sem saldo'), any: _t('pode tirar bins de outros itens'), orphans: _t('o que fazer com quem perdeu a bin'), moves: _t('menos trocas'), clear: _t('tira bins de endereço vazio'), focus: _t('só os produtos filtrados'), maxbins: _t('limite de bins'), types: _t('tipos de bin permitidos') }[h];
    if (lab) s += T(150, 126, lab, true, { 'font-size': 9 });
    return s;
  };
  // ------------------------------------------------------------------ unidades dentro da bin
  D.fit = h => {
    let s = R(90, 24, 120, 84, false, { fill: '#fff', 'stroke-width': 2 });
    const u = (x, y, w, hh, on) => R(x, y, w, hh, on, { fill: on ? HIF : '#cfd8df' });
    if (h === 'rotation') { s += u(96, 76, 26, 30) + u(124, 76, 26, 30) + u(152, 90, 36, 16, true) + T(240, 96, 'girada', true, { 'font-size': 9 }); }
    else if (h === 'mix') { s += u(96, 70, 30, 36) + u(128, 70, 30, 36) + R(164, 82, 40, 24, true, { fill: '#fde68a' }) + T(250, 96, 'outro SKU', true, { 'font-size': 9 }); }
    else { [0, 1, 2].forEach(i => [0, 1].forEach(j => { s += u(96 + i * 36, 78 - j * 30, 34, 28, h === 'geometry'); })); }
    if (h === 'fill') s += L(84, 40, 216, 40, true, { 'stroke-dasharray': '4 2' }) + T(250, 43, _t('enche até %'), true, { 'font-size': 9 });
    if (h === 'growth') s += R(96, 28, 108, 18, true) + T(252, 40, 'folga p/ crescer', true, { 'font-size': 8 });
    if (h === 'weight') s += arrow(150, 4, 150, 20, true) + T(196, 14, _t('kg máx.'), true);
    if (h === 'geometry') s += T(250, 70, _t('cabe pelas'), true, { 'font-size': 9 }) + T(250, 82, 'medidas', true, { 'font-size': 9 });
    if (h === 'single') s += T(250, 70, _t('um tipo só'), true, { 'font-size': 9 });
    if (h === 'maxw') s += dim(90, 114, 210, 114, _t('largura máx.'), true, true);
    if (h === 'maxh') s += dim(80, 24, 80, 108, '', true) + T(46, 68, _t('altura máx.'), true, { 'font-size': 9 });
    return s;
  };
  // ------------------------------------------------------------------ solver
  D.solver = h => {
    let s = L(30, 110, 270, 110) + L(30, 110, 30, 14) + T(150, 126, _t('tempo de cálculo'), false, { 'font-size': 8 }) + T(18, 60, 'custo', false, { 'font-size': 8, transform: 'rotate(-90 18 60)' });
    s += `<path d="M34 24 C 70 80, 120 98, 266 102" fill="none" stroke="${BL}" stroke-width="2"/>`;
    if (h === 'time') s += L(170, 14, 170, 110, true, { 'stroke-dasharray': '4 2' }) + T(210, 30, _t('tempo limite'), true, { 'font-size': 9 });
    if (h === 'unlimited') s += arrow(200, 30, 266, 30, true) + T(226, 22, _t('até provar o ótimo'), true, { 'font-size': 8 });
    if (h === 'engine') s += C(60, 68, 4, true) + T(96, 60, _t('heurístico'), true, { 'font-size': 8 }) + C(260, 102, 4, true) + T(232, 92, _t('pesquisa operacional'), true, { 'font-size': 8 });
    return s;
  };
  // ------------------------------------------------------------------ posição pela saída (slotting)
  D.slot = h => {
    let s = R(8, 8, 284, 114, false, { fill: '#fbfcfd' });
    [26, 58, 90].forEach((y, i) => { s += R(70, y, 200, 12, h === 'ref' && i === 1) + T(60, y + 10, 'ABC'[i], h === 'ref' && i === 1, { 'font-size': 9 }); });
    s += C(30, 64, 6, true) + T(34, 84, _t('expedição'), false, { 'font-size': 8 });
    if (h === 'walk') s += `<path d="M30 14 L130 64 L30 114" fill="${GD}" stroke="#d4a017" stroke-dasharray="5 3"/>` + T(96, 20, _t('zona de ouro da caminhada'), true, { 'font-size': 8 });
    if (h === 'aisle_m') s += dim(276, 32, 276, 64, '', true) + T(240, 50, _t('entre corredores'), true, { 'font-size': 8 });
    if (h === 'bay_m') s += dim(70, 22, 95, 22, '', true) + T(140, 22, _t('frente da estante'), true, { 'font-size': 8 });
    if (h === 'on' || h === 'metric') s += [0, 1, 2].map(i => R(74 + i * 26, 59, 22, 10, true, { fill: ['#e11d48', '#f59e0b', '#9ca3af'][i], stroke: 'none' })).join('') + T(180, 52, _t('mais vendidos perto da expedição'), true, { 'font-size': 8 });
    if (h === 'aff') s += R(74, 59, 22, 10, true) + R(98, 59, 22, 10, true) + T(170, 52, 'saem juntos → ficam perto', true, { 'font-size': 8 });
    if (h === 'goldm') s += T(170, 52, _t('nível ruim "custa" metros'), true, { 'font-size': 8 }) + arrow(120, 76, 120, 60, true);
    if (h === 'levels') s += T(170, 52, _t('níveis bons de pegar: 2, 3'), true, { 'font-size': 8 });
    if (h === 'rows') s += T(60, 36, 'A', true, { 'font-size': 9 }) + T(60, 68, 'B', true, { 'font-size': 9 }) + T(180, 52, _t('uma letra por rua'), true, { 'font-size': 8 });
    if (h === 'serp') s += arrow(80, 50, 260, 50, true) + arrow(260, 82, 80, 82, true);
    if (h === 'xy') s += dim(8, 104, 30, 104, 'X', true) + dim(40, 8, 40, 58, 'Y', true);
    return s;
  };
  // ------------------------------------------------------------------ geração em lote (faixas de códigos)
  D.range = h => {
    let s = '';
    ['01', '02', '03', '…', '10'].forEach((v, i) => {
      const on = (h === 'start' && i === 0) || (h === 'end' && i === 4) || h === 'pad' || (h === 'value' && i === 0);
      if (h === 'value' && i > 0) return;
      s += R(34 + i * 48, 40, 40, 30, on) + T(54 + i * 48, 60, h === 'alpha' ? 'ABC…J'[i] || 'J' : v, on, { 'font-size': 13 });
    });
    const lab = { start: _t('início da faixa'), end: _t('fim da faixa'), pad: _t('dígitos: 1 → 01'), type: _t('números, letras, fixo ou nenhum'), value: _t('valor fixo (um só código)'), alpha: 'letras: A, B, C…', exc: _t('regra diferente para parte dos endereços'), replace: _t('recria os endereços livres'), cond: _t('onde a exceção vale') }[h];
    if (lab) s += T(150, 96, lab, true);
    if (h === 'exc' || h === 'cond') s += R(130, 40, 40, 30, true);
    return s;
  };
  // ------------------------------------------------------------------ imóvel do projeto de filial nova (vista de cima)
  D.site = h => {
    let s = R(10, 10, 280, 110, h === 'L' || h === 'W', { fill: '#fbfcfd', 'stroke-width': 2 });
    s += R(10, 10, 34, 110, h === 'front', { fill: h === 'front' ? HIF : '#fff3cd' }) + R(262, 10, 28, 110, h === 'back', { fill: h === 'back' ? HIF : '#fff3cd' });
    const rows = [22, 34, 58, 70, 94, 106];
    rows.forEach(y => { s += R(56, y, 90, 10, h === 'rows') + R(160, y, 90, 10, h === 'rows'); });
    s += R(56, 44, 194, 14, h === 'aisle', { fill: h === 'aisle' ? HIF : '#e3e9ee', stroke: 'none' }) + R(146, 22, 14, 94, h === 'cross' || h === 'crossw', { fill: (h === 'cross' || h === 'crossw') ? HIF : '#e3e9ee', stroke: 'none' });
    if (h === 'L') s += dim(10, 126, 290, 126, '', true) + T(150, 8, 'comprimento', true, { 'font-size': 9 });
    if (h === 'W') s += dim(296, 10, 296, 120, '', true) + T(274, 68, 'largura', true, { 'font-size': 8 });
    if (h === 'wall') s += dim(56, 10, 56, 22, '', true) + T(100, 18, 'afastamento', true, { 'font-size': 8 });
    if (h === 'aisle') s += T(200, 54, 'corredor', true, { 'font-size': 8 });
    if (h === 'cross') s += T(153, 8, 'transversal', true, { 'font-size': 8 });
    if (h === 'caps') s += R(50, 22, 6, 22, true) + R(50, 58, 6, 22, true) + T(100, 54, _t('cabeceiras nas pontas'), true, { 'font-size': 8 });
    if (h === 'obst') s += R(200, 60, 30, 26, true, { fill: '#2b2f33' });
    if (h === 'sector') s += R(14, 40, 26, 50, true) + T(27, 68, 'setor', true, { 'font-size': 8 });
    if (h === 'zone') s += R(54, 20, 94, 98, true, { fill: 'none', 'stroke-dasharray': '5 3' }) + T(100, 54, 'zona', true, { 'font-size': 9 });
    if (h === 'avenue') s += R(146, 12, 14, 106, true) + T(153, 8, 'avenida', true, { 'font-size': 8 });
    if (h === 'orient') s += arrow(70, 50, 140, 50, true) + T(200, 54, _t('direção das fileiras'), true, { 'font-size': 8 });
    if (h === 'serp') s += arrow(60, 50, 140, 50, true) + arrow(140, 86, 60, 86, true);
    if (h === 'mode') s += T(100, 30, 'A', true, { 'font-size': 9 }) + T(100, 42, 'B', true, { 'font-size': 9 }) + T(200, 54, _t('letra por corredor ou por rua'), true, { 'font-size': 8 });
    if (h === 'H') s += T(150, 54, _t('pé-direito: altura livre'), true, { 'font-size': 9 });
    if (h === 'full') s += rows.map(y => R(56, y, 90, 10, true) + R(160, y, 90, 10, true)).join('') + T(150, 126, _t('maior capacidade: enche o imóvel'), true, { 'font-size': 9 });
    if (h === 'ref') s += C(27, 64, 5, true);
    return s;
  };
  // ------------------------------------------------------------------ vista 3D
  D.view3d = h => {
    let s = box(90, 44, 90, 60, 50, { fill: '#fff' });
    [0, 1, 2].forEach(i => { s += L(90, 64 + i * 14, 180, 64 + i * 14) + box(98 + i * 26, 88, 20, 14, 10, { fill: h === 'bins' || h === 'color' ? ['#4e79a7', '#f28e2b', '#59a14f'][i] : FL }); });
    if (h === 'cut') s += L(70, 60, 220, 60, true, { 'stroke-dasharray': '5 3' }) + T(252, 58, _t('esconde acima'), true, { 'font-size': 8 });
    if (h === 'structs') s += R(90, 44, 4, 60, true, { fill: HI }) + R(176, 44, 4, 60, true, { fill: HI });
    if (h === 'walls') s += R(60, 12, 180, 106, true, { fill: 'none' });
    if (h === 'find') s += box(124, 88, 20, 14, 10, { fill: HI, stroke: HI }) + T(250, 96, 'achado', true, { 'font-size': 9 });
    if (h === 'cam') s += C(40, 30, 8, true) + arrow(48, 34, 90, 56, true) + T(50, 16, _t('câmera'), true, { 'font-size': 9 });
    if (h === 'gold') s += `<rect x="86" y="62" width="98" height="16" fill="${GD}" stroke="#d4a017"/>`;
    if (h === 'prog') s += L(60, 120, 240, 120, false, { 'stroke-width': 3 }) + C(130, 120, 5, true);
    if (h === 'color') s += T(250, 80, _t('cor = critério'), true, { 'font-size': 9 });
    if (h === 'zoom') s += T(250, 80, _t('aumenta o desenho'), true, { 'font-size': 8 });
    return s;
  };
  // ------------------------------------------------------------------ configurações de endereço
  D.cfg = h => {
    if (h === 'zero') return R(50, 40, 70, 34, true) + T(85, 62, 'F.07', true, { 'font-size': 14 }) + T(150, 62, '=', true, { 'font-size': 18 }) + R(180, 40, 70, 34, true) + T(215, 62, 'F.7', true, { 'font-size': 14 }) + T(150, 100, _t('o mesmo local'), true);
    let s = T(80, 20, _t('numérica'), h === 'sort') + T(220, 20, _t('alfabética'), h === 'sort');
    ['2', '3', '10'].forEach((v, i) => { s += R(50, 30 + i * 26, 60, 22) + T(80, 45 + i * 26, 'A-' + v); });
    ['10', '2', '3'].forEach((v, i) => { s += R(190, 30 + i * 26, 60, 22) + T(220, 45 + i * 26, 'A-' + v); });
    return s;
  };
  // ------------------------------------------------------------------ exclusão
  D.del = h => {
    let s = R(110, 14, 80, 26, true) + T(150, 31, 'item', true) + L(125, 31, 175, 31, true);
    [70, 150, 230].forEach((x, i) => { const on = h === 'cascade'; s += L(150, 40, x, 70, on) + R(x - 34, 70, 68, 24, on) + T(x, 86, [_t('endereços'), 'saldos', _t('níveis')][i], on, { 'font-size': 9 }); });
    s += T(150, 118, h === 'cascade' ? _t('os relacionados saem junto') : h === 'levels' ? _t('o nível vazio também sai') : h === 'one' ? _t('só este item é excluído') : _t('só o item é excluído'), true, { 'font-size': 9 });
    return s;
  };
  // ------------------------------------------------------------------ código / nome / observação
  D.tag = h => {
    let s = R(60, 30, 180, 70, false, { fill: '#fff' });
    s += R(72, 42, 60, 20, h === 'code') + T(102, 56, 'CD-01', h === 'code', { 'font-size': 11, 'font-weight': 700 });
    s += R(72, 68, 156, 16, h === 'name') + T(150, 80, _t('Centro de distribuição SP'), h === 'name', { 'font-size': 9 });
    if (h === 'notes') s += L(72, 92, 220, 92, true) + T(150, 116, _t('texto livre, só para consulta'), true, { 'font-size': 9 });
    if (h === 'code') s += T(150, 116, _t('identificador curto e único'), true, { 'font-size': 9 });
    if (h === 'name') s += T(150, 116, _t('descrição que aparece nas telas'), true, { 'font-size': 9 });
    return s;
  };

  function svg(g) {
    const [k, h] = String(g || '').split(':');
    const fn = D[k];
    return fn ? `<svg viewBox="0 0 300 130" width="300" height="130" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif">${fn(h || '')}</svg>` : '';
  }

  // ==================================================================== catálogo e cartão
  let RULES = null;
  const VER = (document.currentScript ? (document.currentScript.dataset.v || '1') : '1') + '-' + ((window.NK_I18N && window.NK_I18N.lang) || 'pt-BR');
  function load() {
    try { const c = sessionStorage.getItem('nk-help-' + VER); if (c) { RULES = compile(JSON.parse(c)); return; } } catch (e) { /* sem cache */ }
    fetch('/help/fields.json?v=' + encodeURIComponent(VER)).then(r => r.json()).then(j => {
      RULES = compile(j);
      try { sessionStorage.setItem('nk-help-' + VER, JSON.stringify(j)); } catch (e) { /* idem */ }
    }).catch(() => { RULES = []; });
  }
  const compile = j => j.map(r => ({ ...r, P: new RegExp('^(?:' + r.p + ')'), K: new RegExp('^(?:' + r.k + ')$') }));
  // chave do campo: name, senão #id, senão o papel (marcar todos / marcar linha)
  function keyOf(el) {
    if (el.name) return el.name;
    if (el.id) return '#' + el.id;
    if (el.dataset && el.dataset.checkAll !== undefined) return '#check-all';
    if (el.type === 'checkbox') return '#row-check';
    return '';
  }
  function lookup(el) {
    if (!RULES) return null;
    const k = keyOf(el), path = location.pathname;
    if (!k) return null;
    return RULES.find(r => r.P.test(path) && r.K.test(k)) || null;
  }
  const FIELD = 'input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea';
  function fieldOf(t) {
    if (!t || !t.closest) return null;
    const f = t.closest(FIELD);
    if (f) return f;
    const lab = t.closest('label');
    if (!lab) return null;
    if (lab.htmlFor) { const e = document.getElementById(lab.htmlFor); return e && e.matches(FIELD) ? e : null; }
    return lab.querySelector(FIELD);
  }

  let card = null, cur = null, timer = null, enabled = true;
  try { enabled = localStorage.getItem('nk-help-off') !== '1'; } catch (e) { /* padrão: ligada */ }
  function ensureCard() {
    if (card) return card;
    card = document.createElement('div');
    card.className = 'nk-help-card';
    card.setAttribute('role', 'tooltip');
    document.body.appendChild(card);
    return card;
  }
  const esc = t => String(t ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function show(el, anchor) {
    const r = lookup(el);
    if (!r) return;
    hide();
    cur = el;
    // o tooltip nativo (title) não aparece junto com o cartão
    [el, anchor].forEach(n => { if (n && n.title) { n.dataset.nkTitle = n.title; n.removeAttribute('title'); } });
    const c = ensureCard();
    c.innerHTML = `<div class="nk-help-t">${esc(r.t)}</div><div class="nk-help-d">${esc(r.d)}</div>${svg(r.g)}`;
    c.style.display = 'block';
    const b = (anchor || el).getBoundingClientRect(), cw = c.offsetWidth, ch = c.offsetHeight;
    let x = Math.min(Math.max(8, b.left), window.innerWidth - cw - 8), y = b.bottom + 8;
    if (y + ch > window.innerHeight - 8) y = b.top - ch - 8 > 8 ? b.top - ch - 8 : Math.max(8, window.innerHeight - ch - 8);
    c.style.left = x + 'px'; c.style.top = y + 'px';
  }
  function hide() {
    clearTimeout(timer);
    if (card) card.style.display = 'none';
    document.querySelectorAll('[data-nk-title]').forEach(n => { n.title = n.dataset.nkTitle; delete n.dataset.nkTitle; });
    cur = null;
  }
  document.addEventListener('mouseover', e => {
    if (!enabled) return;
    const f = fieldOf(e.target);
    if (!f || f === cur) return;
    clearTimeout(timer);
    const anchor = e.target.closest('label') || f;
    timer = setTimeout(() => show(f, anchor), 450);
  });
  document.addEventListener('mouseout', e => {
    const f = fieldOf(e.target);
    if (!f) return;
    const to = e.relatedTarget;
    if (to && fieldOf(to) === f) return;
    if (document.activeElement === f && cur === f) return;  // com o foco no campo o cartão fica
    hide();
  });
  document.addEventListener('focusin', e => { if (enabled && e.target.matches && e.target.matches(FIELD)) { clearTimeout(timer); timer = setTimeout(() => show(e.target, e.target), 250); } });
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', hide, true);
  window.addEventListener('pointerdown', () => { clearTimeout(timer); }, true);

  // botão liga/desliga (na barra do topo)
  function toggleBtn() {
    const b = document.getElementById('nkHelpToggle');
    if (!b) return;
    const paint = () => { b.classList.toggle('active', enabled); b.title = enabled ? _t('Ajuda nos campos: ligada (clique para desligar)') : _t('Ajuda nos campos: desligada (clique para ligar)'); b.setAttribute('aria-pressed', enabled); };
    b.addEventListener('click', e => { e.preventDefault(); enabled = !enabled; try { localStorage.setItem('nk-help-off', enabled ? '0' : '1'); } catch (err) { /* idem */ } if (!enabled) hide(); paint(); });
    paint();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', toggleBtn); else toggleBtn();
  load();
  window.nkHelp = { lookup, keyOf, svg, show, hide, get rules() { return RULES; }, diagrams: Object.keys(D) };
})();
