// The configuration writer of the Bridge's page: a few fields in, a docker-compose.yml and a config.toml out, to copy.
// It runs in the page and sends nothing; no password is asked for, the files name the environment variable the
// bridge reads each one from. Without script the section stays hidden.
'use strict';

(function () {
  const root = document.getElementById('generator');
  if (!root) return;
  const form = root.querySelector('form');
  const [composeBox, configBox] = root.querySelectorAll('.gen-out code');
  const SHOPS = ['tinfoil', 'ownfoil', 'aerofoil'];

  const value = (name) => form.elements[name].value.trim();
  const quote = (text) => JSON.stringify(text);

  function port() {
    const n = Number.parseInt(value('port'), 10);
    return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : 8080;
  }

  function render() {
    const games = value('games');
    const shops = SHOPS.filter((s) => form.elements['use-' + s].checked && value('url-' + s));
    for (const shop of SHOPS) root.querySelector('[data-shop="' + shop + '"] .gen-more').hidden = !form.elements['use-' + shop].checked;

    const secrets = shops.filter((s) => value('user-' + s)).map((s) => s.toUpperCase() + '_PASSWORD');
    const compose = [
      'services:',
      '  lantern-bridge:',
      '    image: lantern-bridge',
      '    restart: unless-stopped',
      '    ports:',
      '      - "' + port() + ':8080"',
      '    volumes:',
      '      - ./config:/config',
    ];
    if (games) compose.push('      - ' + quote(games + ':/games:ro'));
    if (secrets.length) compose.push('    environment:', ...secrets.map((name) => '      - ' + name));
    composeBox.textContent = compose.join('\n') + '\n';

    const config = ['schema = 1', 'name = ' + quote(value('name') || 'Lantern Bridge')];
    if (games) config.push('', '[[sources]]', 'id = "games"', 'kind = "folder"', 'path = "/games"');
    for (const shop of shops) {
      config.push('', '[[sources]]', 'id = ' + quote(shop), 'kind = ' + quote(shop), 'url = ' + quote(value('url-' + shop)));
      if (value('user-' + shop)) config.push('user = ' + quote(value('user-' + shop)), 'password_env = ' + quote(shop.toUpperCase() + '_PASSWORD'));
    }
    configBox.textContent = config.join('\n') + '\n';
  }

  form.addEventListener('submit', (event) => event.preventDefault());
  form.addEventListener('input', () => { if (window.countEvent) window.countEvent('config'); render(); });
  form.addEventListener('change', render);
  render();
  root.hidden = false;
})();
