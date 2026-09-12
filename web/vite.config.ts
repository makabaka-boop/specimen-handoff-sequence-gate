import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Compose 下浏览器经 nginx 访问 /api；本地 vite dev 时代理到 8000。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
});
