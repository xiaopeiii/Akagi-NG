const fs = require('fs');
const path = require('path');
const { notarize } = require('@electron/notarize');

module.exports = async function notarizeApp(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const appleId = process.env.APPLE_ID;
  const appleIdPassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
  const teamId = process.env.APPLE_TEAM_ID;

  if (!appleId || !appleIdPassword || !teamId) {
    console.log(
      '[notarize] Skipping notarization: missing APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID.',
    );
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const appPath = path.join(context.appOutDir, `${appName}.app`);
  if (!fs.existsSync(appPath)) {
    console.warn(`[notarize] App not found at ${appPath}, skipping notarization.`);
    return;
  }

  const appBundleId = context.packager.appInfo.id;
  console.log(`[notarize] Notarizing ${appName}.app (${appBundleId})...`);
  await notarize({
    tool: 'notarytool',
    appBundleId,
    appPath,
    appleId,
    appleIdPassword,
    teamId,
  });
  console.log('[notarize] Notarization completed.');
};
