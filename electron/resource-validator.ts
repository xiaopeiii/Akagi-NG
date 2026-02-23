import fs from 'fs';
import path from 'path';

export interface ResourceStatus {
  lib: boolean;
  models: boolean;
  missingCritical: string[];
  missingOptional: string[];
}

export class ResourceValidator {
  constructor(private projectRoot: string) {}

  public validate(): ResourceStatus {
    const libPath = path.join(this.projectRoot, 'lib');
    const modelsPath = path.join(this.projectRoot, 'models');

    const libExists = this.checkLib(libPath);
    const modelsExists = this.checkModels(modelsPath);

    const missingCritical: string[] = [];
    const missingOptional: string[] = [];

    if (!libExists) {
      missingCritical.push('lib');
    }

    if (!modelsExists) {
      missingOptional.push('models');
    }

    return {
      lib: libExists,
      models: modelsExists,
      missingCritical,
      missingOptional,
    };
  }

  private checkLib(dirPath: string): boolean {
    const isWin = process.platform === 'win32';
    const libRiichi = isWin ? 'libriichi.pyd' : 'libriichi.so';
    const localLibExists = fs.existsSync(dirPath) && fs.readdirSync(dirPath).includes(libRiichi);

    // Prefer local lib/ check first.
    if (localLibExists) return true;

    // On macOS arm64, riichi wheel files can be bundled under the PyInstaller backend directory.
    if (process.platform === 'darwin') {
      return this.checkBundledRiichi(path.join(this.projectRoot, 'bin'));
    }

    // libriichi3p is optional (3P only). For 4P-only usage we only require libriichi.
    return false;
  }

  private checkBundledRiichi(binPath: string): boolean {
    if (!fs.existsSync(binPath)) return false;

    const stack: string[] = [binPath];
    while (stack.length > 0) {
      const current = stack.pop();
      if (!current) continue;

      let entries: fs.Dirent[];
      try {
        entries = fs.readdirSync(current, { withFileTypes: true });
      } catch {
        continue;
      }

      for (const entry of entries) {
        const full = path.join(current, entry.name);
        if (entry.isDirectory()) {
          stack.push(full);
          continue;
        }

        if (!entry.isFile()) continue;

        const isRiichiSo = entry.name.startsWith('riichi') && entry.name.endsWith('.so');
        const isLikelyBackendRiichiPath =
          full.includes(`${path.sep}_internal${path.sep}riichi${path.sep}`) ||
          full.includes(`${path.sep}_internal${path.sep}`);

        if (isRiichiSo && isLikelyBackendRiichiPath) return true;
      }
    }

    return false;
  }

  private checkModels(dirPath: string): boolean {
    if (!fs.existsSync(dirPath)) return false;
    const files = fs.readdirSync(dirPath);
    // Look for at least one .pth file
    return files.some((f) => f.endsWith('.pth'));
  }
}
