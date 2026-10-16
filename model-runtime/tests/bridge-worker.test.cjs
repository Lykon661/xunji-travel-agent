// Local catalog requests only: no provider credentials or network calls.
const assert = require('node:assert/strict');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { test } = require('node:test');

const bridge = path.resolve(__dirname, '..', 'bridge.mjs');

function startWorker() {
  const child = spawn(process.execPath, [bridge, '--worker'], { stdio: ['pipe', 'pipe', 'ignore'] });
  const events = [];
  const waiters = [];
  readline.createInterface({ input: child.stdout }).on('line', line => {
    events.push(JSON.parse(line));
    for (const waiter of [...waiters]) waiter();
  });
  const until = predicate => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('worker did not answer')), 20000);
    const check = () => {
      if (!predicate(events)) return;
      clearTimeout(timer);
      waiters.splice(waiters.indexOf(check), 1);
      resolve(events);
    };
    waiters.push(check);
    check();
  });
  const send = message => child.stdin.write(`${JSON.stringify(message)}\n`);
  return { child, events, until, send };
}

test('concurrent requests are answered by id and each ends once', async () => {
  const worker = startWorker();
  try {
    worker.send({ id: 'first', request: { action: 'catalog' } });
    worker.send({ id: 'second', request: { action: 'catalog' } });
    const events = await worker.until(all => ['first', 'second'].every(id => all.some(event => event.id === id && event.type === 'end')));
    for (const id of ['first', 'second']) {
      const own = events.filter(event => event.id === id).map(event => event.type);
      assert.deepEqual(own, ['catalog', 'end']);
    }
  } finally {
    worker.child.kill();
  }
});

test('a broken request reports its own error and the worker keeps serving', async () => {
  const worker = startWorker();
  try {
    worker.send({ id: 'broken', request: { action: 'complete', config: { provider: 'custom', api: 'unknown-api' }, messages: [] } });
    worker.send('not a request');
    worker.send({ id: 'after', request: { action: 'catalog' } });
    const events = await worker.until(all => all.some(event => event.id === 'after' && event.type === 'end'));
    assert.ok(events.some(event => event.id === 'broken' && event.type === 'error'));
    assert.ok(events.some(event => event.id === 'broken' && event.type === 'end'));
    assert.ok(events.some(event => event.id === 'after' && event.type === 'catalog'));
  } finally {
    worker.child.kill();
  }
});

test('closing stdin stops the worker', async () => {
  const worker = startWorker();
  const exited = new Promise(resolve => worker.child.on('exit', code => resolve(code)));
  worker.child.stdin.end();
  assert.equal(await exited, 0);
});
