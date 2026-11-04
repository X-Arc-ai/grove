/* Public guide/catalog assets load once, only when a person opens their screen. */
(function () {
  'use strict';
  var urls = {}, loads = {};
  window.GroveDeferred = {
    register: function (name, path) { urls[name] = path; },
    load: function (name) {
      if (loads[name]) return loads[name];
      var path = urls[name];
      if (!path || !/^\/assets\/grove\/[a-f0-9]{64}\.js$/.test(path)) {
        return Promise.reject(new Error('This view is not available. Try again.'));
      }
      loads[name] = new Promise(function (resolve, reject) {
        var script = document.createElement('script');
        script.src = path;
        script.onload = resolve;
        script.onerror = function () {
          delete loads[name];
          script.remove();
          reject(new Error('This view could not load. Try again.'));
        };
        document.head.appendChild(script);
      });
      return loads[name];
    }
  };
}());
