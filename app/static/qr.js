/* QR-Code für Einladungslinks – klein, ohne Abhängigkeiten.
 * Byte-Modus, Fehlerkorrektur M, Version 1–14 (bis 362 Byte, reicht für GiroCodes). Eigene Umsetzung nach ISO/IEC 18004; der
 * Aufbau folgt dem bekannten „QR Code generator“ von Project Nayuki (MIT), siehe THIRD_PARTY.md.
 * qrSvg(text) → SVG-Markup (schwarz auf weiß, 4 Module Ruhezone).
 *
 * Teile nach „QR Code generator library“, Copyright (c) Project Nayuki (https://www.nayuki.io/page/qr-code-generator-library):
 * Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
 * documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
 * rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
 * persons to whom the Software is furnished to do so, subject to the following conditions:
 * - The above copyright notice and this permission notice shall be included in all copies or substantial portions of
 *   the Software.
 * - The Software is provided "as is", without warranty of any kind, express or implied, including but not limited to
 *   the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the
 *   authors or copyright holders be liable for any claim, damages or other liability, whether in an action of
 *   contract, tort or otherwise, arising from, out of or in connection with the Software or the use or other dealings
 *   in the Software. */
const QR = (() => {
  // Fehlerkorrektur-Codewörter pro Block und Anzahl Blöcke für Stufe M, Index = Version
  // Bis Version 14 (26.11.2026): ein GiroCode (EPC, bis 331 Byte) braucht bis Version 13
  const ECC = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24];
  const BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9];

  function rawModules(v) {
    let r = (16 * v + 128) * v + 64;
    if (v >= 2) { const n = Math.floor(v / 7) + 2; r -= (25 * n - 10) * n - 55; if (v >= 7) r -= 36; }
    return r;
  }
  const dataCodewords = (v) => Math.floor(rawModules(v) / 8) - ECC[v] * BLOCKS[v];

  function gfMul(x, y) {
    let z = 0;
    for (let i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11d); z ^= ((y >>> i) & 1) * x; }
    return z;
  }
  function rsDivisor(degree) {
    const r = new Array(degree).fill(0); r[degree - 1] = 1;
    let root = 1;
    for (let i = 0; i < degree; i++) {
      for (let j = 0; j < degree; j++) { r[j] = gfMul(r[j], root); if (j + 1 < degree) r[j] ^= r[j + 1]; }
      root = gfMul(root, 2);
    }
    return r;
  }
  function rsRemainder(data, div) {
    const r = new Array(div.length).fill(0);
    for (const b of data) {
      const f = b ^ r.shift(); r.push(0);
      div.forEach((d, i) => { r[i] ^= gfMul(d, f); });
    }
    return r;
  }

  function encode(text) {
    const bytes = Array.from(new TextEncoder().encode(text));
    let v = 1;
    const top = ECC.length - 1;
    for (; v <= top; v++) if (4 + (v < 10 ? 8 : 16) + bytes.length * 8 <= dataCodewords(v) * 8) break;
    if (v > top) throw new Error("Text zu lang für den QR-Code");
    // Bitstrom: Modus Byte (0100), Länge, Daten, Abschluss, Füllbytes
    const bits = [];
    const push = (val, n) => { for (let i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    push(4, 4); push(bytes.length, v < 10 ? 8 : 16); bytes.forEach((b) => push(b, 8));
    const cap = dataCodewords(v) * 8;
    push(0, Math.min(4, cap - bits.length));
    push(0, (8 - bits.length % 8) % 8);
    for (let pad = 0xec; bits.length < cap; pad ^= 0xec ^ 0x11) push(pad, 8);
    const data = [];
    for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));

    // Blöcke mit Fehlerkorrektur, verschränkt
    const nb = BLOCKS[v], ecl = ECC[v], raw = Math.floor(rawModules(v) / 8);
    const shortN = nb - raw % nb, shortLen = Math.floor(raw / nb), div = rsDivisor(ecl);
    const blocks = [];
    for (let i = 0, k = 0; i < nb; i++) {
      const dat = data.slice(k, k + shortLen - ecl + (i < shortN ? 0 : 1)); k += dat.length;
      const ec = rsRemainder(dat, div);
      if (i < shortN) dat.push(0);
      blocks.push(dat.concat(ec));
    }
    const words = [];
    for (let i = 0; i < blocks[0].length; i++)
      blocks.forEach((b, j) => { if (i !== shortLen - ecl || j >= shortN) words.push(b[i]); });

    // Matrix mit Funktionsmustern
    const size = v * 4 + 17;
    const m = Array.from({ length: size }, () => new Array(size).fill(false));
    const fn = Array.from({ length: size }, () => new Array(size).fill(false));
    const set = (x, y, dark) => { m[y][x] = dark; fn[y][x] = true; };
    for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]])
      for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
        const x = cx + dx, y = cy + dy, d = Math.max(Math.abs(dx), Math.abs(dy));
        if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4);
      }
    if (v >= 2) {
      const n = Math.floor(v / 7) + 2, step = Math.floor((v * 8 + n * 3 + 5) / (n * 4 - 4)) * 2;
      const pos = [6];
      for (let p = size - 7; pos.length < n; p -= step) pos.splice(1, 0, p);
      pos.forEach((a, i) => pos.forEach((b, j) => {
        if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) return;
        for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(a + dx, b + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }));
    }
    const drawFormat = (mask) => {
      const d = (0 << 3) | mask;   // Stufe M = 00
      let r = d; for (let i = 0; i < 10; i++) r = (r << 1) ^ ((r >>> 9) * 0x537);
      const b = ((d << 10) | r) ^ 0x5412, bit = (i) => ((b >>> i) & 1) === 1;
      for (let i = 0; i <= 5; i++) set(8, i, bit(i));
      set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
      for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
      for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
      for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
      set(8, size - 8, true);
    };
    drawFormat(0);
    if (v >= 7) {
      let r = v; for (let i = 0; i < 12; i++) r = (r << 1) ^ ((r >>> 11) * 0x1f25);
      const b = (v << 12) | r;
      for (let i = 0; i < 18; i++) {
        const dark = ((b >>> i) & 1) === 1, a = size - 11 + i % 3, c = Math.floor(i / 3);
        set(a, c, dark); set(c, a, dark);
      }
    }
    // Daten im Zickzack von unten rechts
    let i = 0;
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < size; vert++) for (let j = 0; j < 2; j++) {
        const x = right - j, up = ((right + 1) & 2) === 0, y = up ? size - 1 - vert : vert;
        if (!fn[y][x] && i < words.length * 8) { m[y][x] = ((words[i >>> 3] >>> (7 - (i & 7))) & 1) === 1; i++; }
      }
    }
    // Maske mit der geringsten Strafpunktzahl wählen
    const masks = [(x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
      (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => (x * y) % 2 + (x * y) % 3 === 0,
      (x, y) => ((x * y) % 2 + (x * y) % 3) % 2 === 0, (x, y) => ((x + y) % 2 + (x * y) % 3) % 2 === 0];
    const apply = (k) => { for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!fn[y][x] && masks[k](x, y)) m[y][x] = !m[y][x]; };
    let best = 0, bestScore = Infinity;
    for (let k = 0; k < 8; k++) {
      apply(k); drawFormat(k);
      const s = penalty(m, size);
      if (s < bestScore) { bestScore = s; best = k; }
      apply(k);
    }
    apply(best); drawFormat(best);
    return m;
  }

  function penalty(m, size) {
    let s = 0, dark = 0;
    const lines = [];
    for (let y = 0; y < size; y++) lines.push(m[y]);
    for (let x = 0; x < size; x++) lines.push(m.map((row) => row[x]));
    for (const line of lines) {
      let run = 1;
      for (let i = 1; i <= size; i++) {
        if (i < size && line[i] === line[i - 1]) run++;
        else { if (run >= 5) s += run - 2; run = 1; }
      }
      const str = "0000" + line.map((b) => (b ? "1" : "0")).join("") + "0000";
      for (let i = str.indexOf("1011101"); i >= 0; i = str.indexOf("1011101", i + 1))
        if (str.slice(i - 4, i) === "0000" || str.slice(i + 7, i + 11) === "0000") s += 40;
    }
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
      if (m[y][x]) dark++;
      if (x < size - 1 && y < size - 1 && m[y][x] === m[y][x + 1] && m[y][x] === m[y + 1][x] && m[y][x] === m[y + 1][x + 1]) s += 3;
    }
    return s + Math.floor(Math.abs(dark * 20 - size * size * 10) / (size * size)) * 10;
  }

  function svg(text) {
    const m = encode(text), n = m.length, q = 4;
    let d = "";
    m.forEach((row, y) => row.forEach((on, x) => { if (on) d += `M${x + q} ${y + q}h1v1h-1z`; }));
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${n + 2 * q} ${n + 2 * q}" shape-rendering="crispEdges" role="img" aria-label="QR-Code">` +
      `<rect width="100%" height="100%" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
  }
  return { encode, svg };
})();
