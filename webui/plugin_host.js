(function () {
  if (window.ArkPluginHost) return;

  var state = { iframe: null, plugin: null, onHeight: null, token: '' };
  var lastReady = null;

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function theme() {
    return {
      background: cssVar('--bg', '#0d1015'),
      panel: cssVar('--panel', '#151a22'),
      text: cssVar('--text', '#d8dee9'),
      faint: cssVar('--faint', '#7c8798'),
      accent: cssVar('--accent', '#d6b36a'),
      border: cssVar('--line', '#28303c')
    };
  }

  function send(type, payload) {
    if (!state.iframe || !state.iframe.contentWindow) return;
    state.iframe.contentWindow.postMessage(Object.assign({
      ark: 1,
      type: type,
      pluginId: state.plugin && state.plugin.id,
      token: state.token
    }, payload || {}), '*');
  }

  function sendInit() {
    if (!state.iframe || !state.plugin) return;
    send('init', {
      config: {
        id: state.plugin.id,
        title: state.plugin.name || state.plugin.id,
        version: state.plugin.version || '',
        theme: theme()
      }
    });
  }

  async function rpc(source, message) {
    var id = message.id;
    var method = String(message.method || '');
    var input = message.input;
    if (!state.plugin || !method) return;
    try {
      var res = await fetch('/api/plugins/' + encodeURIComponent(state.plugin.id) + '/rpc/' + encodeURIComponent(method), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: input })
      });
      var data = await res.json().catch(function () { return {}; });
      source.postMessage({
        ark: 1,
        type: 'rpc-result',
        id: id,
        ok: res.ok,
        result: data.result,
        error: data.detail || data.error || ''
      }, '*');
    } catch (e) {
      source.postMessage({
        ark: 1,
        type: 'rpc-result',
        id: id,
        ok: false,
        error: String(e && e.message || e)
      }, '*');
    }
  }

  function onMessage(event) {
    if (!state.iframe || event.source !== state.iframe.contentWindow) return;
    var data = event.data;
    if (!data || data.ark !== 1) return;
    if (data.pluginId && state.plugin && data.pluginId !== state.plugin.id) return;
    if (data.type === 'ready') {
      lastReady = event.source;
      sendInit();
      return;
    }
    if (data.type === 'rpc') {
      rpc(event.source, data);
      return;
    }
    if (data.type === 'height') {
      var h = Math.max(180, Math.min(1400, Number(data.height) || 420));
      if (state.onHeight) state.onHeight(h);
    }
  }

  window.addEventListener('message', onMessage);

  window.ArkPluginHost = {
    mount: function (iframe, plugin, options) {
      state.iframe = iframe || null;
      state.plugin = plugin || null;
      state.onHeight = options && options.onHeight || null;
      state.token = Math.random().toString(36).slice(2);
      if (state.iframe && lastReady && lastReady === state.iframe.contentWindow) sendInit();
    },
    unmount: function () {
      state.iframe = null;
      state.plugin = null;
      state.onHeight = null;
      state.token = '';
    },
    theme: theme
  };
})();
