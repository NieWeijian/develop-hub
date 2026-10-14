// Map only the application HTTP listener to the private Unix socket.
const net = require('node:net');
const original = net.Server.prototype.listen;
net.Server.prototype.listen = function (...args) {
  const port = typeof args[0] === 'object' ? args[0]?.port : args[0];
  if (Number(port) === Number(process.env.DEV_HTTP_PORT)) {
    const callback = args.find(a => typeof a === 'function');
    return original.call(this, { path: '/var/dev/web.sock' }, callback);
  }
  return original.apply(this, args);
};
