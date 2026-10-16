// Interactive OAuth: public steps go to stdout, tokens only to the credential store.
import readline from 'node:readline';
import { profileCredentials, installationDeviceId } from './credential-store.mjs';
const send = value => process.stdout.write(`${JSON.stringify(value)}\n`);
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const lines = input[Symbol.asyncIterator]();
const abort = new AbortController();
const pending = new Map();
const timer = setTimeout(() => abort.abort(), 10 * 60_000);
let promptSequence = 0;

async function main() {
  const first = await lines.next();
  const request = JSON.parse(first.value);
  const { builtinModels } = await import('@earendil-works/pi-ai/providers/all');
  const credentials = profileCredentials(request.credentials_file, request.provider);
  const models = builtinModels({ credentials });
  const provider = models.getProvider(request.provider);
  if (!provider?.auth.oauth) throw new Error('unsupported');
  if (request.action === 'logout') {
    await models.logout(provider.id, { signal: abort.signal });
    send({ type: 'logged_out' });
    return;
  }
  const deviceId = await installationDeviceId(request.credentials_file);
  const listen = async () => {
    for await (const line of lines) {
      const message = JSON.parse(line);
      if (message.action === 'cancel') abort.abort();
      if (message.action === 'answer') pending.get(message.prompt_id)?.resolve(String(message.value || ''));
    }
    abort.abort();
  };
  listen().catch(() => abort.abort());
  await models.login(provider.id, 'oauth', {
    signal: abort.signal,
    notify: event => send({ type: 'event', event }),
    prompt: prompt => new Promise((resolve, reject) => {
      const id = String(++promptSequence);
      const signals = [abort.signal, ...(prompt.signal ? [prompt.signal] : [])];
      const cancel = () => {
        finish(); send({ type: 'prompt_cancelled', prompt_id: id });
        reject(new Error('cancelled'));
      };
      const finish = () => { pending.delete(id); for (const signal of signals) signal.removeEventListener('abort', cancel); };
      pending.set(id, { resolve: value => { finish(); resolve(value); } });
      for (const signal of signals) signal.addEventListener('abort', cancel, { once: true });
      if (signals.some(signal => signal.aborted)) { cancel(); return; }
      const { signal, ...publicPrompt } = prompt;
      send({ type: 'prompt', prompt_id: id, prompt: publicPrompt });
    }),
  }, { getDeviceId: () => deviceId });
  send({ type: 'logged_in' });
}

main().catch(error => {
  const status = /(?:status[=: ]+|HTTP\s+)([45]\d\d)/i.exec(error.message || '')?.[1];
  send({ type: 'error', cancelled: abort.signal.aborted,
    message: abort.signal.aborted ? '登录已取消或超过十分钟，请重新登录。'
      : error.code === 'EADDRINUSE' ? '本机授权回调端口被占用，请关闭其他正在登录的应用后重试。'
      : error.message === 'unsupported' ? '该服务商不支持订阅登录。'
      : `订阅授权失败${status ? `（HTTP ${status}）` : ''}。请重新登录，并确认账号具有相应订阅权限。` });
}).finally(() => { clearTimeout(timer); input.close(); process.stdin.destroy(); });
