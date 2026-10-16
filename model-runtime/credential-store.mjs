// One credential file per app connection; all token mutations share a process-safe lock.
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';

export function profileCredentials(file, providerId) {
  const lockFile = `${file}.lock`;
  async function read() {
    try {
      const value = JSON.parse(await fs.readFile(file, 'utf8'));
      return value.provider === providerId ? value.credential : undefined;
    } catch (error) {
      if (error.code === 'ENOENT') return undefined;
      throw new Error('无法读取本机订阅凭据，请重新登录。');
    }
  }
  async function locked(operation, signal) {
    await fs.mkdir(path.dirname(file), { recursive: true });
    const deadline = Date.now() + 35_000;
    let handle;
    while (!handle) {
      signal?.throwIfAborted();
      try {
        handle = await fs.open(lockFile, 'wx', 0o600);
        await handle.writeFile(String(process.pid));
      } catch (error) {
        if (handle) { await handle.close(); await fs.unlink(lockFile).catch(() => {}); throw error; }
        if (error.code !== 'EEXIST') throw error;
        // A killed bridge cannot release its lock; only recover a definitely dead owner.
        try {
          const pid = Number(await fs.readFile(lockFile, 'utf8'));
          if (Number.isInteger(pid) && pid > 0) {
            try { process.kill(pid, 0); }
            catch (ownerError) { if (ownerError.code === 'ESRCH') await fs.unlink(lockFile).catch(() => {}); }
          } else {
            const stat = await fs.stat(lockFile);
            if (Date.now() - stat.mtimeMs > 120_000) await fs.unlink(lockFile).catch(() => {});
          }
        } catch {}
        if (Date.now() >= deadline) throw new Error('订阅凭据正在更新，请稍后重试。');
        await delay(100, undefined, { signal });
      }
    }
    try { return await operation(); }
    finally { await handle.close(); await fs.unlink(lockFile).catch(() => {}); }
  }
  return {
    read: async (id, options) => { options?.signal?.throwIfAborted(); return id === providerId ? read() : undefined; },
    list: async () => { const value = await read(); return value ? [{ providerId, type: value.type }] : []; },
    modify: async (id, fn, options) => {
      if (id !== providerId) throw new Error('凭据服务商不匹配。');
      return locked(async () => {
        const current = await read();
        const value = await fn(current);
        if (value === undefined) return current;
        options?.signal?.throwIfAborted();
        const temporary = `${file}.${randomUUID()}.tmp`;
        try {
          await fs.writeFile(temporary, JSON.stringify({ provider: providerId, credential: value }), { mode: 0o600 });
          await fs.rename(temporary, file);
        } finally { await fs.unlink(temporary).catch(() => {}); }
        return value;
      }, options?.signal);
    },
    delete: async (id, options) => {
      if (id !== providerId) return;
      await locked(async () => { await fs.unlink(file).catch(error => { if (error.code !== 'ENOENT') throw error; }); }, options?.signal);
    },
  };
}

export async function installationDeviceId(file) {
  const identityFile = path.join(path.dirname(file), 'device-id');
  await fs.mkdir(path.dirname(file), { recursive: true });
  const identity = randomUUID();
  try { await fs.writeFile(identityFile, identity, { flag: 'wx', mode: 0o600 }); }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
  return (await fs.readFile(identityFile, 'utf8')).trim();
}
