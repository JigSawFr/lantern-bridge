// "Will my files be recognised?" on the Bridge's page: the bridge's own rule for reading a game's identity from a
// file name (src/lantern_bridge/identify.py), run in the page on the names the visitor pastes. Nothing is sent.
// tests/test_site_identify.py runs the same names through this file and through identify.py and compares.
'use strict';

(function () {
  const FORMATS = ['nsp', 'nsz', 'xci', 'xcz'];
  const ID = /\[([0-9A-Fa-f]{16})\]/;
  const VERSION = /\[v(\d{1,10})\]/;
  const TAGS = /\s*\[[^\]]*\]|\s*\([^)]*\)/g;

  // Same steps as identify(): the part after a '#', the last path segment, its suffix, the id in brackets.
  function identify(path) {
    const name = path.split('#').pop().split('/').filter(Boolean).pop() || '';
    const dot = name.lastIndexOf('.');
    const suffix = dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toLowerCase() : '';
    if (!FORMATS.includes(suffix)) return { why: 'format' };
    const stem = name.slice(0, dot);
    const found = ID.exec(stem);
    if (!found) return { why: 'id' };
    const appId = found[1].toUpperCase();
    const value = BigInt('0x' + appId);
    const low = value & 0xfffn;
    let type = 'dlc';
    let titleId;
    if (low === 0x000n) { type = 'base'; titleId = appId; }
    else if (low === 0x800n) { type = 'update'; titleId = appId.slice(0, 13) + '000'; }
    else titleId = ((value & ~0xfffn) - 0x1000n).toString(16).toUpperCase().padStart(16, '0');
    const version = VERSION.exec(stem);
    return {
      app_id: appId, title_id: titleId, type, version_code: version ? Number.parseInt(version[1], 10) : 0,
      name: stem.replace(TAGS, '').trim() || appId, format: suffix,
    };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { identify };
  if (typeof document === 'undefined') return;

  const root = document.getElementById('identify');
  if (!root) return;
  const T = root.dataset;
  const input = root.querySelector('textarea');
  const out = root.querySelector('.idf-out');
  const count = root.querySelector('.idf-count');
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const label = { base: T.kindBase, update: T.kindUpdate, dlc: T.kindDlc };

  function render() {
    const names = input.value.split('\n').map((l) => l.trim()).filter(Boolean);
    out.replaceChildren();
    if (!names.length) { count.textContent = T.empty; return; }
    let good = 0;
    for (const name of names) {
      const who = identify(name);
      const item = el('li', who.why ? 'idf-item idf-no' : 'idf-item idf-yes');
      item.append(el('code', 'idf-file', name));
      if (who.why) {
        item.append(el('span', 'idf-why', who.why === 'format' ? T.noFormat : T.noId));
      } else {
        good += 1;
        item.append(el('strong', null, who.name));
        const meta = el('span', 'idf-meta');
        meta.append(el('code', null, label[who.type]), el('span', null, T.fTitle + ' ' + who.title_id), el('span', null, T.fApp + ' ' + who.app_id), el('span', null, T.fVer + ' ' + who.version_code));
        item.append(meta);
      }
      out.append(item);
    }
    count.textContent = T.count.replace('{ok}', String(good)).replace('{all}', String(names.length));
  }

  input.addEventListener('input', () => { if (window.countEvent) window.countEvent('identify'); render(); });
  render();
  root.hidden = false;
})();
