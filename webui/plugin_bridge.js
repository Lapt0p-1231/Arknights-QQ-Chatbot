(function () {
  if (window.ArkPlugin) return;

  var pending = new Map();
  var seq = 0;
  var token = '';
  var config = {};
  var ready = false;
  var readyQueue = [];

  function send(type, payload) {
    window.parent.postMessage(Object.assign({
      ark: 1,
      type: type,
      pluginId: (config && config.id) || '',
      token: token
    }, payload || {}), '*');
  }

  function flushReady() {
    ready = true;
    var queue = readyQueue.slice();
    readyQueue = [];
    queue.forEach(function (fn) {
      try { fn(config); } catch (e) { console.error(e); }
    });
  }

  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) return;
    var data = event.data;
    if (!data || data.ark !== 1) return;
    if (data.type === 'init') {
      config = data.config || {};
      token = data.token || '';
      flushReady();
      return;
    }
    if (data.type === 'rpc-result') {
      var item = pending.get(data.id);
      if (!item) return;
      pending.delete(data.id);
      if (data.ok) item.resolve(data.result);
      else item.reject(new Error(data.error || '插件后端请求失败'));
    }
  });

  window.ArkPlugin = {
    get config() { return config; },
    get ready() { return ready; },
    onReady: function (fn) {
      if (typeof fn !== 'function') return;
      if (ready) fn(config);
      else readyQueue.push(fn);
    },
    request: function (method, input) {
      var id = ++seq;
      return new Promise(function (resolve, reject) {
        pending.set(id, { resolve: resolve, reject: reject });
        send('rpc', { id: id, method: method, input: input });
      });
    },
    setHeight: function (height) {
      send('height', { height: Number(height) || 0 });
    }
  };

  function announce() {
    if (!ready) send('ready');
  }
  announce();
  setTimeout(announce, 60);
  setTimeout(announce, 250);
  setTimeout(announce, 1000);
  window.addEventListener('load', announce);
})();
