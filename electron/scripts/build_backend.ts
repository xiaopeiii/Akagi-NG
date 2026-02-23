import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Backend build script that finds a usable Python executable.
 *
 * Resolution order:
 * 1) AKAGI_PYTHON (absolute path)
 * 2) akagi_backend/.venv
 * 3) python on PATH
 */
function buildBackend() {
  try {
    const electronDir = path.resolve(__dirname, '..');
    const projectRoot = path.resolve(electronDir, '..');
    const backendDir = path.join(projectRoot, 'akagi_backend');
    const buildScript = path.join(backendDir, 'scripts', 'build_backend.py');

    const candidates: Array<{ label: string; exe: string; mustExist: boolean }> = [];

    const envPython = (process.env.AKAGI_PYTHON || '').trim();
    if (envPython) {
      candidates.push({ label: 'AKAGI_PYTHON', exe: envPython, mustExist: true });
    }

    const venvPythonExec =
      process.platform === 'win32'
        ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
        : path.join(backendDir, '.venv', 'bin', 'python');
    candidates.push({ label: '.venv', exe: venvPythonExec, mustExist: true });

    // Some environments (e.g. conda prefix-style) place python.exe at the env root.
    if (process.platform === 'win32') {
      candidates.push({ label: '.venv (alt)', exe: path.join(backendDir, '.venv', 'python.exe'), mustExist: true });
    }

    candidates.push({ label: 'PATH', exe: 'python', mustExist: false });

    let pythonPath = 'python';
    let pythonSource = 'PATH';
    for (const c of candidates) {
      if (!c.mustExist || fs.existsSync(c.exe)) {
        pythonPath = c.exe;
        pythonSource = c.label;
        break;
      }
      if (c.label === 'AKAGI_PYTHON') {
        console.warn(`[build_backend] AKAGI_PYTHON is set but not found: ${c.exe}`);
      }
    }

    console.log(`[build_backend] Using Python (${pythonSource}): ${pythonPath}`);

    if (process.platform === 'darwin') {
      console.log('[build_backend] Installing riichi wheel for macOS builds...');
      const riichiInstall = spawnSync(
        pythonPath,
        [
          '-m',
          'pip',
          'install',
          '--find-links',
          'https://github.com/shinkuan/Akagi/releases/expanded_assets/v0.1.1-libriichi',
          'riichi>=0.1.1',
        ],
        {
          cwd: backendDir,
          stdio: 'inherit',
          shell: false,
        },
      );

      if (riichiInstall.error) {
        console.error(`[build_backend] Failed to install riichi wheel: ${riichiInstall.error.message}`);
        process.exit(1);
      }

      if (riichiInstall.status !== 0) {
        console.error(
          `[build_backend] riichi wheel install failed with exit code ${riichiInstall.status}`,
        );
        process.exit(riichiInstall.status || 1);
      }
    }

    console.log(`[build_backend] Running backend build script: ${buildScript}`);

    const result = spawnSync(pythonPath, [buildScript], {
      cwd: electronDir,
      stdio: 'inherit',
      shell: false,
    });

    if (result.error) {
      console.error(`[build_backend] Failed to spawn Python: ${result.error.message}`);
      process.exit(1);
    }

    if (result.status !== 0) {
      console.error(`[build_backend] Backend build failed with exit code ${result.status}`);
      process.exit(result.status || 1);
    }

    console.log('[build_backend] Backend build completed successfully');
  } catch (error) {
    console.error('[build_backend] Unexpected error during backend build:', error);
    process.exit(1);
  }
}

buildBackend();
