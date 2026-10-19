import EmbeddedPostgres from 'embedded-postgres';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import getBinaries from './node_modules/embedded-postgres/dist/binary.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const runtime = path.resolve(process.env.LINSEMAIL_RUNTIME || path.join(root, '.runtime'));
mkdirSync(runtime, { recursive: true });
const credentialsPath = path.join(runtime, 'database.local.json');
if (!existsSync(credentialsPath)) {
  const password = randomBytes(24).toString('hex');
  writeFileSync(credentialsPath, JSON.stringify({
    user: 'linsemail', password, port: 55432,
    url: 'postgresql+psycopg://linsemail:' + password + '@127.0.0.1:55432/linsemail',
  }, null, 2), { flag: 'wx', mode: 0o600 });
}
const credentials = JSON.parse(readFileSync(credentialsPath, 'utf8'));
const databaseDir = path.join(runtime, 'postgres-data');
const postgres = new EmbeddedPostgres({
  databaseDir, user: credentials.user, password: credentials.password,
  port: credentials.port, persistent: true, authMethod: 'scram-sha-256',
  initdbFlags: ['--encoding=UTF8', '--locale=C'],
  postgresFlags: ['-h', '127.0.0.1', '-c', 'max_connections=50'],
  onLog: value => process.stderr.write(String(value).replaceAll(credentials.password, '[redacted]') + '\n'),
  onError: value => process.stderr.write(String(value).replaceAll(credentials.password, '[redacted]') + '\n'),
});
let closing = false;
const binaries = await getBinaries();
const pgCtl = path.join(path.dirname(binaries.postgres), process.platform === 'win32' ? 'pg_ctl.exe' : 'pg_ctl');
function control(args, allowFailure = false) {
  // PostgreSQL inherits handles on Windows; detached server handles must not
  // keep Node's capture pipes open after pg_ctl itself has already exited.
  const result = spawnSync(pgCtl, args, { encoding: 'utf8', windowsHide: true, stdio: 'ignore', timeout: 45000 });
  if (result.status !== 0 && !allowFailure) {
    process.stderr.write(String(result.error || 'pg_ctl exited ' + result.status).replaceAll(credentials.password, '[redacted]') + '\n');
    throw new Error('PostgreSQL control failed; inspect .runtime/postgres.log');
  }
  return result.status;
}
async function stop() {
  if (closing) return;
  closing = true;
  control(['stop', '-D', databaseDir, '-m', 'fast', '-w']);
  process.exit(0);
}
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
if (!existsSync(path.join(databaseDir, 'PG_VERSION'))) await postgres.initialise();
if (control(['status', '-D', databaseDir], true) !== 0) {
  control(['start', '-D', databaseDir, '-l', path.join(runtime, 'postgres.log'), '-o',
    '-p ' + credentials.port + ' -h 127.0.0.1 -c max_connections=50', '-w']);
}
const client = postgres.getPgClient('postgres', '127.0.0.1');
await client.connect();
const database = await client.query("SELECT 1 FROM pg_database WHERE datname = 'linsemail'");
if (database.rowCount === 0) await client.query('CREATE DATABASE linsemail');
await client.end();
console.log(JSON.stringify({ service: 'postgresql', status: 'ready', host: '127.0.0.1', port: credentials.port }));
const keepAlive = setInterval(() => {}, 60_000);
process.on('exit', () => clearInterval(keepAlive));
