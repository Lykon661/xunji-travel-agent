// Synthetic child processes only: no FlyAI CLI, credentials, or network calls.
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const preload = path.resolve(__dirname, '..', 'flyai-exit.cjs');

function child(source, usePreload = true) {
  const args = usePreload ? ['--require', preload, '-e', source] : ['-e', source];
  const result = spawnSync(process.execPath, args, {
    encoding: 'utf8',
    timeout: 10000,
    maxBuffer: 4 * 1024 * 1024,
  });
  assert.ifError(result.error);
  assert.equal(result.signal, null);
  return result;
}

test('async exit zero drains stdout and stops the action', () => {
  const output = 'x'.repeat(512 * 1024);
  const result = child(`
    (async () => {
      await Promise.resolve();
      console.log('x'.repeat(512 * 1024));
      process.exit(0);
      console.log('continued');
      process.exit(1);
    })();
  `);
  assert.equal(result.status, 0);
  assert.equal(result.stdout, output + '\n');
  assert.equal(result.stderr, '');
});

test('synchronous exit one preserves its status and diagnostic', () => {
  const result = child(`
    console.error('synthetic provider failure');
    process.exit(1);
    console.log('continued');
  `);
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /synthetic provider failure/);
});

test('unexpected rejection remains a visible failure', () => {
  const result = child("Promise.reject(new Error('unexpected rejection')); ");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unexpected rejection/);
});

test('unexpected synchronous exception remains a visible failure', () => {
  const result = child("throw new Error('unexpected exception');");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unexpected exception/);
});

test('a child without the preload keeps normal process.exit behavior', () => {
  const result = child(`
    try {
      process.exit(0);
    } catch {
      console.log('exit was intercepted');
    }
  `, false);
  assert.equal(result.status, 0);
  assert.equal(result.stdout, '');
  assert.equal(result.stderr, '');
});
