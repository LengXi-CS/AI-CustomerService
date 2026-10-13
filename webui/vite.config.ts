import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/admin/',
  build: {
    outDir: '../src/fastgpt_assistant/server/admin_static',
    emptyOutDir: true,
  },
  server: {
    proxy: { '/api/admin': { target: 'http://127.0.0.1:5330' } },
  },
})
