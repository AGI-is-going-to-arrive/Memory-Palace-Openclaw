import { configDefaults, defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.js',
    globals: true,
    css: true,
    poolOptions: {
      forks: {
        execArgv: ['--no-experimental-webstorage'],
      },
      threads: {
        execArgv: ['--no-experimental-webstorage'],
      },
    },
    clearMocks: true,
    restoreMocks: true,
    exclude: [...configDefaults.exclude, 'e2e/**', '**/*.tmp.test.*'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      reportsDirectory: './coverage',
      include: ['src/**/*.{js,jsx}'],
      exclude: ['src/test/**', 'src/**/*.test.{js,jsx}', 'src/main.jsx'],
    },
  },
});
