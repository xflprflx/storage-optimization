// Aparência 3D de bins e estruturas (services/appearance.py): posição das peças e a prévia do cadastro.
// Peça normalizada: {s: 'b'|'c', a: eixo do cilindro, x, y, z, w, h, d (frações), c: cor, o: opacidade, t: tinge, l: por nível}
// Local: x = largura (frente, da esquerda para a direita), y = altura, z = profundidade (0 = frente, lado do corredor).
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// item (cm, coordenadas da planta): x, y = canto; z = base; W = largura ao longo da frente, D = profundidade, H = altura;
// fx = frente ao longo de X da planta; ff = corredor do lado maior (frente "longe" do canto). Devolve a caixa da peça
// na planta (x, y, z, w, d, h) e o eixo do cilindro na cena ('x', 'y' = altura, 'z' = Y da planta).
export function place(p, it) {
  let u0 = p.x, du = p.w, t0 = p.z, dt = p.d;
  if (it.ff) { u0 = 1 - u0 - du; t0 = 1 - t0 - dt; }  // virada 180°: a frente olha para o outro lado
  const o = { z: it.z + p.y * it.H, h: p.h * it.H, ax: p.a || 'y' };
  if (it.fx) {
    o.x = it.x + u0 * it.W; o.w = du * it.W; o.y = it.y + t0 * it.D; o.d = dt * it.D;
  } else {  // girada 90°: a largura corre ao longo de Y da planta
    o.x = it.x + t0 * it.D; o.w = dt * it.D; o.y = it.y + (1 - u0 - du) * it.W; o.d = du * it.W;
    if (o.ax === 'x') o.ax = 'z'; else if (o.ax === 'z') o.ax = 'x';
  }
  return o;
}

// geometrias de lado 1 com a origem no canto (escala = tamanho): caixa e cilindros nos três eixos
export function geometries() {
  const box = new THREE.BoxGeometry(1, 1, 1); box.translate(0.5, 0.5, 0.5);
  const cyl = ax => {
    const g = new THREE.CylinderGeometry(0.5, 0.5, 1, 18);
    if (ax === 'x') g.rotateZ(Math.PI / 2); else if (ax === 'z') g.rotateX(Math.PI / 2);
    g.translate(0.5, 0.5, 0.5); return g;
  };
  return { b: box, x: cyl('x'), y: cyl('y'), z: cyl('z') };
}

export const geoOf = (G, p, ax) => p.s === 'c' ? G[ax || p.a || 'y'] : G.b;

export function material(p) {
  const op = p.o == null ? 1 : p.o;
  return new THREE.MeshLambertMaterial({ color: 0xffffff, transparent: op < 0.999, opacity: op, depthWrite: op >= 0.999 });
}

// cor da peça: as "tingidas" levam a cor da vista; sem nenhuma tingida, todas puxam um pouco para ela
export function partColor(p, look, viewColor, out) {
  if (look.solid) return out.set(p.c || '#8a96a0');  // estrutura: as cores reais de cada peça
  if (p.t) return out.copy(viewColor);
  out.set(p.c || '#8a96a0');
  return look.tint ? out : out.lerp(viewColor, 0.35);
}

// ------------------------------------------------------------------ prévia do cadastro
// kind 'bin' | 'struct'; draw(look, dims {w, h, d}, levels [base de cada nível, cm], floor)
export function mountPreview(host) {
  const scene = new THREE.Scene(); scene.background = new THREE.Color('#f4f6f8');
  const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 200);
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  host.innerHTML = ''; host.appendChild(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x9aa5ad, 1.1));
  const sun = new THREE.DirectionalLight(0xffffff, 1.3); sun.position.set(3, 5, 4); scene.add(sun);
  const grid = new THREE.GridHelper(10, 20, 0xc9d1d6, 0xe3e9ee); scene.add(grid);
  const G = geometries(), group = new THREE.Group(); scene.add(group);
  const controls = new OrbitControls(camera, renderer.domElement);
  const size = () => { const w = host.clientWidth || 300, h = host.clientHeight || 260; renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix(); };
  new ResizeObserver(size).observe(host); size();
  const view = new THREE.Color('#4e79a7'), tmpC = new THREE.Color();
  const add = (o, color, op = 1) => {  // caixa/cilindro na planta (cm) -> cena (m)
    const p = { s: o.s || 'b', o: op };
    const m = new THREE.Mesh(geoOf(G, p, o.ax), material(p));
    m.material.color.copy(color);
    m.position.set(o.x / 100, o.z / 100, o.y / 100); m.scale.set(Math.max(o.w, 0.05) / 100, Math.max(o.h, 0.05) / 100, Math.max(o.d, 0.05) / 100);
    group.add(m);
  };
  function draw(look, dims, levels, opts = {}) {
    group.clear();
    const W = Math.max(+dims.w || 0, 1), H = Math.max(+dims.h || 0, 1), D = Math.max(+dims.d || 0, 1);
    const it = { x: -W / 2, y: -D / 2, z: 0, W, D, H, fx: true, ff: false };
    if (look) {
      look.parts.forEach(p => {
        const bases = p.l ? (levels.length ? levels : [0]) : [0];
        bases.forEach(zb => {
          const o = place(p, { ...it, z: zb });
          if (o.z < -0.01) return;  // peça do nível abaixo do piso (longarina do 1º nível no chão)
          add({ ...o, s: p.s }, partColor(p, look, view, tmpC), p.o == null ? 1 : p.o);
        });
      });
    } else if (opts.kind === 'struct') {  // desenho simples: montantes e bandejas
      const P = 4, col = new THREE.Color(opts.floor ? '#b8864b' : '#8a96a0');
      if (!opts.floor) [[0, 0], [W - P, 0], [0, D - P], [W - P, D - P]].forEach(([dx, dy]) => add({ x: it.x + dx, y: it.y + dy, z: 0, w: P, d: P, h: H }, col));
      (levels.length ? levels : [0]).forEach(z => add({ x: it.x, y: it.y, z, w: W, d: D, h: opts.floor ? 14 : 2.5 }, col.clone().lerp(new THREE.Color('#fff'), 0.4), 0.85));
    } else {
      add({ x: it.x + 0.5, y: it.y + 0.5, z: 0, w: W - 1, d: D - 1, h: H }, view);
    }
    const r = Math.max(W, H, D) / 100;
    grid.scale.setScalar(Math.max(0.3, r / 3));
    camera.position.set(r * 1.25, r * 0.95, r * 1.6);
    const tgt = new THREE.Vector3(0, H / 200, 0);
    if (controls) { controls.target.copy(tgt); controls.update(); } else camera.lookAt(tgt);
  }
  (function loop() { renderer.render(scene, camera); requestAnimationFrame(loop); })();
  return { draw, renderer };
}
