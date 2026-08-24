import { execFileSync } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, readdirSync, statSync } from 'node:fs'
import { dirname, extname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const projectRoot = resolve(frontendRoot, '..')
const backendRoot = join(projectRoot, 'backend')
const tauriRoot = join(frontendRoot, 'src-tauri')
const sourceRoot = join(backendRoot, 'src', 'deskpilot')
const entryPoint = join(sourceRoot, 'sidecar.py')
const python = process.platform === 'win32'
  ? join(backendRoot, '.venv', 'Scripts', 'python.exe')
  : join(backendRoot, '.venv', 'bin', 'python')

function latestMtime(path) {
  const details = statSync(path)
  if (!details.isDirectory()) return details.mtimeMs
  return readdirSync(path, { withFileTypes: true }).reduce((latest, entry) => {
    const child = join(path, entry.name)
    return Math.max(latest, latestMtime(child))
  }, details.mtimeMs)
}

if (!existsSync(python)) throw new Error(`DeskPilot backend virtualenv is missing: ${python}`)

const rustVersion = execFileSync('rustc', ['-vV'], { encoding: 'utf8' })
const host = rustVersion.match(/^host:\s+(.+)$/m)?.[1]
if (!host || !/^[a-zA-Z0-9_.-]+$/.test(host)) {
  throw new Error('Unable to resolve the Rust host triple for the desktop sidecar')
}

const extension = process.platform === 'win32' ? '.exe' : ''
const binariesDir = join(tauriRoot, 'binaries')
const target = join(binariesDir, `deskpilot-backend-sidecar-${host}${extension}`)
const buildRoot = join(tauriRoot, 'target', 'sidecar-build')
const distRoot = join(buildRoot, 'dist')
const built = join(distRoot, `deskpilot-backend-sidecar${extension}`)
const inputs = [
  sourceRoot,
  join(backendRoot, 'pyproject.toml'),
  join(backendRoot, 'uv.lock'),
  fileURLToPath(import.meta.url),
]
const newestInput = Math.max(...inputs.map(latestMtime))

mkdirSync(binariesDir, { recursive: true })
if (existsSync(target) && statSync(target).mtimeMs >= newestInput) {
  process.stdout.write(`DeskPilot sidecar is current: ${target}\n`)
  process.exit(0)
}

execFileSync(python, [
  '-m',
  'PyInstaller',
  '--noconfirm',
  '--clean',
  '--onefile',
  '--name',
  'deskpilot-backend-sidecar',
  '--paths',
  join(backendRoot, 'src'),
  '--collect-all',
  'deskpilot',
  '--collect-all',
  'aiosqlite',
  '--recursive-copy-metadata',
  'deskpilot-backend',
  '--distpath',
  distRoot,
  '--workpath',
  join(buildRoot, 'work'),
  '--specpath',
  join(buildRoot, 'spec'),
  entryPoint,
], {
  cwd: backendRoot,
  stdio: 'inherit',
  env: {
    ...process.env,
    PYTHONNOUSERSITE: '1',
  },
})

if (!existsSync(built) || extname(built) !== extension) {
  throw new Error(`PyInstaller did not produce the expected sidecar: ${built}`)
}
copyFileSync(built, target)
process.stdout.write(`Built DeskPilot sidecar: ${target}\n`)
