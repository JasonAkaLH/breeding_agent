import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000';

export default defineConfig({
  base: process.env.VITE_APP_BASE_PATH ?? '/',
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return undefined;
          }
          if (id.includes('/react/') || id.includes('/react-dom/') || id.includes('/scheduler/')) {
            return 'vendor-react';
          }
          if (id.includes('/antd/es/table') || id.includes('/antd/es/pagination')) {
            return 'vendor-antd-table';
          }
          if (id.includes('/antd/es/input') || id.includes('/antd/es/button') || id.includes('/antd/es/select')) {
            return 'vendor-antd-form';
          }
          if (id.includes('/antd/')) {
            return 'vendor-antd';
          }
          if (id.includes('/@ant-design/icons') || id.includes('/@ant-design/cssinjs')) {
            return 'vendor-ant-design-runtime';
          }
          if (id.includes('/@rc-component/') || id.includes('/rc-')) {
            return 'vendor-rc';
          }
          if (id.includes('/dayjs/')) {
            return 'vendor-date';
          }
          return 'vendor';
        },
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: Number(process.env.VITE_DEV_PORT ?? 5173),
    strictPort: true,
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    globals: true,
    css: true,
  },
});
