import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import getBinaries from './node_modules/embedded-postgres/dist/binary.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const runtime = path.resolve(process.env.LINSEMAIL_RUNTIME || path.join(root, '.runtime'));
const directory = path.join(runtime, 'postgres-data');
if (existsSync(path.join(directory, 'PG_VERSION'))) {
  const binaries = await getBinaries();
  const ctl = path.join(path.dirname(binaries.postgres), process.platform === 'win32' ? 'pg_ctl.exe' : 'pg_ctl');
  const options = { windowsHide: true, stdio: 'inherit', timeout: 45000 };
  const status = spawnSync(ctl, ['status', '-D', directory], { ...options, stdio: 'ignore' });
  if (status.status === 0) {
    const stopped = spawnSync(ctl, ['stop', '-D', directory, '-m', 'fast', '-w'], options);
    process.exitCode = stopped.status === 0 ? 0 : 1;
  }
}
