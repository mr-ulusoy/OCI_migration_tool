import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const proxyTarget = loadEnv(mode, '.', '').VITE_DEV_PROXY_TARGET

  return {
    plugins: [react()],
    server: proxyTarget ? {
      proxy: {
        '/backend': {
          target: proxyTarget,
          changeOrigin: true,
          rewrite: path => path.replace(/^\/backend/, ''),
          configure: proxy => {
            proxy.on('proxyReq', proxyRequest => proxyRequest.removeHeader('origin'))
          },
        },
      },
    } : undefined,
  }
})
