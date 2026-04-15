import { removePathWithRetries } from './tempArtifacts.js';

export default async function globalTeardown() {
  if (process.env.PLAYWRIGHT_E2E_OWNS_TEMP_ROOT !== '1') {
    return;
  }
  try {
    await removePathWithRetries(process.env.PLAYWRIGHT_E2E_TEMP_ROOT);
  } catch (error) {
    if (['EBUSY', 'EMFILE', 'ENOTEMPTY', 'EPERM'].includes(error?.code)) {
      console.warn(
        `Skipping Playwright temp cleanup for busy path: ${process.env.PLAYWRIGHT_E2E_TEMP_ROOT}`
      );
      return;
    }
    throw error;
  }
}
