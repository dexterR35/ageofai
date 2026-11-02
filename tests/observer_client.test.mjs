import test from "node:test";
import assert from "node:assert/strict";
import { LiveConnection } from "../web/connection.js";

function harness() {
  const timers = new Map(), sockets = [], states = [], messages = [];
  let next = 0;
  class Socket {
    constructor(url) { this.url = url; sockets.push(this); }
    close() { this.closed = true; }
    message(value) { this.onmessage?.({ data: JSON.stringify(value) }); }
  }
  const connection = new LiveConnection("ws://example.test/ws", {
    Socket, onState: (...state) => states.push(state), onMessage: (message) => messages.push(message),
    later: (fn, delay) => { timers.set(++next, { fn, delay }); return next; },
    cancel: (id) => timers.delete(id),
  });
  const fire = (id) => {
    const timer = timers.get(id);
    assert.ok(timer, `Timer ${id} exists`);
    timers.delete(id);
    timer.fn();
  };
  connection.connect();
  return { connection, timers, sockets, states, messages, fire };
}

test("a greeting doesn't claim the world is live until a frame arrives", () => {
  const h = harness();
  h.sockets[0].message({ type: "hello" });
  assert.deepEqual(h.states.at(-1), ["connecting"]);
  h.sockets[0].message({ type: "frame", seq: 1 });
  assert.deepEqual(h.states.at(-1), ["live"]);
  assert.equal(h.messages.length, 2);
});

test("retries back off to 30 seconds and a frame resets the delay", () => {
  const h = harness();
  for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
    h.sockets.at(-1).onclose();
    assert.deepEqual(h.states.at(-1), ["reconnecting", delay]);
    assert.equal(h.timers.size, 1);
    h.fire(h.connection.retryTimer);
  }
  h.sockets.at(-1).message({ type: "frame" });
  h.sockets.at(-1).onclose();
  assert.deepEqual(h.states.at(-1), ["reconnecting", 1000]);
});

test("manual retries cancel old timers and detach obsolete sockets", () => {
  const h = harness();
  const original = h.sockets[0];
  original.onclose();
  h.connection.retry();
  assert.ok(original.closed);
  assert.equal(original.onmessage, null);
  assert.equal(original.onclose, null);
  assert.equal(h.sockets.length, 2);
  assert.equal(h.timers.size, 1); // only the new watchdog
});

test("an open but silent connection is replaced after 30 seconds", () => {
  const h = harness();
  assert.equal(h.timers.get(h.connection.watchdog).delay, 30000);
  h.fire(h.connection.watchdog);
  assert.ok(h.sockets[0].closed);
  assert.deepEqual(h.states.at(-1), ["reconnecting", 1000]);
});

test("a dropped-delta signal immediately reconnects for a fresh greeting", () => {
  const h = harness();
  h.sockets[0].message({ type: "resync" });
  assert.ok(h.sockets[0].closed);
  assert.equal(h.sockets.length, 2);
  assert.equal(h.messages.length, 0);
});

test("stopping releases timers and prevents new connections", () => {
  const h = harness();
  h.connection.stop();
  h.connection.retry();
  assert.ok(h.sockets[0].closed);
  assert.equal(h.timers.size, 0);
  assert.equal(h.sockets.length, 1);
});

test("browser timer functions are called without the connection as their receiver", () => {
  const originalLater = globalThis.setTimeout, originalCancel = globalThis.clearTimeout;
  let connection;
  try {
    globalThis.setTimeout = function () { assert.notEqual(this, connection); return 1; };
    globalThis.clearTimeout = function () { assert.notEqual(this, connection); };
    connection = new LiveConnection("ws://example.test/ws", {
      Socket: class { close() {} }, onMessage() {}, onState() {},
    });
    connection.connect();
    connection.stop();
  } finally {
    globalThis.setTimeout = originalLater;
    globalThis.clearTimeout = originalCancel;
  }
});
