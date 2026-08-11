import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  // Monaco Editor worker configuration
  worker: {
    format: 'es',
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          tdesign: ['tdesign-vue-next'],
          monaco: ['monaco-editor'],
          charts: ['echarts'],
          markdown: ['markdown-it', 'dompurify', 'katex', 'mermaid'],
          g6: ['@antv/g6', 'dagre'],
        },
      },
    },
    chunkSizeWarningLimit: 600,
  },
})
