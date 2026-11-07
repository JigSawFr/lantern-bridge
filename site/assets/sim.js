// The simulator of the Bridge's page: pick the sources, see the library a Lantern client would get from them.
// Everything here is made up (games, sizes, hashes) and stays in the page; nothing is sent anywhere. Without script
// the section stays hidden: the rest of the page says the same thing in words.
'use strict';

(function () {
  const root = document.getElementById('simulator');
  if (!root) return;
  const T = root.dataset;
  const lang = document.documentElement.lang || 'en';

  // Seven files, as a bridge would hash them: the same SHA-256 in two sources is one file in the library.
  const FILES = [
    { id: 'a_Fr0sT1', tid: '0100ABCD00010000', app: '0100ABCD00010000', type: 'base', code: 0, version: '1.0.0', name: 'Frostline Tales', size: 5368709120, format: 'xci', sha: '156e32f78e91b392bef501d0a30959ba4ccd498d9dacdbf29a5848fe80e1ff6b', in: ['folders', 'tinfoil', 'ownfoil'] },
    { id: 'a_Fr0sT2', tid: '0100ABCD00010000', app: '0100ABCD00010800', type: 'update', code: 196608, version: '1.3.0', name: 'Frostline Tales', size: 734003200, format: 'nsz', sha: '03f2b8d16a6ea16b515cd594c51748bcf94cb3036234bfe06316a9afc459df31', in: ['tinfoil', 'ownfoil'] },
    { id: 'a_M0ss01', tid: '0100ABCD00020000', app: '0100ABCD00020000', type: 'base', code: 0, version: '1.0.0', name: 'Moss & Ember', size: 2147483648, format: 'nsp', sha: '2b54b5982173b18ee94fa6179ed975d9cce4ca75758521e004f24718f097cad2', in: ['folders', 'aerofoil'] },
    { id: 'a_M0ss02', tid: '0100ABCD00020000', app: '0100ABCD00020800', type: 'update', code: 65536, version: '1.1.0', name: 'Moss & Ember', size: 268435456, format: 'nsp', sha: '12137ccaf7d0d63299732c4636ecc0692170c61938b7a1514161beed7803613b', in: ['aerofoil'] },
    { id: 'a_C1nd01', tid: '0100ABCD00030000', app: '0100ABCD00030000', type: 'base', code: 0, version: '1.2.0', name: 'Cinder Harbor', size: 3221225472, format: 'xci', sha: '0a3eecb79bd1f2354e1ca0eca3bc6474725f98ebf06b777c96fea25367d43358', in: ['tinfoil'] },
    { id: 'a_Pp3r01', tid: '0100ABCD00040000', app: '0100ABCD00040000', type: 'base', code: 0, version: '1.0.1', name: 'Paper Comet', size: 1610612736, format: 'nsp', sha: 'ddb1a0f5fd72ac7f975f9dc616daa7fad19ec8b9367ca556e6562feaf2805dd0', in: ['ownfoil', 'aerofoil'] },
    { id: 'a_T1d3p1', tid: '0100ABCD00050000', app: '0100ABCD00050000', type: 'base', code: 0, version: '1.0.0', name: 'Tidepool Racers', size: 4294967296, format: 'xci', sha: 'ee9ce192bdfea2403e806d5719b299483afe182fdafe52a01b240c11017f9e25', in: ['folders'] },
  ];
  const SOURCES = ['folders', 'tinfoil', 'ownfoil', 'aerofoil'];
  const EXTRAS = { ownfoil: T.extraOwnfoil, aerofoil: T.extraAerofoil };

  const chosen = new Set(['folders', 'tinfoil']);
  const picker = root.querySelector('.sim-sources');
  const inBox = root.querySelector('.sim-in');
  const outBox = root.querySelector('.sim-out');
  const extrasBox = root.querySelector('.sim-extras');
  const jsonBox = root.querySelector('.sim-json code');

  const bytes = (n) => {
    const gb = n / 1073741824;
    const unit = gb >= 1 ? [gb, 'GB'] : [n / 1048576, 'MB'];
    return new Intl.NumberFormat(lang, { maximumFractionDigits: 1 }).format(unit[0]) + ' ' + unit[1];
  };
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const label = (source) => T['name' + source[0].toUpperCase() + source.slice(1)];
  const line = (file) => file.name + ' ' + file.version + ' · ' + file.format.toUpperCase() + ' · ' + bytes(file.size);

  function render() {
    for (const button of picker.querySelectorAll('button')) button.setAttribute('aria-pressed', String(chosen.has(button.dataset.source)));

    inBox.replaceChildren();
    for (const source of SOURCES.filter((s) => chosen.has(s))) {
      const group = el('div', 'sim-group');
      group.append(el('h4', null, label(source)));
      const list = el('ul');
      for (const file of FILES.filter((f) => f.in.includes(source))) list.append(el('li', null, line(file)));
      group.append(list);
      inBox.append(group);
    }
    if (!chosen.size) inBox.append(el('p', 'sim-empty', T.empty));

    const merged = FILES.map((f) => ({ file: f, seen: f.in.filter((s) => chosen.has(s)).length })).filter((m) => m.seen);
    outBox.replaceChildren();
    if (!merged.length) outBox.append(el('p', 'sim-empty', T.empty));
    for (const { file, seen } of merged) {
      const item = el('li', seen > 1 ? 'sim-item sim-merged' : 'sim-item');
      item.append(el('strong', null, line(file)));
      const meta = el('span', 'sim-meta');
      meta.append(el('code', null, file.type), el('code', null, file.sha.slice(0, 8)), el('span', null, T.seen.replace('{n}', String(seen))));
      item.append(meta);
      outBox.append(item);
    }

    extrasBox.replaceChildren();
    const extras = SOURCES.filter((s) => chosen.has(s) && EXTRAS[s]).map((s) => EXTRAS[s]);
    extrasBox.append(el('h3', null, T.extras));
    const list = el('ul', 'chips');
    for (const text of extras.length ? extras : [T.extraNone]) list.append(el('li', null, text));
    extrasBox.append(list);

    jsonBox.textContent = JSON.stringify({
      schema: 1,
      items: merged.map(({ file }) => ({
        id: file.id, platform: 'switch', title_id: file.tid, app_id: file.app, type: file.type, version_code: file.code,
        version_name: file.version, name: file.name, size: file.size, format: file.format, sha256: file.sha,
        added: '2026-11-01T10:00:00Z', file: '/api/lantern/v1/files/' + file.id.replace('a_', 'f_'),
      })),
    }, null, 2);
  }

  for (const source of SOURCES) {
    const button = el('button', null, label(source));
    button.type = 'button'; button.dataset.source = source;
    button.addEventListener('click', () => { if (window.countEvent) window.countEvent('simulator'); if (chosen.has(source)) chosen.delete(source); else chosen.add(source); render(); });
    picker.append(button);
  }
  render();
  root.hidden = false;
})();
